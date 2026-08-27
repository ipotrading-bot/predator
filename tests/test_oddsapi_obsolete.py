"""
tests/test_oddsapi_obsolete.py — OddsAPI déclaré OBSOLÈTE (2026-08-26).

Décision opérateur : Predator ne s'appuie plus sur une source de cotes
PAYANTE. `run_engine.ODDS_API_ENABLED` vaut 0 par défaut ; le scan part
directement sur les sources gratuites (api-sports, odds-api.io, titan007,
Matchbook, harvest soft).

Ce que ces tests verrouillent, et pourquoi chacun :

1. Aucun appel à `fetch_odds` — l'invariant du mode, sur le modèle des
   sentinelles de `tests/test_reprice_mode.py`.
2. Aucune alerte de pool. Un pool mort n'est plus une panne, c'est l'état
   nominal : sans cette garde, Telegram recevrait « rotation requise » à
   chaque scan, pour toujours.
3. Le message « 0 matchs » n'accuse plus une clé — il enverrait chercher un
   secret dont le moteur n'a plus besoin.
4. GOLDEN_HOUR ne sort plus prématurément. Sa sortie anticipée supposait un
   Tier 1 vivant ; sans OddsAPI `matches` est TOUJOURS vide à cet endroit, et
   la garde faisait de golden_hour.yml un no-op horaire permanent (déjà
   constaté en prod avant la décision, run 32965494280).
5. `ODDS_API=1` rallume tout — l'obsolescence est une décision, pas une
   amputation.
"""
import pytest

import run_engine as eng
from tests.test_reprice_mode import FakeSB


def _sentinel(name):
    def _boom(*_a, **_k):
        raise AssertionError(f"{name} appelé alors qu'OddsAPI est obsolète")
    return _boom


@pytest.fixture
def scan_env(monkeypatch):
    """run() câblé en scan normal, OddsAPI obsolète, sources gratuites vides."""
    sb = FakeSB()
    telegrams = []

    monkeypatch.setattr(eng, "ODDS_API_ENABLED", False)
    monkeypatch.setattr(eng, "REPRICE", False)
    monkeypatch.setattr(eng, "GUERRILLA", False)
    monkeypatch.setattr(eng, "GOLDEN_HOUR", False)
    monkeypatch.setattr(eng, "DEEP_SCAN", False)
    monkeypatch.delenv("BETFAIR_APP_KEY", raising=False)

    # Contrat D3 : rend le budget armé (journalisé par run()).
    monkeypatch.setattr(eng, "_arm_global_timeout", lambda mode=None: 900)
    monkeypatch.setattr(eng, "get_db", lambda write=True: sb)
    monkeypatch.setattr(eng, "_purge_old_signals", lambda _sb: None)
    monkeypatch.setattr(eng, "_load_thresholds", lambda _sb: {})
    monkeypatch.setattr(eng, "_load_segment_thresholds", lambda _sb: {})
    monkeypatch.setattr(eng, "_load_edge_ceilings", lambda _sb: {})
    monkeypatch.setattr(eng, "_load_odds_ceilings", lambda _sb: {})
    monkeypatch.setattr(eng, "_load_sport_ranking", lambda _sb: [])
    monkeypatch.setattr(eng.time, "sleep", lambda _s: None)
    monkeypatch.setattr(eng._risk_manager, "check_circuit_breaker", lambda _sb: False)
    monkeypatch.setattr(eng, "_suggest_systems_by_window",
                        lambda _sigs, _log, _sb=None: [])
    monkeypatch.setattr(eng, "_telegram", lambda t: telegrams.append(t))

    # Sources payantes / alertes de pool : toute sollicitation fait ÉCHOUER.
    for fn in ("fetch_odds", "_alert_oddsapi_pool_levels",
               "_alert_oddsapi_pool_if_dead", "_build_spend_policy",
               "capture_from_scan"):
        monkeypatch.setattr(eng, fn, _sentinel(fn))

    # Sources gratuites : présentes, mais sans résultat (on teste le chemin,
    # pas l'émission — celle-ci est couverte ailleurs).
    monkeypatch.setattr(eng, "_harvest_recently_empty", lambda _sb: None)
    monkeypatch.setattr(eng, "_note_harvest_result", lambda _sb, _m: None)
    monkeypatch.setattr(eng, "fetch_matches", lambda: [])
    monkeypatch.setattr(eng, "_api_sports_all", lambda **_k: [])
    monkeypatch.setattr(eng, "_odds_api_io_all", lambda **_k: [])
    monkeypatch.setattr(eng, "_titan007_fetch", lambda **_k: [])
    monkeypatch.setattr(eng, "fetch_matchbook_prices", lambda **_k: {})
    monkeypatch.setattr(eng, "fetch_betfair_prices", lambda **_k: {})

    return sb, telegrams


