"""
tests/test_tennis_discovery.py — clés OddsAPI tennis DYNAMIQUES (Phase 3).

OddsAPI ne sert pas le tennis comme un sport permanent : une clé par tournoi,
qui apparaît quelques jours avant et disparaît après. Une entrée statique dans
SPORT_KEYS serait morte onze mois sur douze. Les clés sont donc résolues à
chaque fetch_odds via le catalogue /sports — GRATUIT — filtrées sur les
Grands Chelems et Masters 1000, et fusionnées dans `keys_to_scan`.

Ce que ces tests verrouillent : le filtre (un ATP 250 ne passe pas), les
outrights exclus, la panne qui rend {} sans exception, le coupe-circuit, la
fenêtre favorable par préfixe — et surtout l'INJECTION, parce qu'une
découverte qui marche mais n'est pas branchée est la panne la plus silencieuse
de ce dépôt (AUDIT.md §1).
"""
from datetime import datetime, timezone

import pytest

from core import odds_api, scan_windows

CATALOGUE = [
    {"key": "tennis_atp_us_open",          "active": True,  "has_outrights": False},
    {"key": "tennis_wta_us_open",          "active": True,  "has_outrights": False},
    {"key": "tennis_atp_cincinnati_open",  "active": True,  "has_outrights": False},
    {"key": "tennis_atp_winston_salem",    "active": True,  "has_outrights": False},  # ATP 250 : NON
    {"key": "tennis_atp_us_open_winner",   "active": True,  "has_outrights": True},   # outright : NON
    {"key": "tennis_atp_wimbledon",        "active": False, "has_outrights": False},  # inactif : NON
    {"key": "tennis_atp_aus_open_singles", "active": True,  "has_outrights": False},
    {"key": "soccer_epl",                  "active": True,  "has_outrights": False},  # pas du tennis
]


class _Resp:
    def __init__(self, status=200, payload=None, raise_json=False):
        self.status_code = status
        self._p = payload
        self._raise = raise_json

    def json(self):
        if self._raise:
            raise ValueError("pas du JSON")
        return self._p


@pytest.fixture
def catalogue(monkeypatch):
    monkeypatch.delenv("TENNIS_DYNAMIC", raising=False)
    monkeypatch.setattr(odds_api.requests, "get", lambda *a, **k: _Resp(200, CATALOGUE))


class TestFiltre:
    def test_slams_et_masters_seulement(self, catalogue):
        found = odds_api.discover_tennis_keys("k")
        assert set(found) == {"tennis_atp_us_open", "tennis_wta_us_open",
                              "tennis_atp_cincinnati_open", "tennis_atp_aus_open_singles"}
        assert set(found.values()) == {"tennis"}

    def test_un_atp_250_ne_passe_pas(self, catalogue):
        assert "tennis_atp_winston_salem" not in odds_api.discover_tennis_keys("k")

    def test_outrights_et_inactifs_exclus(self, catalogue):
        found = odds_api.discover_tennis_keys("k")
        assert "tennis_atp_us_open_winner" not in found
        assert "tennis_atp_wimbledon" not in found

    def test_ne_touche_pas_aux_autres_sports(self, catalogue):
        assert "soccer_epl" not in odds_api.discover_tennis_keys("k")


class TestPanneEtCoupeCircuit:
    """Politique du dépôt : une panne réseau rend [] + log, jamais une exception."""

    def test_http_non_200_rend_vide(self, monkeypatch):
        monkeypatch.delenv("TENNIS_DYNAMIC", raising=False)
        monkeypatch.setattr(odds_api.requests, "get", lambda *a, **k: _Resp(503))
        assert odds_api.discover_tennis_keys("k") == {}

    def test_reseau_mort_rend_vide(self, monkeypatch):
        monkeypatch.delenv("TENNIS_DYNAMIC", raising=False)

        def _boom(*a, **k):
            raise ConnectionError("dns")

        monkeypatch.setattr(odds_api.requests, "get", _boom)
        assert odds_api.discover_tennis_keys("k") == {}

    def test_json_illisible_rend_vide(self, monkeypatch):
        monkeypatch.delenv("TENNIS_DYNAMIC", raising=False)
        monkeypatch.setattr(odds_api.requests, "get", lambda *a, **k: _Resp(200, raise_json=True))
        assert odds_api.discover_tennis_keys("k") == {}

    def test_coupe_circuit_sans_aucun_appel(self, monkeypatch):
        monkeypatch.setenv("TENNIS_DYNAMIC", "0")

        def _interdit(*a, **k):
            pytest.fail("aucun appel réseau attendu avec TENNIS_DYNAMIC=0")

        monkeypatch.setattr(odds_api.requests, "get", _interdit)
        assert odds_api.discover_tennis_keys("k") == {}


class TestInjectionDansFetchOdds:
    """La découverte est branchée DANS fetch_odds, après la résolution de
    keys_to_scan — donc elle couvre le scan normal ET Golden Hour sans
    dupliquer une troisième liste de clés."""

    def test_un_seul_get_sports_par_scan(self, monkeypatch):
        """La sonde de clé (probe_key) télécharge déjà le catalogue : la
        découverte le RÉUTILISE. Deux GET /sports pour une information,
        c'était le premier jet — et ça cassait l'invariant du pool « chaque
        clé est sondée une fois » (tests/test_odds_api_keypool.py)."""
        monkeypatch.delenv("TENNIS_DYNAMIC", raising=False)

        def _interdit(*a, **k):
            pytest.fail("avec un catalogue fourni, aucun appel réseau n'est attendu")

        monkeypatch.setattr(odds_api.requests, "get", _interdit)
        found = odds_api.discover_tennis_keys("k", catalogue=CATALOGUE)
        assert "tennis_atp_us_open" in found

    def test_le_code_fusionne_les_cles_decouvertes(self):
        import inspect
        src = inspect.getsource(odds_api.fetch_odds)
        assert "discover_tennis_keys(api_key, catalogue=_LAST_CATALOGUE)" in src
        # L'injection vient APRÈS la résolution statique : on enrichit, on ne remplace pas
        assert src.index("keys_to_scan = sport_keys if sport_keys") < src.index("discover_tennis_keys(api_key")

    def test_les_cles_statiques_ne_portent_pas_le_tennis(self):
        """Sinon deux sources de vérité : la clé statique serait morte onze
        mois sur douze et contredirait la dynamique le douzième."""
        assert not [k for k in odds_api.SPORT_KEYS if k.startswith("tennis_")]


class TestFenetreParPrefixe:
    def test_une_cle_tennis_inconnue_a_une_fenetre(self):
        midi = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
        assert scan_windows.is_favorable("tennis_atp_us_open", midi)
        assert scan_windows.is_favorable("tennis_wta_jamais_vue", midi)

    def test_hors_fenetre(self):
        six_h = datetime(2026, 9, 1, 6, 0, tzinfo=timezone.utc)
        assert not scan_windows.is_favorable("tennis_atp_us_open", six_h)

    def test_une_cle_exacte_garde_la_priorite_sur_le_prefixe(self):
        """Si un jour une clé tennis reçoit sa propre entrée dans _WINDOWS,
        c'est elle qui doit gagner — le préfixe n'est qu'un repli."""
        assert scan_windows._WINDOWS.get("americanfootball_ncaaf") is not None
        # Et une clé totalement inconnue, sans préfixe déclaré, reste « jamais favorable »
        assert not scan_windows.is_favorable("lacrosse_pll", datetime(2026, 9, 1, 12, tzinfo=timezone.utc))
