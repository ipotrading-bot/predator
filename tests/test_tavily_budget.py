"""
tests/test_tavily_budget.py — le budget Tavily partagé entre les runs.

CE QUI A CASSÉ (2026-08-27)
---------------------------
`_tavily_used` est un global de MODULE : il repart à zéro à chaque processus.
Le plan gratuit fait 1 000 crédits par MOIS, ce dépôt lance ~40 runs par jour
et chacun s'autorisait `_TAVILY_RUN_BUDGET` = 25 crédits. Jusqu'à 1 000
crédits par JOUR contre un plan MENSUEL de 1 000 : le mois entier pouvait
partir en une journée, et « Tavily: plafond de PLAN atteint (HTTP 432) »
était l'état permanent, pas un pic.

POURQUOI ÇA COÛTAIT DES SIGNAUX
--------------------------------
Tavily est l'ÉTAGE 2 de la recherche de prix Pinnacle. L'étage 1
(groq/compound-mini) meurt sur son quota de tokens journalier vers 18:10.
Les deux morts ensemble donnent « Pinnacle/Search: 0/25 prices received » —
141 refus « Échec prix Sharp » mesurés sur 5 runs, premier motif de rejet du
pipeline, très loin devant toutes les gardes d'edge réunies (0 refus PLAFOND,
0 SUSPECT sur les mêmes runs).

C'est la panne que core/daily_quota.py existe pour empêcher. Tavily n'y avait
jamais été câblé.
"""
from datetime import datetime, timezone

import pytest

from core import ai_search, daily_quota


@pytest.fixture(autouse=True)
def _isole(monkeypatch):
    """Compteurs remis à neuf, et aucun appel réseau ne doit partir."""
    monkeypatch.setattr(ai_search, "_tavily_used", 0)
    monkeypatch.setattr(ai_search, "_tavily_plan_dead", False)
    monkeypatch.setattr(ai_search, "_priorite_settlement", False)
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")

    def _interdit(*a, **k):
        raise AssertionError("un appel réseau Tavily est parti malgré le budget")

    monkeypatch.setattr(ai_search.requests, "post", _interdit)


def _quota(monkeypatch, depense: int):
    monkeypatch.setattr(daily_quota, "spent", lambda bucket: depense)
    monkeypatch.setattr(ai_search.daily_quota, "spent", lambda bucket: depense)


class TestBudgetPartage:

    def test_un_run_ne_peut_plus_depenser_le_mois_entier(self, monkeypatch):
        """La régression exacte : 40 runs × 25 crédits contre un plan de
        1 000 PAR MOIS."""
        _quota(monkeypatch, ai_search._TAVILY_DAILY_BUDGET)
        assert ai_search.tavily_search("prix pinnacle psg lyon") == []

    def test_un_run_du_matin_ne_peut_pas_prendre_la_journee(self, monkeypatch):
        """Le budget de RUN (25) reste plus large que la part quotidienne :
        ce n'est donc pas lui qui borne un run, c'est le RYTHME. Sans cette
        garde, le premier cron du jour reprendrait tout et les scans du soir
        — quand le slate européen entre dans la zone jouable — repartiraient
        sans prix sharp, exactement comme avant."""
        _quota(monkeypatch, 0)
        # `ai_search.daily_quota` EST le module daily_quota : on capture la
        # vraie fonction avant de la remplacer, sinon le double s'appelle
        # lui-même.
        vraie = daily_quota.paced_allowance
        monkeypatch.setattr(
            daily_quota, "paced_allowance",
            lambda budget, floor, now=None: vraie(
                budget, floor, datetime(2026, 8, 27, 6, tzinfo=timezone.utc)))
        part_matinale = ai_search._tavily_budget_du_jour()
        assert 0 < part_matinale < ai_search._TAVILY_DAILY_BUDGET

    def test_le_budget_quotidien_tient_dans_le_plan_mensuel(self):
        assert ai_search._TAVILY_DAILY_BUDGET * 31 <= ai_search._TAVILY_MONTHLY_BUDGET

    def test_search_exhausted_voit_le_budget_partage(self, monkeypatch):
        """`core/audit_engine` teste ceci AVANT d'écrire un état TERMINAL :
        un None qui veut dire « je n'ai pas pu chercher » ne doit jamais être
        lu comme « l'information n'existe pas »."""
        _quota(monkeypatch, 0)
        assert ai_search.search_exhausted() is False
        _quota(monkeypatch, ai_search._TAVILY_DAILY_BUDGET)
        assert ai_search.search_exhausted() is True

    def test_credits_left_ne_promet_jamais_plus_que_le_jour(self, monkeypatch):
        _quota(monkeypatch, ai_search._TAVILY_DAILY_BUDGET - 1)
        assert ai_search.search_credits_left() <= 1


