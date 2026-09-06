"""
tests/test_audit_priorite.py — le règlement sert les RECOMMANDÉS avant les
fantômes, et ne dépense jamais TheSportsDB pour un fantôme (2026-09-05).

Mesure qui justifie la règle : depuis l'époque A6, 137 lignes émises sur 198
étaient des fantômes (< T-2h, `is_shadow`), exclus de l'apprentissage par
construction mais réglés sur le même budget TheSportsDB (150/jour) — saturé
les 02, 03 et 04 septembre pendant que des recommandés restaient `active`.

Ce qui NE change pas : un fantôme reste réglé par les voies gratuites (ESPN,
LiveScore, MLB) — archivé, jamais jeté (règle n°9). Aucun réseau ici :
`settle_signal` est monkeypatché.
"""
from datetime import datetime, timedelta, timezone

import core.audit_engine as audit_engine

NOW = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)


def _sig(sid: int, hours_ago: float, shadow: bool = False) -> dict:
    return {"id": sid, "match": f"A{sid} vs B{sid}", "sport": "soccer",
            "market_key": "h2h", "selection_name": f"A{sid}", "xbet_odd": 1.8,
            "pinnacle_price": 1.7, "status": "active", "is_shadow": shadow,
            "match_time": (NOW - timedelta(hours=hours_ago)).isoformat()}


class TestOrdreDeService:
    def test_les_recommandes_passent_avant_les_fantomes(self):
        pending = [_sig(1, 20, shadow=True), _sig(2, 10), _sig(3, 8, shadow=True), _sig(4, 5)]
        ordre = [s["id"] for s in audit_engine.prioriser(pending)]
        assert ordre == [2, 4, 1, 3]

    def test_le_tri_est_stable_dans_chaque_groupe(self):
        """fetch_pending rend le coup d'envoi croissant : on ne le réordonne pas."""
        pending = [_sig(i, 30 - i) for i in range(6)]
        assert [s["id"] for s in audit_engine.prioriser(pending)] == list(range(6))

    def test_un_budget_court_sert_dabord_les_recommandes(self, monkeypatch):
        regles = []
        monkeypatch.setattr(audit_engine, "settle_signal",
                            lambda sb, sig, now_iso, tsdb_ok=True: regles.append(sig["id"]) or True)
        pending = audit_engine.prioriser([_sig(1, 20, shadow=True), _sig(2, 10), _sig(3, 9)])
        budget = [2]
        for sig in pending:
            audit_engine.audit_one(None, sig, budget, NOW)
        assert regles == [2, 3]          # le fantôme attend le run suivant


class TestTheSportsDBReserveAuxRecommandes:
    def _tsdb_ok_pour(self, monkeypatch, sig) -> bool | None:
        vu = {}
        monkeypatch.setattr(audit_engine, "settle_signal",
                            lambda sb, s, now_iso, tsdb_ok=True: vu.setdefault("ok", tsdb_ok) or True)
        audit_engine.audit_one(None, sig, [5], NOW)
        return vu.get("ok")

    def test_un_fantome_ne_recoit_jamais_le_repli(self, monkeypatch):
        assert self._tsdb_ok_pour(monkeypatch, _sig(1, 5, shadow=True)) is False

    def test_un_recommande_recent_le_recoit(self, monkeypatch):
        assert self._tsdb_ok_pour(monkeypatch, _sig(2, 5)) is True

    def test_un_fantome_est_tout_de_meme_regle_par_les_voies_gratuites(self, monkeypatch):
        appels = []
        monkeypatch.setattr(audit_engine, "settle_signal",
                            lambda sb, s, now_iso, tsdb_ok=True: appels.append(s["id"]) or True)
        assert audit_engine.audit_one(None, _sig(9, 5, shadow=True), [5], NOW) == "settled"
        assert appels == [9]

    def test_la_fenetre_de_retry_reste_intacte_pour_un_recommande(self):
        vieux = _sig(3, audit_engine.TSDB_RETRY_WINDOW_H + 1)
        assert audit_engine._tsdb_encore_utile(vieux, NOW) is False
        assert audit_engine._tsdb_encore_utile(_sig(4, 1), NOW) is True


class TestLeContratNeCompteQueLesRecommandes:
    """2026-09-06 : « éligible » au sens du contrat de fin = RECOMMANDÉ en
    attente. Un fantôme est réglé si une voie gratuite l'a, TheSportsDB lui
    étant refusé par construction : un fantôme seul sans score n'est pas une
    contradiction interne. Le 06/09, un fantôme chilien introuvable a peint
    deux audits en rouge et envoyé deux alertes Telegram — un faux rouge qui
    aurait masqué un vrai stérile jusqu'à son expiration.

    `run()` est joué à blanc : base, lecture des pending, règlement, relance
    et apprentissage sont tous monkeypatchés — aucun réseau."""

    @staticmethod
    def _jouer(monkeypatch, pending, statuts):
        alertes = []
        monkeypatch.setattr(audit_engine, "get_db", lambda write=True: object())
        monkeypatch.setattr(audit_engine, "fetch_pending", lambda sb: list(pending))
        monkeypatch.setattr(audit_engine, "audit_one",
                            lambda sb, sig, budget, now: statuts[sig["id"]])
        monkeypatch.setattr(audit_engine, "_relancer_expires", lambda sb: None)
        monkeypatch.setattr(audit_engine, "_learn", lambda sb: None)
        monkeypatch.setattr(audit_engine, "_effacer_marqueur_sterile", lambda sb: None)
        monkeypatch.setattr(audit_engine, "_signaler_audit_sterile",
                            lambda sb, counts, total: alertes.append(total))
        try:
            audit_engine.run()
        except SystemExit as e:
            return e.code, alertes
        return None, alertes

    def test_un_fantome_seul_sans_score_ne_rend_pas_laudit_sterile(self, monkeypatch):
        code, alertes = self._jouer(monkeypatch, [_sig(1, 24, shadow=True)], {1: "skipped"})
        assert code is None, "un fantôme introuvable n'est pas une panne du pipeline"
        assert alertes == [], "et n'envoie pas d'alerte « audit stérile »"

    def test_un_recommande_sans_score_le_rend_sterile(self, monkeypatch):
        code, alertes = self._jouer(monkeypatch, [_sig(1, 24)], {1: "skipped"})
        assert code == 1
        assert alertes == [1]

    def test_lalerte_compte_les_recommandes_pas_les_fantomes(self, monkeypatch):
        pending = [_sig(1, 24), _sig(2, 24), _sig(3, 24, shadow=True)]
        code, alertes = self._jouer(monkeypatch, pending, {1: "skipped", 2: "skipped", 3: "skipped"})
        assert code == 1
        assert alertes == [2]

    def test_un_fantome_regle_prouve_que_les_sources_repondent(self, monkeypatch):
        """Un recommandé sans score AU MILIEU de fantômes réglés n'est pas une
        panne : les sources ont répondu, ce match-là n'y est pas encore."""
        pending = [_sig(1, 24), _sig(2, 24, shadow=True)]
        code, alertes = self._jouer(monkeypatch, pending, {1: "skipped", 2: "settled"})
        assert code is None
        assert alertes == []
