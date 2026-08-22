"""
tests/test_wiz_retry.py — le TTL de Wiz ne s'applique pas à une NON-RÉPONSE.

Constaté le 2026-08-22 : /wiz était vide alors que `wiz_analysis` contenait
14 lignes, toutes INDISPONIBLE — c'est-à-dire 14 fois « je n'ai pas pu
chercher », mises en cache 8 h par le TTL ordinaire. Deux conséquences, aussi
mauvaises l'une que l'autre : un incident passager (quota du connecteur épuisé
pendant un run) blanchissait la page pour la journée, et toute réparation des
sources mettait 8 h à se voir à l'écran.

Une analyse et un aveu d'échec ne se périment pas au même rythme. Ce module
épingle la distinction — et le fait que le reste du TTL, lui, ne bouge pas.
"""
from datetime import datetime, timedelta, timezone

import pytest

import run_wiz
from core.constants import WIZ_CONFIRM_WINDOW_H, WIZ_RETRY_UNAVAILABLE_H
from core.wiz_engine import INDISPONIBLE

NOW = datetime(2026, 8, 22, 14, 0, tzinfo=timezone.utc)
TTL = run_wiz.wiz_ttl_h()


def _il_y_a(heures: float, verdict: str = "NEUTRE"):
    """Le couple (analyzed_at, verdict) que `_load_last_analyses` renvoie."""
    return (NOW - timedelta(hours=heures), verdict)


class TestNeedsAnalysis:
    def test_jamais_vu(self):
        assert run_wiz._needs_analysis({}, None, NOW)[0] is True

    def test_analyse_fraiche_reste_en_cache(self):
        refaire, motif = run_wiz._needs_analysis({}, _il_y_a(1), NOW)
        assert refaire is False and "en cache" in motif

    def test_analyse_perimee_est_refaite(self):
        assert run_wiz._needs_analysis({}, _il_y_a(TTL + 0.1), NOW)[0] is True

    def test_indisponible_est_rejoue_des_le_run_suivant(self):
        """LA correction du 2026-08-22 : un échec ne se met pas en cache 8 h."""
        refaire, motif = run_wiz._needs_analysis(
            {}, _il_y_a(WIZ_RETRY_UNAVAILABLE_H, INDISPONIBLE), NOW)
        assert refaire is True and "INDISPONIBLE" in motif

    def test_indisponible_tres_recent_nest_pas_rejoue_en_boucle(self):
        """Le cron passe toutes les 2 h ; deux runs rapprochés (rattrapage,
        relance manuelle) ne doivent pas rebrûler le quota sur le même match."""
        refaire, motif = run_wiz._needs_analysis(
            {}, _il_y_a(WIZ_RETRY_UNAVAILABLE_H / 2, INDISPONIBLE), NOW)
        assert refaire is False and "INDISPONIBLE récent" in motif

    def test_le_rejeu_est_plus_court_que_le_ttl_normal(self):
        # Sinon la règle est inerte : elle ne se déclencherait jamais avant
        # l'expiration ordinaire, et la panne resterait affichée 8 h.
        assert WIZ_RETRY_UNAVAILABLE_H < TTL

    def test_un_verdict_reel_garde_le_ttl_normal(self):
        """Seule la non-réponse est rejouée : un VETO frais reste un VETO —
        Wiz ne doit pas se mettre à repayer un appel par run et par match."""
        age = (WIZ_RETRY_UNAVAILABLE_H + TTL) / 2
        for verdict in ("CONFIRME", "NEUTRE", "ALERTE", "VETO"):
            refaire, _ = run_wiz._needs_analysis({}, _il_y_a(age, verdict), NOW)
            assert refaire is False, verdict

    def test_fenetre_de_confirmation_toujours_prioritaire(self):
        """Les compositions officielles tombent à T-3h : cette seconde passe
        n'est pas concernée par le débat sur les non-réponses."""
        # Coup d'envoi dans W-1 heures : la fenêtre s'est ouverte il y a 1 h.
        # L'analyse d'il y a 1,5 h est donc ANTÉRIEURE à son ouverture — elle
        # n'a pas pu voir les compositions, on repasse malgré le TTL.
        ko = NOW + timedelta(hours=WIZ_CONFIRM_WINDOW_H - 1)
        refaire, motif = run_wiz._needs_analysis({"kickoff_dt": ko}, _il_y_a(1.5), NOW)
        assert refaire is True and "confirmation" in motif

    def test_une_seule_seconde_passe_dans_la_fenetre(self):
        """Analyse déjà faite DANS la fenêtre : on ne repasse pas en boucle."""
        ko = NOW + timedelta(hours=WIZ_CONFIRM_WINDOW_H - 1)
        refaire, _ = run_wiz._needs_analysis({"kickoff_dt": ko}, _il_y_a(0.5), NOW)
        assert refaire is False


class _Table:
    def __init__(self, rows):
        self.rows = rows

    def select(self, *_a, **_k): return self
    def gte(self, *_a, **_k):    return self
    def order(self, *_a, **_k):  return self
    def limit(self, *_a, **_k):  return self

    def execute(self):
        return type("R", (), {"data": self.rows})()


class _SB:
    def __init__(self, rows):
        self.rows = rows

    def table(self, _name):
        return _Table(self.rows)


class TestLoadLastAnalyses:
    def test_le_verdict_remonte_avec_la_date(self):
        sb = _SB([{"match_id": "m1", "match": "A vs B",
                   "analyzed_at": "2026-08-22T12:00:00+00:00",
                   "verdict": INDISPONIBLE}])
        assert run_wiz._load_last_analyses(sb, ["m1"])["m1"][1] == INDISPONIBLE

    def test_la_plus_recente_gagne(self):
        sb = _SB([
            {"match_id": "m1", "analyzed_at": "2026-08-22T12:00:00+00:00", "verdict": "VETO"},
            {"match_id": "m1", "analyzed_at": "2026-08-22T09:00:00+00:00", "verdict": INDISPONIBLE},
        ])
        assert run_wiz._load_last_analyses(sb, ["m1"])["m1"][1] == "VETO"

    def test_verdict_absent_ne_casse_rien(self):
        # Ligne écrite par une version antérieure : on veut un couple
        # exploitable, pas une KeyError en plein run.
        sb = _SB([{"match_id": "m1", "analyzed_at": "2026-08-22T12:00:00+00:00"}])
        dt, verdict = run_wiz._load_last_analyses(sb, ["m1"])["m1"]
        assert verdict == "" and dt.year == 2026

    def test_table_absente_nempeche_pas_le_run(self):
        class _Boom:
            def table(self, _n):
                raise RuntimeError('relation "wiz_analysis" does not exist')

        assert run_wiz._load_last_analyses(_Boom(), ["m1"]) == {}


@pytest.mark.parametrize("last", [None, _il_y_a(1), _il_y_a(1, INDISPONIBLE)])
def test_la_decision_de_cache_ne_leve_jamais(last):
    """Un run Wiz ne meurt pas sur une décision de cache."""
    refaire, motif = run_wiz._needs_analysis({}, last, NOW)
    assert isinstance(refaire, bool) and motif