class TestReserveDuSettlement:
    """Les SCANS sont amputés, la réserve ne l'est jamais.

    Même remède que api_sports.RESULTS_RESERVE et que le cloisonnement Groq
    du 2026-08-02 : un signal dont le score n'a pas pu être cherché sort du
    ledger en `expired` et n'apprend plus rien à personne.
    """

    def test_un_scan_ne_touche_pas_la_reserve(self, monkeypatch):
        depense = ai_search._TAVILY_DAILY_BUDGET - ai_search._TAVILY_RESULTS_RESERVE
        _quota(monkeypatch, depense)
        monkeypatch.setattr(ai_search, "_priorite_settlement", False)
        assert ai_search._tavily_budget_du_jour() == 0

    def test_le_settlement_y_a_droit(self, monkeypatch):
        depense = ai_search._TAVILY_DAILY_BUDGET - ai_search._TAVILY_RESULTS_RESERVE
        _quota(monkeypatch, depense)
        monkeypatch.setattr(ai_search, "_priorite_settlement", True)
        assert ai_search._tavily_budget_du_jour() > 0

    def test_run_audit_leve_bien_le_drapeau(self):
        """Le drapeau ne sert à rien s'il n'est pas posé : run_audit.py est
        le SEUL point d'entrée du settlement."""
        source = open("run_audit.py", encoding="utf-8").read()
        assert "prioriser_settlement()" in source


class TestRythme:
    """Un compteur partagé empêche de dépasser un plan ; il ne dit rien de
    QUAND on le dépense. Les crons du matin raflaient tout."""

    def test_l_ouverture_croit_avec_la_journee(self):
        matin = daily_quota.paced_allowance(
            100, 5, datetime(2026, 8, 27, 6, tzinfo=timezone.utc))
        soir = daily_quota.paced_allowance(
            100, 5, datetime(2026, 8, 27, 20, tzinfo=timezone.utc))
        assert matin < soir

    def test_plancher_le_premier_run_du_jour_part_toujours(self):
        assert daily_quota.paced_allowance(
            100, 5, datetime(2026, 8, 27, 0, 2, tzinfo=timezone.utc)) == 5

    def test_l_ouverture_ne_depasse_jamais_le_budget(self):
        assert daily_quota.paced_allowance(
            100, 5, datetime(2026, 8, 27, 23, 59, tzinfo=timezone.utc)) <= 100

    def test_un_plancher_plus_grand_que_le_budget_ne_le_depasse_pas(self):
        """Sinon un plancher mal réglé rouvrirait tout le budget dès 00h00."""
        assert daily_quota.paced_allowance(
            3, 50, datetime(2026, 8, 27, 0, 1, tzinfo=timezone.utc)) == 3


class TestDegradation:
    """Sans Supabase (tests, sandbox, panne réseau), le compteur est muet.
    Une source de cotes ne doit jamais tomber parce que son compteur l'est."""

    def test_sans_base_la_source_reste_utilisable(self, monkeypatch):
        monkeypatch.setattr(ai_search.daily_quota, "spent", lambda bucket: 0)
        assert ai_search._tavily_budget_du_jour() > 0
        assert ai_search.search_exhausted() is False
