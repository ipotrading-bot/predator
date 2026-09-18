"""
tests/test_learning_telegram.py — où part la couche d'apprentissage sur
Telegram, depuis le 2026-09-18.

Demande opérateur : « je ne veux pas tout le temps ce message ; message
learning une fois par jour, avec le message scan de 6 h, par contre signaler
erreurs et anomalies dès que ça survient. » Le pavé « 🧠 Learning (dernier
cycle) » partait en pied de CHAQUE digest de 2 h — douze fois par jour, cinq
lignes à chaque fois, et les deux qui signalaient une erreur de données s'y
noyaient.

Trois gardes :
  1. le marqueur d'anomalie est posé À LA SOURCE (core/learning_layer.py) ;
  2. le digest ne porte plus QUE les anomalies, et seulement fraîches ;
  3. le résumé complet part avec le premier scan `standard` du jour, dont
     l'heure est DÉRIVÉE du cron (règle n°6).
"""
from datetime import datetime, timedelta, timezone

import run_engine
import run_rapport
from core.learning_layer import (ANOMALIE_MARQUEUR, _edge_band_diagnostic,
                                 lignes_anomalies)
from scripts.ci_scan_mode import standard_slots

NOW = datetime(2026, 9, 18, 13, 6, tzinfo=timezone.utc)

ROUTINE = "soccer/spreads: seuil 2.20% → 2.60% (réussite 54% IC95 35-71%)"
ANOMALIE = f"{ANOMALIE_MARQUEUR} [baseball] Edge 4-8%+ sous son point mort — possible erreur de données"


class TestMarqueurALaSource:
    def test_lignes_anomalies_ne_garde_que_les_lignes_marquees(self):
        assert lignes_anomalies([ROUTINE, ANOMALIE]) == [ANOMALIE]

    def test_un_resume_de_routine_ne_declenche_rien(self):
        assert lignes_anomalies([ROUTINE]) == []

    def test_le_diagnostic_de_bande_porte_le_marqueur(self):
        # Bande haute perdante (cote 2.0, point mort 62.5% après taxe) face à
        # une bande basse largement au-dessus du sien : c'est le cas que le
        # diagnostic doit crier — une erreur de données/matching probable.
        rows = ([{"outcome": "WIN", "initial_edge": 1.0, "odds": 2.0} for _ in range(20)]
                + [{"outcome": "LOSS", "initial_edge": 5.0, "odds": 2.0} for _ in range(10)])
        msg = _edge_band_diagnostic("baseball", rows)
        assert msg and msg.startswith(ANOMALIE_MARQUEUR)
        assert lignes_anomalies([msg]) == [msg]

    def test_le_retrait_propose_porte_le_marqueur(self):
        # L'autre ligne d'alerte du résumé (voir _save_sport_verdicts) : elle
        # doit rester marquée, sinon un retrait proposé attendrait le matin.
        from pathlib import Path

        src = Path(__file__).resolve().parents[1] / "core" / "learning_layer.py"
        ecrites = [l for l in src.read_text(encoding="utf-8").splitlines()
                   if "retrait proposé —" in l]
        assert ecrites, "la ligne « retrait proposé » a disparu du résumé"
        assert all(ANOMALIE_MARQUEUR in l for l in ecrites)


class TestDigestSansPave:
    def test_plus_de_pave_learning_dans_le_digest(self):
        msg = run_rapport._composer([_sig()], [], NOW)
        assert "Learning" not in msg

    def test_une_anomalie_passe_en_tete_avec_les_alertes(self):
        msg = run_rapport._composer([_sig()], [], NOW, anomalies=[ANOMALIE])
        assert "Anomalie" in msg
        assert msg.index("Anomalie") < msg.index("Toulouse")

    def test_une_anomalie_seule_vaut_un_message(self):
        # Rien à lister, moteur vivant : le digest se tait — sauf si le
        # dernier cycle d'audit a trouvé une erreur.
        assert run_rapport._composer([], [], NOW) is None
        msg = run_rapport._composer([], [], NOW, anomalies=[ANOMALIE])
        assert ANOMALIE in msg


class TestAnomalieFraiche:
    def _quand(self, h_ago: float) -> str:
        return (NOW - timedelta(hours=h_ago)).isoformat()

    def test_un_cycle_recent_alerte(self):
        assert run_rapport._anomalies_fraiches(
            [ROUTINE, ANOMALIE], self._quand(0.5), NOW) == [ANOMALIE]

    def test_un_vieux_cycle_ne_realerte_pas_douze_fois(self):
        assert run_rapport._anomalies_fraiches(
            [ANOMALIE], self._quand(run_rapport.REPORT_WINDOW_H + 0.1), NOW) == []

    def test_sans_date_lisible_on_se_tait(self):
        assert run_rapport._anomalies_fraiches([ANOMALIE], None, NOW) == []
        assert run_rapport._anomalies_fraiches([ANOMALIE], "pas une date", NOW) == []

    def test_la_routine_ne_passe_jamais_meme_fraiche(self):
        assert run_rapport._anomalies_fraiches([ROUTINE], self._quand(0.1), NOW) == []


