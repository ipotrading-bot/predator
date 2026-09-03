"""
tests/test_rythme_des_sources.py — le verrou transverse du 2026-08-27.

CE QUI A CASSÉ
--------------
Quatre sources, la même panne, découverte quatre fois de suite dans la même
soirée : un budget journalier partait PREMIER ARRIVÉ, PREMIER SERVI. Les
crons du matin le raflaient, et les scans du soir — quand le slate européen
entre dans la zone jouable 2-24 h — repartaient sans la source.

    api-sports    budget de SCAN 64/64 dès 19:20  → slate sharp 42 → 25
    odds-api.io   « budget journalier atteint (400/400) » à 20:05
    Tavily        plan MENSUEL épuisé (compteur jamais partagé entre runs)
    Groq          TPD des deux organisations à sec dès 18:10

Les deux étages de la recherche de prix Pinnacle mouraient donc ensemble,
chaque soir : « Pinnacle/Search: 0/25 prices received », 141 refus « Échec
prix Sharp » sur 5 runs — le premier motif de rejet du pipeline, très loin
devant toutes les gardes d'edge réunies (0 refus PLAFOND, 0 SUSPECT).

POURQUOI CE TEST EXISTE
-----------------------
Corriger les quatre ne protège pas la CINQUIÈME. La règle dure n°6 de
CLAUDE.md dit de ne jamais tenir à la main une liste qui existe ailleurs :
ce test DÉRIVE la liste des sources budgétées et exige que chacune consulte
`daily_quota.paced_allowance`. Une source ajoutée sans rythme fait échouer la
suite, avec le nom du fichier fautif.
"""
import inspect
import re

import pytest

from core import (daily_quota, odds500,
                  odds_api_io, prediction_markets, score_sources, sevenm,
                  titan007)

# Sources qui dépensent un budget journalier ET qui alimentent un scan.
# core/ai_search.py en est SORTI le 2026-09-02 : ses budgets Tavily et
# compound-mini ont été supprimés avec la recherche web — il ne dépense plus
# aucun budget de source, le routeur IA porte les siens.
SOURCES_BUDGETEES = {
    "core/odds_api_io.py":  odds_api_io,
    "core/titan007.py":     titan007,
}

# EXEMPTÉES, chacune pour une raison écrite. Une exemption sans motif est une
# régression déguisée : si l'une de ces sources redevient émettrice, elle
# rejoint la table du dessus.
EXEMPTEES = {
    # Filtrée par IP en amont depuis les runners GitHub (403 de 500.com sur
    # les IP de sortie US). Rend 0 match : rien à étaler.
    "core/odds500.py":            odds500,
    # Source de NOMS, pas de cotes, et seulement appelée quand odds500 rend
    # des fixtures à résoudre — donc jamais tant qu'odds500 est bloquée.
    "core/sevenm.py":             sevenm,
    # Rôle CONSENSUS : mesure et n'émet jamais. Mesuré le 2026-08-27,
    # 74 marchés cotés et 0 apparié au slate.
    "core/prediction_markets.py": prediction_markets,
    # Sources de SCORES du settlement (2026-09-02) : budgets journaliers
    # partagés (daily_quota) mais SANS rythme horaire, et c'est voulu —
    # étaler le settlement était une faute (incident du 2026-08-28 : un match
    # déjà joué ne se règle pas mieux plus tard, il sort en `expired`).
    "core/score_sources.py":      score_sources,
}


def _budgets_declares(module) -> list[str]:
    """Constantes de budget journalier déclarées par le module."""
    return [n for n in dir(module)
            if n.endswith("DAILY_BUDGET") or n.endswith("DAILY")]


class TestToutBudgetJournalierEstEtale:

    @pytest.mark.parametrize("chemin", sorted(SOURCES_BUDGETEES))
    def test_la_source_consulte_le_rythme_commun(self, chemin):
        module = SOURCES_BUDGETEES[chemin]
        src = inspect.getsource(module)
        assert "paced_allowance" in src, (
            f"{chemin} dépense un budget journalier sans consulter "
            f"daily_quota.paced_allowance : son budget repartira premier "
            f"arrivé, premier servi et les scans du soir le trouveront à sec."
        )

    @pytest.mark.parametrize("chemin", sorted(SOURCES_BUDGETEES))
    def test_la_source_declare_bien_un_budget(self, chemin):
        """Garde-fou du test lui-même : si une source cesse d'avoir un budget,
        c'est sa ligne dans SOURCES_BUDGETEES qu'il faut retirer, pas le
        rythme qu'il faut oublier."""
        module = SOURCES_BUDGETEES[chemin]
        assert _budgets_declares(module), f"{chemin} ne déclare plus de budget"

    def test_aucune_source_n_est_a_la_fois_budgetee_et_exemptee(self):
        assert not (set(SOURCES_BUDGETEES) & set(EXEMPTEES))

    def test_le_rythme_vit_a_UN_seul_endroit(self):
        """Trois copies d'une même règle finissent toujours par diverger —
        c'est la panne la plus fréquente de ce dépôt (règle dure n°6)."""
        formule = re.compile(r"total_seconds\(\)\s*/\s*86400")
        porteurs = [c for c, m in {**SOURCES_BUDGETEES, **EXEMPTEES}.items()
                    if formule.search(inspect.getsource(m))]
        assert porteurs == [], (
            f"le calcul du rythme a été recopié dans {porteurs} au lieu "
            f"d'appeler daily_quota.paced_allowance")
        assert formule.search(inspect.getsource(daily_quota)), \
            "la formule a disparu de son unique domicile"


class TestContratDuRythme:
    """Ce que `paced_allowance` doit garantir, quel que soit l'appelant."""

    def test_il_reste_toujours_de_quoi_scanner_le_soir(self):
        """La garantie qui manquait : à 18:00, au moins un quart du budget
        est encore fermé, donc réservé aux runs plus tardifs."""
        from datetime import datetime, timezone
        budget = 100
        ouvert = daily_quota.paced_allowance(
            budget, 5, datetime(2026, 8, 27, 18, tzinfo=timezone.utc))
        assert budget - ouvert >= 24

    def test_le_budget_entier_est_ouvert_en_fin_de_journee(self):
        """Étaler ne doit pas GASPILLER : ce qui n'a pas servi le matin doit
        pouvoir servir avant minuit."""
        from datetime import datetime, timezone
        assert daily_quota.paced_allowance(
            100, 5, datetime(2026, 8, 27, 23, 58, tzinfo=timezone.utc)) >= 99
