"""
tests/test_timeout_par_mode.py — PHASE D3.

Une valeur unique de 540 s servait tous les modes de scan. Mesuré sur
l'historique des runs avant la fusion dans scan.yml (golden, deep et
guerrilla ont été supprimés le 2026-09-03 ; leurs mesures restent, elles
justifient la borne standard) :

    golden      médiane  58 s, max 454 s   → 540 s = 9× la médiane
    standard    médiane 389 s, max 591 s   → frôle le plafond
    deep        2 runs, 569 s tous deux    → 1 échec sur 2
    guerrilla   médiane 418 s, max 571 s   → 1 échec sur 7

L'échec Guerrilla 32990495899 est nommé dans son log : « TIMEOUT: Engine
exceeded 540 seconds ». Un moteur au travail, coupé net — AVANT la
persistance, qui est en toute fin de run() : un dépassement ne perd pas la
fin du scan, il perd TOUT le scan (zéro signal là où trois clés mortes en
émettaient douze).

Ce que ces tests gardent :

  · chaque mode a un budget, et l'ordre des budgets suit l'ordre des durées
    mesurées — pas de valeur unique de retour ;
  · la résolution du mode suit la MÊME priorité que run() (REPRICE prime),
    par une fonction partagée et non une seconde chaîne de if recopiée ;
  · le budget armé est celui du mode, il est RENDU à l'appelant et cité par
    le message de timeout — un filet dont personne ne connaît la taille ne
    s'explique pas quand il se déclenche ;
  · aucun budget ne dépasse ce que le `timeout-minutes` du job laisse après
    lui (passe closing line + REPRICE) : sinon le tueur devient GitHub, qui
    ne laisse ni log « TIMEOUT » ni Traceback.
"""
import signal as signal_module

import pytest

import run_engine
from core.constants import GLOBAL_TIMEOUT, SCAN_TIMEOUTS


@pytest.fixture
def desarme():
    """Rend la main avec l'alarme ÉTEINTE, même si un test l'a armée."""
    yield
    try:
        signal_module.alarm(0)
    except (AttributeError, ValueError):
        pass


def _poser_mode(monkeypatch, **flags):
    monkeypatch.setattr(run_engine, "REPRICE", flags.get("REPRICE", False))


class TestLaTableDesBudgets:
    def test_chaque_mode_du_pipeline_a_son_budget(self):
        """Les modes de scripts/ci_scan_mode.py::MODES, exactement (deux
        depuis le 2026-09-03). Un mode ajouté sans budget retomberait sur le
        repli en silence ; un budget orphelin serait un mode fantôme."""
        from scripts.ci_scan_mode import MODES
        assert set(SCAN_TIMEOUTS) == set(MODES)

    def test_lordre_des_budgets_suit_lordre_des_durees_mesurees(self):
        """reprice (12-15 s) < standard (méd. 389 s, max 591 s) — et le
        standard garde une marge au-dessus de son maximum mesuré."""
        t = SCAN_TIMEOUTS
        assert t["reprice"] < t["standard"]
        assert t["standard"] > 600

    def test_aucun_budget_ne_depasse_la_part_du_job(self):
        """scan.yml donne 30 min au JOB entier : setup, scan, passe closing
        line, REPRICE. Si le budget du moteur mange tout, le tueur devient
        `timeout-minutes` — et lui ne laisse ni log TIMEOUT ni Traceback,
        c'est-à-dire exactement l'illisibilité qu'on répare."""
        for mode, budget in SCAN_TIMEOUTS.items():
            assert budget <= 25 * 60, f"{mode} ne laisse plus de marge au job"

    def test_le_repli_est_le_budget_standard(self):
        """Un mode inconnu n'a aucune raison d'être plus généreux que le cas
        ordinaire. GLOBAL_TIMEOUT n'a plus qu'un seul lecteur (run_engine, en
        repli) mais garde son nom : c'est la constante historique, et un
        renommage cosmétique n'est pas dans le périmètre."""
        assert GLOBAL_TIMEOUT == SCAN_TIMEOUTS["standard"]