class TestOddsApiObsolete:

    def test_le_defaut_du_module_est_obsolete(self):
        """Personne n'a à poser une variable pour être dans le nouveau monde.

        Vérifié sur la SOURCE et non sur la valeur courante : l'environnement
        du runner qui exécute les tests pourrait porter ODDS_API=1 et rendre
        une assertion sur `eng.ODDS_API_ENABLED` verte pour la mauvaise
        raison. Ce qui doit être verrouillé, c'est le DÉFAUT.
        """
        import inspect
        import re

        src = inspect.getsource(eng)
        m = re.search(r'ODDS_API_ENABLED\s*=\s*os\.environ\.get\(\s*"ODDS_API"\s*,\s*"(\d)"\s*\)',
                      src)
        assert m, "ODDS_API_ENABLED ne se lit plus dans l'environnement"
        assert m.group(1) == "0", (
            "OddsAPI est obsolète : le défaut doit être 0. Le rallumer par "
            "défaut remettrait une source PAYANTE sur le chemin de chaque scan.")

    def test_aucun_appel_ni_alerte_oddsapi(self, scan_env):
        """Les sentinelles portent l'assertion : fetch_odds, les deux alertes
        de pool et la politique de dépense ne doivent jamais être atteints."""
        eng.run()

    def test_le_message_zero_match_n_accuse_pas_une_cle(self, scan_env):
        _sb, telegrams = scan_env
        eng.run()
        msg = "\n".join(telegrams)
        assert "0 matchs" in msg, telegrams
        assert "rotate_odds_key" not in msg
        assert "clé(s) OddsAPI" not in msg
        assert "obsolète" in msg

    def test_golden_hour_ne_sort_plus_prematurement(self, scan_env, monkeypatch):
        """Sans OddsAPI, `matches` est toujours vide à la sortie anticipée.
        La laisser active rendait golden_hour.yml muet une fois par heure."""
        monkeypatch.setattr(eng, "GOLDEN_HOUR", True)
        vus = []
        monkeypatch.setattr(eng, "fetch_matches", lambda: vus.append("tier2") or [])
        eng.run()
        assert vus == ["tier2"], "le Tier 2 n'a pas été atteint en GOLDEN_HOUR"

    def test_odds_api_1_rallume_le_tier_1(self, scan_env, monkeypatch):
        """L'obsolescence est un interrupteur, pas une amputation."""
        monkeypatch.setattr(eng, "ODDS_API_ENABLED", True)
        appels = []
        monkeypatch.setattr(eng, "fetch_odds",
                            lambda **_k: appels.append("tier1") or [])
        monkeypatch.setattr(eng, "_build_spend_policy", lambda _sb, _now: None)
        monkeypatch.setattr(eng, "_alert_oddsapi_pool_levels", lambda _sb: None)
        monkeypatch.setattr(eng, "_alert_oddsapi_pool_if_dead", lambda _sb: None)
        eng.run()
        assert appels == ["tier1"]
