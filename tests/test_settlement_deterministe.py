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

Or `core/api_sports.fetch_sport` téléchargeait DÉJÀ la réponse qui porte ces
scores (`/fixtures?date=`) et jetait les matchs commencés.
"""
from datetime import datetime, timedelta, timezone

import pytest

from core import settlement


@pytest.fixture(autouse=True)
def _cache_neuf():
    settlement.reset_cache()
    yield
    settlement.reset_cache()


def _fixture(home, away, hs, as_, fid=1):
    return {"id": fid, "home": home, "away": away,
            "home_score": hs, "away_score": as_, "when": None}


class TestScoreSansIA:
    def test_le_score_vient_dapi_sports_sans_aucun_appel_ia(self, monkeypatch):
        """Le chemin nominal ne doit toucher NI Groq NI Tavily."""
        monkeypatch.setattr(settlement, "fetch_results",
                            lambda jour, sport: [_fixture("Moss FK", "Stabaek", 2, 1)])
        def _interdit(*a, **k):
            raise AssertionError("la recherche web ne doit pas être appelée")
        monkeypatch.setattr(settlement, "ai_search_complete", _interdit)
        monkeypatch.setattr(settlement, "ai_available", lambda: True)

        r = settlement.fetch_match_result("Moss FK vs Stabaek", "soccer", "2026-08-25")
        assert r == {"home_score": 2, "away_score": 1, "completed": True,
                     "source": "api_sports"}

    def test_une_journee_ne_coute_quune_requete(self, monkeypatch):
        """Un audit règle des dizaines de matchs du même jour. Sans cache, le
        budget api-sports (100/jour) partirait en une seule passe."""
        appels = []
        monkeypatch.setattr(settlement, "fetch_results",
                            lambda jour, sport: appels.append((jour, sport)) or
                            [_fixture("A FC", "B FC", 1, 0), _fixture("C FC", "D FC", 3, 3)])
        monkeypatch.setattr(settlement, "ai_available", lambda: False)
        for m in ("A FC vs B FC", "C FC vs D FC"):
            assert settlement.fetch_match_result(m, "soccer", "2026-08-25")
        assert len(appels) == 1, f"{len(appels)} requêtes pour une seule journée"

    def test_appariement_flou_des_noms(self, monkeypatch):
        """Les fournisseurs ne nomment pas les équipes pareil — c'est le piège
        qui tenait déjà l'enrichissement d'exchange à zéro."""
        monkeypatch.setattr(settlement, "fetch_results",
                            lambda jour, sport: [_fixture("Deportivo Macara", "Delfin SC", 0, 2)])
        monkeypatch.setattr(settlement, "ai_available", lambda: False)
        r = settlement.fetch_match_result("CSD Macara vs Delfin SC", "soccer", "2026-08-25")
        assert r["away_score"] == 2

    def test_deux_candidats_font_REFUSER_pas_deviner(self, monkeypatch):
        """Régler le mauvais match écrirait un WIN/LOSS faux et DÉFINITIF dans
        le ledger. Le refus est le comportement correct."""
        monkeypatch.setattr(settlement, "fetch_results", lambda jour, sport: [
            _fixture("Racing Club", "Boca Juniors", 1, 0, 1),
            _fixture("Racing Club II", "Boca Juniors II", 0, 4, 2)])
        monkeypatch.setattr(settlement, "ai_available", lambda: False)
        assert settlement.fetch_match_result("Racing Club vs Boca Juniors",
                                             "soccer", "2026-08-25") is None

    def test_un_match_tardif_est_cherche_le_lendemain_utc(self, monkeypatch):
        """Un coup d'envoi à 23h30 UTC tombe dans la journée suivante côté
        calendrier : ne chercher que `match_date` en raterait la moitié."""
        monkeypatch.setattr(settlement, "fetch_results", lambda jour, sport:
                            [_fixture("Palmeiras", "Flamengo", 2, 2)] if jour == "2026-08-26" else [])
        monkeypatch.setattr(settlement, "ai_available", lambda: False)
        assert settlement.fetch_match_result("Palmeiras vs Flamengo", "soccer", "2026-08-25")

    def test_la_recherche_web_reste_le_repli(self, monkeypatch):
        """api-sports ne couvre pas tout : le chemin IA ne disparaît pas."""
        monkeypatch.setattr(settlement, "fetch_results", lambda jour, sport: [])
        monkeypatch.setattr(settlement, "ai_available", lambda: True)
        monkeypatch.setattr(settlement, "ai_search_complete",
                            lambda *a, **k: '{"completed":true,"home_score":4,"away_score":0}')
        r = settlement.fetch_match_result("Obscure FC vs Inconnu SC", "mma", "2026-08-25")
        assert r["home_score"] == 4 and "source" not in r

    def test_aucune_source_ne_fait_pas_planter(self, monkeypatch):
        monkeypatch.setattr(settlement, "fetch_results", lambda jour, sport: [])
        monkeypatch.setattr(settlement, "ai_available", lambda: False)
        assert settlement.fetch_match_result("A vs B", "soccer", "2026-08-25") is None

    def test_un_nom_sans_separateur_est_ignore_sans_erreur(self, monkeypatch):
        monkeypatch.setattr(settlement, "ai_available", lambda: False)
        assert settlement.result_from_api_sports("MatchSansVs", "soccer", "2026-08-25") is None
        assert settlement.result_from_api_sports("A vs B", "soccer", "") is None


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