class TestLaResolutionDuMode:
    def test_reprice_donne_reprice(self, monkeypatch):
        _poser_mode(monkeypatch, REPRICE=True)
        assert run_engine._mode_courant() == "reprice"

    def test_aucun_drapeau_donne_standard(self, monkeypatch):
        _poser_mode(monkeypatch)
        assert run_engine._mode_courant() == "standard"

    def test_les_anciens_drapeaux_nexistent_plus(self):
        """golden, deep et guerrilla sont partis le 2026-09-03 : un module qui
        les relirait ferait revivre un mode sans cron ni budget."""
        for nom in ("GOLDEN_HOUR", "GUERRILLA", "DEEP_SCAN", "GOLDEN_SPORT_KEYS", "_QUOTA_DEEP"):
            assert not hasattr(run_engine, nom), nom


class TestLArmement:
    def test_le_budget_arme_est_celui_du_mode(self, monkeypatch, desarme):
        _poser_mode(monkeypatch, REPRICE=True)
        assert run_engine._arm_global_timeout() == SCAN_TIMEOUTS["reprice"]
        _poser_mode(monkeypatch)
        assert run_engine._arm_global_timeout() == SCAN_TIMEOUTS["standard"]

    def test_un_mode_explicite_gagne_sur_les_drapeaux(self, monkeypatch, desarme):
        _poser_mode(monkeypatch)
        assert run_engine._arm_global_timeout("reprice") == SCAN_TIMEOUTS["reprice"]

    def test_un_mode_inconnu_retombe_sur_le_repli(self, monkeypatch, desarme):
        _poser_mode(monkeypatch)
        assert run_engine._arm_global_timeout("mode_fantome") == GLOBAL_TIMEOUT

    def test_lalarme_est_reellement_posee(self, monkeypatch, desarme):
        """`signal.alarm(0)` rend le reliquat de l'alarme précédente : après
        l'armement il doit être > 0, sinon la fonction rend un budget qu'elle
        n'a pas posé."""
        _poser_mode(monkeypatch)
        run_engine._arm_global_timeout()
        assert signal_module.alarm(0) > 0

    def test_le_message_de_timeout_cite_le_budget_arme(self, monkeypatch, desarme, caplog):
        """L'opérateur qui lit « exceeded 540 seconds » sur un budget de
        900 chercherait la mauvaise constante."""
        import logging
        _poser_mode(monkeypatch)
        run_engine._arm_global_timeout()
        with caplog.at_level(logging.ERROR, logger="PREDATOR"):
            with pytest.raises(run_engine.EngineTimeout) as exc:
                run_engine._timeout_handler(signal_module.SIGALRM, None)
        assert str(SCAN_TIMEOUTS["standard"]) in str(exc.value)
        assert any(str(SCAN_TIMEOUTS["standard"]) in r.getMessage() for r in caplog.records)


class TestLeTimeoutNestPasAvalable:
    """2026-08-28 15:50:56 : « TIMEOUT: Engine exceeded 600 seconds » — puis
    le run a continué 7 min, l'exception attrapée par un `except Exception`
    de la boucle d'alias IA (et `core.net` retente sur TimeoutError comme sur
    une erreur réseau). Le tick a tenu 17 min 44 s sous le verrou d'écriture.
    Un timeout attrapable par accident n'est pas un timeout."""

    def test_le_handler_leve_une_BaseException(self):
        with pytest.raises(run_engine.EngineTimeout):
            run_engine._timeout_handler(14, None)
        assert not issubclass(run_engine.EngineTimeout, Exception)

    def test_un_except_Exception_ne_l_arrete_pas(self):
        def boucle_jamais_bloquante():
            try:
                run_engine._timeout_handler(14, None)
            except Exception:                       # le patron « jamais bloquant »
                return "avalé"
            return "passé"
        with pytest.raises(run_engine.EngineTimeout):
            boucle_jamais_bloquante()

    def test_core_net_ne_le_retente_pas_comme_une_erreur_transitoire(self):
        from core import net
        assert not issubclass(run_engine.EngineTimeout, net._TRANSIENT)
