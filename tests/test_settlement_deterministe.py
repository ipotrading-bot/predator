"""
tests/test_settlement_deterministe.py — le score vient d'un CHAMP, plus d'un LLM.

MESURÉ LE 2026-08-26, et c'est ce qui justifie ce chantier : le taux de
résolution réelle du ledger est tombé de 65 % (23 août) à 11 % (24-26 août)
parce que les DEUX chemins de recherche web ont lâché en même temps — Tavily au
plafond de plan (HTTP 432) et le `compound-mini` de Groq en limite par minute.
Un audit a rendu « 0 settled | 0 closed | 0 expired | 52 skipped », en vert et
sans alerte, et ces 52 signaux étaient promis à la purge en `expired` — donc
exclus de `learning_layer._clv_stats`. Une panne d'API ne retardait pas
l'apprentissage : elle détruisait l'échantillon.

Les scores viennent de CHAMPS d'API ouvertes (core/score_sources : MLB
statsapi, ESPN, TheSportsDB). api-sports, premier étage de cette chaîne du
2026-08-26 au 2026-09-03, est retiré (deux comptes gratuits suspendus).
"""
from datetime import datetime, timedelta, timezone


from core import settlement


class TestScoreSansIA:
    def test_la_chaine_est_score_sources_sans_aucun_appel_ia(self, monkeypatch):
        """Le score vient d'un CHAMP (MLB statsapi, ESPN, TheSportsDB) — plus
        aucune IA (2026-09-02), plus d'api-sports (2026-09-03)."""
        monkeypatch.setattr(settlement, "fetch_score",
                            lambda *a, **k: {"completed": True, "home_score": 4,
                                        "away_score": 0, "source": "espn"})
        r = settlement.fetch_match_result("Obscure FC vs Inconnu SC", "soccer", "2026-08-25")
        assert r["home_score"] == 4 and r["source"] == "espn"

    def test_aucune_source_ne_fait_pas_planter(self, monkeypatch):
        monkeypatch.setattr(settlement, "fetch_score", lambda *a, **k: None)
        assert settlement.fetch_match_result("A vs B", "soccer", "2026-08-25") is None


class TestAuditSterile:
    """Un travail nul doit être discernable d'un travail sans objet."""

    class _SB:
        def __init__(self): self.upserts, self.deletes = [], []
        def table(self, _n): return self
        def upsert(self, row, **_k): self.upserts.append(row); return self
        def delete(self): self._d = True; return self
        def eq(self, _c, v): self.deletes.append(v); return self
        def execute(self): return type("R", (), {"data": []})()

    def test_zero_regle_alerte_et_pose_le_marqueur(self, monkeypatch):
        from core import audit_engine as ae
        envois = []
        monkeypatch.setattr(ae, "_alerte_telegram", lambda t: envois.append(t))
        sb = self._SB()
        ae._signaler_audit_sterile(sb, {"settled": 0, "skipped": 52}, 52)
        assert envois and "Audit stérile" in envois[0] and "52" in envois[0]
        assert any(u["key"] == ae.SETTLEMENT_STARVED_KEY for u in sb.upserts)

    def test_un_audit_productif_nalerte_pas(self, monkeypatch):
        from core import audit_engine as ae
        envois = []
        monkeypatch.setattr(ae, "_alerte_telegram", lambda t: envois.append(t))
        ae._signaler_audit_sterile(self._SB(), {"settled": 3, "skipped": 1}, 4)
        assert not envois

    def test_rien_a_auditer_nest_pas_une_famine(self, monkeypatch):
        from core import audit_engine as ae
        envois = []
        monkeypatch.setattr(ae, "_alerte_telegram", lambda t: envois.append(t))
        ae._signaler_audit_sterile(self._SB(), {"settled": 0}, 0)
        assert not envois


class TestPurgeNeDetruitPasLechantillon:
    """Un signal purgé part en `expired`, et `_clv_stats` exclut ces lignes."""

    class _SB:
        def __init__(self, valeur): self.valeur = valeur
        def table(self, _n): return self
        def select(self, *_a, **_k): return self
        def eq(self, *_a, **_k): return self
        def limit(self, _n): return self
        def execute(self):
            d = [{"value": self.valeur}] if self.valeur else []
            return type("R", (), {"data": d})()

    def test_famine_fraiche_repousse_la_purge(self):
        import run_engine as eng
        recent = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        assert eng._settlement_affame(self._SB(recent)) is True

    def test_famine_perimee_ne_repousse_plus(self):
        import run_engine as eng
        vieux = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
        assert eng._settlement_affame(self._SB(vieux)) is False

    def test_sans_marqueur_le_comportement_est_inchange(self):
        import run_engine as eng
        assert eng._settlement_affame(self._SB(None)) is False

    def test_une_valeur_illisible_ne_bloque_pas_la_purge(self):
        """Échouer OUVERT ici : une purge bloquée par une donnée corrompue
        ferait gonfler la table sans fin."""
        import run_engine as eng
        assert eng._settlement_affame(self._SB("pas-une-date")) is False
