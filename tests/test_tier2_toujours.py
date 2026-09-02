"""
tests/test_tier2_toujours.py — le Tier 2 tourne à CHAQUE tick (sauf REPRICE).

Incident du 2026-09-02 : la garde `if not tier1_ok` faisait sauter tout le
Tier 2 (titan007, odds-api.io, sevenm, harvest) dès qu'OddsAPI rendait UN
event. Pendant l'US Open + la trêve internationale, le Tier 1 rendait du
tennis sans edge et le foot hors-Europe — qui portait tout le volume —
n'était plus jamais scanné : 26 signaux/jour avant le rallumage OddsAPI,
4/jour après (run 33551932260). Même classe de bug que la garde sur
`matches` corrigée après le run 30768093911 (1 combat UFC masquait le foot).

Ces gardes sont des invariants de FORME du moteur : on les vérifie sur le
source, comme le fait tests/test_api_admin_auth.py sur l'AST du dashboard.
"""
import inspect
import re
from pathlib import Path

import run_engine

_SOURCE = Path(run_engine.__file__).read_text(encoding="utf-8")


class TestLeTier2NestPasGardeParLeTier1:
    def test_aucune_garde_tier1_sur_le_bloc_tier2(self):
        # La forme exacte du bug : conditionner le Tier 2 au silence du
        # Tier 1. Toute variante `not tier1_ok and …` est le même piège.
        assert not re.search(r"if\s+not\s+tier1_ok", _SOURCE), (
            "Le Tier 2 est de nouveau gardé par tier1_ok : un Tier 1 qui rend "
            "un event sans edge (tennis US Open) affame les sources qui "
            "portent le volume — voir INCIDENTS.md, run 33551932260.")

    def test_seul_reprice_saute_le_tier2(self):
        # Le bloc Tier 2 existe toujours et reste borné par REPRICE seul.
        assert re.search(r"# ── Tier 2 :.*\n(?:\s*#.*\n)*\s*if not REPRICE:",
                         _SOURCE), (
            "Le bloc Tier 2 (commentaire « ── Tier 2 : » suivi de "
            "`if not REPRICE:`) est introuvable — s'il a été restructuré, "
            "reporter cette garde sur la nouvelle forme.")


class TestLesGatesDecisifsSontVisibles:
    """Un filtre qui jette un signal potentiel doit le dire (audit 2026-09-02 :
    les gates totals/spreads étaient muets, coût en volume immesurable)."""

    def test_lowprob_est_logge_sur_totals(self):
        src = inspect.getsource(run_engine._process_totals)
        assert "LOWPROB" in src, (
            "_process_totals jette sous prob_min sans log — le gate redevient "
            "immesurable.")

    def test_lowprob_est_logge_sur_spreads(self):
        src = inspect.getsource(run_engine._process_spreads)
        assert "LOWPROB" in src, (
            "_process_spreads jette sous prob_min sans log — le gate "
            "redevient immesurable.")


class TestLaPurgeEstHonnete:
    def test_la_regle_morte_pending_nest_pas_revenue(self):
        # Rien dans le dépôt n'écrit jamais status='pending' sur un signal ;
        # la règle de purge correspondante était morte depuis sa création.
        assert '"pending"' not in _SOURCE and "'pending'" not in _SOURCE, (
            "Une règle status=pending est revenue dans run_engine — aucun "
            "code n'écrit ce statut, la règle serait morte (ou pire : un "
            "nouveau statut homonyme serait purgé par accident).")

    def test_une_destruction_est_loggee_en_info(self):
        # Les retraits de la purge ne doivent jamais dépendre de DEBUG_MODE
        # pour être visibles (« aucune réduction silencieuse »).
        assert re.search(r'log\.info\("PURGE \|', _SOURCE), (
            "La purge ne logge plus ses destructions au niveau info.")