class TestMessageDuMatin:
    def test_avant_le_premier_creneau_ce_n_est_pas_le_jour(self):
        # Un dispatch manuel de 02:00 sert le dernier créneau d'HIER : ce
        # n'est pas le rendez-vous du matin.
        _minute, heures = standard_slots()
        jour = datetime(2026, 9, 18, tzinfo=timezone.utc)
        assert not run_engine._creneau_standard_du_jour(jour.replace(hour=2, minute=0))
        assert run_engine._creneau_standard_du_jour(jour.replace(hour=heures[0], minute=30))

    def test_un_scan_en_retard_sert_encore_le_creneau_du_matin(self):
        # Règle dure n°12 : on suit le CRÉNEAU DÛ, pas l'heure du run. Un
        # 06:03 rattrapé par le chien de garde porte le message.
        _minute, heures = standard_slots()
        tard = datetime(2026, 9, 18, heures[1] - 1, 40, tzinfo=timezone.utc)
        assert run_engine._creneau_standard_du_jour(tard)

    def test_la_dedup_couvre_toute_la_journee_mais_pas_le_lendemain(self):
        # Sinon le scan du soir répéterait le message du matin, ou le
        # lendemain se tairait. Bornes DÉRIVÉES du cron (règle n°6).
        _minute, heures = standard_slots()
        assert heures[-1] - heures[0] < run_engine._learning_ttl_h() < 24

    def test_reprice_ne_dit_jamais_le_learning(self, monkeypatch):
        envoyes = _capture(monkeypatch)
        monkeypatch.setattr(run_engine, "REPRICE", True)
        monkeypatch.setattr(run_engine, "_load_learning_summary", lambda _sb: [ROUTINE])
        run_engine._message_learning_du_jour(_FauxSB(), _matin())
        assert envoyes == []

    def test_le_scan_du_matin_envoie_le_resume_complet(self, monkeypatch):
        envoyes = _capture(monkeypatch)
        monkeypatch.setattr(run_engine, "REPRICE", False)
        monkeypatch.setattr(run_engine, "_load_learning_summary",
                            lambda _sb: [ROUTINE, ANOMALIE])
        run_engine._message_learning_du_jour(_FauxSB(), _matin())
        assert len(envoyes) == 1
        assert ROUTINE in envoyes[0] and ANOMALIE in envoyes[0]

    def test_un_seul_message_par_jour_meme_avec_huit_scans(self, monkeypatch):
        # Le rendez-vous est dédupliqué en base, pas par l'heure : si le scan
        # du matin meurt sur zéro match (sortie anticipée de run()), c'est le
        # premier scan suivant qui parle — et lui seul.
        envoyes = _capture(monkeypatch)
        monkeypatch.setattr(run_engine, "REPRICE", False)
        monkeypatch.setattr(run_engine, "_load_learning_summary", lambda _sb: [ROUTINE])
        _minute, heures = standard_slots()
        sb = _FauxSB()
        for h in heures:
            run_engine._message_learning_du_jour(
                sb, datetime(2026, 9, 18, h, 30, tzinfo=timezone.utc))
        assert len(envoyes) == 1

    def test_une_sortie_anticipee_du_matin_est_rattrapee(self, monkeypatch):
        envoyes = _capture(monkeypatch)
        monkeypatch.setattr(run_engine, "REPRICE", False)
        monkeypatch.setattr(run_engine, "_load_learning_summary", lambda _sb: [ROUTINE])
        _minute, heures = standard_slots()
        # Le 06:03 n'appelle jamais (mort avant) ; le 09:03 parle.
        run_engine._message_learning_du_jour(
            _FauxSB(), datetime(2026, 9, 18, heures[1], 30, tzinfo=timezone.utc))
        assert len(envoyes) == 1

    def test_un_resume_vide_ne_fait_pas_de_message(self, monkeypatch):
        envoyes = _capture(monkeypatch)
        monkeypatch.setattr(run_engine, "REPRICE", False)
        monkeypatch.setattr(run_engine, "_load_learning_summary", lambda _sb: [])
        run_engine._message_learning_du_jour(_FauxSB(), _matin())
        assert envoyes == []

    def test_une_base_illisible_ne_tue_pas_le_scan(self, monkeypatch):
        def _boom(_sb):
            raise RuntimeError("Supabase KO")

        envoyes = _capture(monkeypatch)
        monkeypatch.setattr(run_engine, "REPRICE", False)
        monkeypatch.setattr(run_engine, "_load_learning_summary", _boom)
        run_engine._message_learning_du_jour(_FauxSB(), _matin())
        assert envoyes == []


class _FauxSB:
    """Une base qui n'a encore rien horodaté : `_alert_once` laisse passer le
    premier message, puis se souvient du sien."""

    def __init__(self):
        self.stamps: dict[str, str] = {}


def _capture(monkeypatch) -> list:
    """Telegram et l'horodatage meta de `_alert_once`, en mémoire."""
    envoyes: list = []
    monkeypatch.setattr(run_engine, "_telegram", envoyes.append)
    monkeypatch.setattr(run_engine, "_meta_stamp",
                        lambda sb, k, v: sb.stamps.__setitem__(k, v) if sb else None)
    monkeypatch.setattr(run_engine, "_meta_stamp_age_h",
                        lambda sb, k: 0.5 if sb and k in sb.stamps else None)
    return envoyes


def _matin() -> datetime:
    _minute, heures = standard_slots()
    return datetime(2026, 9, 18, heures[0], 30, tzinfo=timezone.utc)


def _sig() -> dict:
    return {
        "match": "Toulouse vs Lille", "sport": "soccer", "market_key": "h2h",
        "selection_name": "Lille", "xbet_odd": 1.56, "sharp_prob": 0.66,
        "edge_pct": 3.9, "risk_flag": "VALUE",
        "created_at": (NOW - timedelta(hours=1)).isoformat(),
        "match_time": (NOW + timedelta(hours=3)).isoformat(),
    }
