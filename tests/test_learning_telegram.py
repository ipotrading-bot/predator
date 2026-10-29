"""
tests/test_learning_telegram.py — où part la couche d'apprentissage sur
Telegram, depuis le 2026-09-18.

Demande opérateur : « je ne veux pas tout le temps ce message ; message
learning une fois par jour, avec le message scan de 6 h, par contre signaler
erreurs et anomalies dès que ça survient. » Le pavé « 🧠 Learning (dernier
cycle) » partait en pied de CHAQUE digest de 2 h — douze fois par jour, cinq
lignes à chaque fois, et les deux qui signalaient une erreur de données s'y
noyaient.

Depuis le 2026-10-29 (décision opérateur : Telegram = paris, résultats,
bilan de la veille), NI le résumé ni les anomalies ne partent plus sur
Telegram : ils restent dans meta.learning_summary. Reste gardé ici : le
marqueur d'anomalie posé À LA SOURCE, et le silence des sports retirés.
"""
from datetime import datetime, timezone

from core.learning_layer import (ANOMALIE_MARQUEUR, _edge_band_diagnostic,
                                 lignes_anomalies)

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


class TestSportHorsPerimetre:
    """Le 2026-09-20, le digest criait toutes les 2 h une anomalie de bande
    d'edge sur le BASEBALL — sorti du scan payant le 2026-09-17 (décision
    opérateur, core/odds_api.LIGUES_RETIREES). Un diagnostic sur un sport
    qu'on n'achète plus ne peut plus rien corriger : la mesure continue, la
    parole s'arrête."""

    def test_le_perimetre_exclut_les_ligues_retirees_et_garde_le_tennis(self):
        from core.odds_api import LIGUES_RETIREES, SPORT_KEYS, sports_au_perimetre

        perimetre = sports_au_perimetre()
        # Dérivé, jamais écrit à la main (règle n°6).
        assert set(SPORT_KEYS.values()) <= perimetre
        assert "tennis" in perimetre                 # clés dynamiques par tournoi
        retires = {k.split("_")[0] for k in LIGUES_RETIREES}
        assert "baseball" in retires and "baseball" not in perimetre

    def test_le_resume_ecrit_ne_parle_pas_d_un_sport_retire(self):
        # Bout en bout : un baseball assez fourni pour bouger son seuil ne
        # met plus une ligne dans le résumé que l'opérateur reçoit.
        import json

        from core.learning_layer import _MIN_SAMPLES, compute_and_save
        from test_learning_layer import _FakeSupabase, _row

        rows = [_row("LOSS") for _ in range(_MIN_SAMPLES)]
        for cible, muet in (("baseball", True), ("soccer", False)):
            sb = _FakeSupabase({cible: rows})
            compute_and_save(sb)
            resumes = [w for w in sb.meta_writes if w["key"] == "learning_summary"]
            lignes = json.loads(resumes[0]["value"]) if resumes else []
            assert any(cible in l for l in lignes) is not muet, (cible, lignes)
            # La MESURE continue dans les deux cas (règle n°9) : le seuil du
            # sport retiré est toujours calculé et persisté.
            assert any(w["key"] == f"threshold_{cible}" for w in sb.meta_writes)

    def test_un_verdict_de_sport_retire_est_persiste_mais_jamais_annonce(self):
        from core.learning_layer import _save_sport_verdicts

        ecrits: list = []

        class _SB:
            def table(self, _name):
                return self

            def upsert(self, payload, **_k):
                ecrits.append(payload)
                return self

            def execute(self):
                return type("R", (), {"data": None})()

        # Perte PROUVÉE des deux côtés (borne haute sous le point mort) :
        # sans le filtre, les deux sports proposeraient leur retrait.
        perdant = {"n": 40, "hit_rate": 0.25, "roi": -0.45, "pnl_flat": -12.0,
                   "wilson_lower": 0.14, "wilson_upper": 0.40, "p_breakeven": 0.625}
        stats = {"baseball": dict(perdant), "soccer": dict(perdant)}
        lignes = _save_sport_verdicts(_SB(), stats, {}, "2026-09-20T12:00:00+00:00",
                                      perimetre=frozenset({"soccer"}))
        # Les DEUX verdicts sont en base — c'est par là qu'un sport retiré
        # redevient un jour éligible (rapport hebdo, dashboard).
        assert {w["key"] for w in ecrits} == {"sport_verdict_baseball", "sport_verdict_soccer"}
        # Une seule PAROLE : celle du sport encore acheté.
        assert lignes and all("baseball" not in l for l in lignes)


class TestResumeInchange:
    """L'audit tourne toutes les 3 h et réécrivait le même résumé à chaque
    passage : sa date restait éternellement fraîche et le digest, qui n'a que
    cette fraîcheur pour mémoire, répétait la même anomalie toutes les 2 h."""

    def _sb(self, existant: list[str]):
        import json

        class _SB:
            def __init__(self):
                self.writes: list = []

            def table(self, _name):
                return self

            def select(self, *_a, **_k):
                return self

            def eq(self, *_a, **_k):
                return self

            def limit(self, *_a, **_k):
                return self

            def like(self, *_a, **_k):
                return self

            def upsert(self, payload, **_k):
                self.writes.append(payload)
                return self

            def execute(self):
                return type("R", (), {"data": [{"value": json.dumps(existant)}]})()

        return _SB()

    def test_un_resume_identique_n_est_pas_reecrit(self, monkeypatch):
        import core.learning_layer as ll

        sb = self._sb([ANOMALIE])
        monkeypatch.setattr(ll, "load_learning_summary", lambda _sb: [ANOMALIE])
        ll._persister_resume(sb, [ANOMALIE], "2026-09-20T12:00:00+00:00")
        assert sb.writes == []

    def test_un_resume_qui_change_repart_avec_sa_date(self, monkeypatch):
        import core.learning_layer as ll

        sb = self._sb([ANOMALIE])
        monkeypatch.setattr(ll, "load_learning_summary", lambda _sb: [ANOMALIE])
        ll._persister_resume(sb, [ANOMALIE, ROUTINE], "2026-09-20T12:00:00+00:00")
        assert len(sb.writes) == 1
        assert sb.writes[0]["updated_at"] == "2026-09-20T12:00:00+00:00"
