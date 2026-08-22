"""
tests/test_wiz_cache.py — le TTL de Wiz, et l'exception qui compte.

POURQUOI (mesuré le 2026-08-22, /wiz entièrement vide) : les 14 analyses de
la fenêtre étaient toutes INDISPONIBLE, et le cache les tenait pour des
analyses valides pendant 8 h. Or une ligne INDISPONIBLE n'est pas une
analyse : c'est « je n'ai pas pu chercher » — quota du connecteur, source
muette, JSON illisible. La garder en cache fige un incident passager pour la
journée entière et, surtout, retarde de 8 h l'effet de toute réparation des
sources : on répare, le run suivant saute tous les matchs « déjà analysés »,
et la page reste vide comme avant.

`run_wiz._needs_analysis()` distingue donc les deux : une vraie analyse tient
WIZ_TTL_H (8 h), une non-réponse est retentée dès le run suivant
(WIZ_RETRY_UNAVAILABLE_H = 2 h, cadence du cron).
"""
from datetime import datetime, timedelta, timezone

import pytest

import run_wiz
from core.constants import WIZ_CONFIRM_WINDOW_H, WIZ_RETRY_UNAVAILABLE_H
from core.wiz_engine import INDISPONIBLE

NOW = datetime(2026, 8, 22, 14, 0, tzinfo=timezone.utc)


def _ctx(kickoff_h_from_now: float | None = 20.0) -> dict:
    ko = NOW + timedelta(hours=kickoff_h_from_now) if kickoff_h_from_now is not None else None
    return {"match": "A vs B", "key": "name:a vs b", "kickoff_dt": ko}


def _il_y_a(heures: float):
    return NOW - timedelta(hours=heures)


class TestTTLNominal:
    def test_jamais_analyse(self):
        assert run_wiz._needs_analysis(_ctx(), None, NOW)[0] is True

    def test_analyse_recente_tient_le_cache(self):
        needed, why = run_wiz._needs_analysis(_ctx(), (_il_y_a(1), "NEUTRE"), NOW)
        assert needed is False and "en cache" in why

    def test_analyse_perimee_est_refaite(self):
        needed, why = run_wiz._needs_analysis(_ctx(), (_il_y_a(9), "CONFIRME"), NOW)
        assert needed is True and "vieille" in why

    def test_fenetre_de_confirmation_prime_sur_le_cache(self):
        # Les compositions officielles tombent à T-3h : une analyse faite
        # avant l'entrée dans la fenêtre ne les a pas vues, même si le TTL
        # la déclare fraîche.
        ctx = _ctx(kickoff_h_from_now=WIZ_CONFIRM_WINDOW_H - 1)   # entrée en fenêtre il y a 1h
        needed, why = run_wiz._needs_analysis(ctx, (_il_y_a(1.5), "CONFIRME"), NOW)
        assert needed is True and "confirmation" in why


class TestNonReponse:
    """LA régression du 2026-08-22 : une non-réponse mise en cache 8 h."""

    def test_indisponible_est_reessaye_au_run_suivant(self):
        age = WIZ_RETRY_UNAVAILABLE_H + 0.1
        needed, why = run_wiz._needs_analysis(_ctx(), (_il_y_a(age), INDISPONIBLE), NOW)
        assert needed is True and "nouvel essai" in why

    def test_indisponible_du_run_en_cours_nest_pas_reessaye_en_boucle(self):
        # Retenter dans les minutes qui suivent ne peut que rejouer le même
        # échec et brûler le budget du run.
        needed, why = run_wiz._needs_analysis(_ctx(), (_il_y_a(0.2), INDISPONIBLE), NOW)
        assert needed is False and "INDISPONIBLE" in why

    @pytest.mark.parametrize("verdict", ["CONFIRME", "NEUTRE", "ALERTE", "VETO"])
    def test_un_vrai_verdict_garde_le_TTL_long(self, verdict):
        # Le raccourci ne doit PAS s'appliquer aux analyses abouties : les
        # compositions ne bougent pas toutes les deux heures, et Mistral est
        # limité à 2 RPM.
        age = WIZ_RETRY_UNAVAILABLE_H + 0.1
        needed, _why = run_wiz._needs_analysis(_ctx(), (_il_y_a(age), verdict), NOW)
        assert needed is False


def test_le_verdict_est_bien_relu_depuis_supabase():
    """`_load_last_analyses` doit SELECT le verdict, sinon `_needs_analysis`
    ne peut pas distinguer une analyse d'une non-réponse."""
    vu = {}

    class _Table:
        def select(self, cols):
            vu["cols"] = cols
            return self

        def gte(self, *_a):
            return self

        def order(self, *_a, **_k):
            return self

        def limit(self, *_a):
            return self

        def execute(self):
            return type("R", (), {"data": [
                {"match_id": "", "match": "A vs B",
                 "analyzed_at": "2026-08-22T12:00:00+00:00", "verdict": INDISPONIBLE},
            ]})()

    class _SB:
        def table(self, _name):
            return _Table()

    out = run_wiz._load_last_analyses(_SB(), ["name:a vs b"])
    assert "verdict" in vu["cols"]
    assert out["name:a vs b"][1] == INDISPONIBLE
