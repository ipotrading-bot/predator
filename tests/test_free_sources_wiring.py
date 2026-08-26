"""
tests/test_free_sources_wiring.py — les trois réparations du 2026-08-26.

1. `core/net.py` — porte de sortie proxy pour les sources filtrées par IP.
   odds.500.com rend HTTP 200 depuis un poste de développement et
   `Connection refused` depuis les runners GitHub : c'est la PLAGE D'IP qui
   est refusée, aucune correction de code ne lève ça. Le module doit donc
   être strictement inerte tant qu'aucun proxy n'est configuré — sinon on
   impose un proxy à des sources qui n'en ont pas besoin.

2. 7M — mémoire des matchs DÉJÀ JOUÉS. Mesuré sur 30 identifiants de tête du
   sitemap : 0 échec de requête, **27 matchs terminés**, 3 utiles. Sans
   mémoire, ces 27 sont repayés à chaque passage du curseur.

3. Kalshi/Polymarket — branchés. Le module existait depuis le 2026-08-22 et
   n'était importé NULLE PART hors de ses tests : capacité morte en silence.
   Rôle `consensus` : il mesure, il n'émet jamais et ne modifie aucun prix.

Aucun réseau (tests/conftest.py) : tout ce qui sort est stubbé.
"""
import os
from datetime import datetime, timedelta, timezone

import pytest

from core import free_sources, net, sevenm
from core.source_adapter import Fixture


# ── 1. core/net.py ──────────────────────────────────────────────────────

class TestPorteDeSortieProxy:

    @pytest.fixture(autouse=True)
    def _memo_neuf(self, monkeypatch):
        """`proxy_for` mémorise sa résolution POUR TOUT LE PROCESSUS (sans
        quoi l'absence de proxy — le cas nominal — relirait Supabase à chaque
        requête HTTP). Sans ce reset, le premier test figerait la réponse des
        suivants. `secret_store` est court-circuité : ces tests portent sur la
        résolution, pas sur la base."""
        net.reset()
        monkeypatch.setattr("core.secret_store.get_secret",
                            lambda name, **_k: os.environ.get(name) or None)
        yield
        net.reset()

    def test_inerte_sans_configuration(self, monkeypatch):
        """Le cas nominal : aucun proxy, aucun changement de comportement."""
        for var in ("FREE_SOURCES_PROXY", "ODDS500_PROXY", "SEVENM_PROXY"):
            monkeypatch.delenv(var, raising=False)
        assert net.proxy_for("odds500") == ""
        assert net.opener_for("odds500") is None

    def test_override_par_source_bat_le_global(self, monkeypatch):
        """500.com et 7M ne sont pas hébergés au même endroit : l'un peut
        être bloqué sans l'autre."""
        monkeypatch.setenv("FREE_SOURCES_PROXY", "http://global:1")
        monkeypatch.setenv("ODDS500_PROXY", "http://pour-500:2")
        assert net.proxy_for("odds500") == "http://pour-500:2"
        assert net.proxy_for("sevenm") == "http://global:1"
        assert net.opener_for("sevenm") is not None

    def test_le_message_distingue_injoignable_et_en_panne(self, monkeypatch):
        """Les deux se ressemblent dans un log de cron et n'appellent PAS la
        même action : « fournis un proxy » vs « le site a changé »."""
        monkeypatch.delenv("FREE_SOURCES_PROXY", raising=False)
        monkeypatch.delenv("ODDS500_PROXY", raising=False)

        refus = net.describe_failure("odds500", OSError("[Errno 111] Connection refused"))
        assert "INJOIGNABLE" in refus and "ODDS500_PROXY" in refus

        panne = net.describe_failure("odds500", ValueError("balise absente"))
        assert "INJOIGNABLE" not in panne and "balise absente" in panne

    def test_avec_proxy_le_message_accuse_le_proxy(self, monkeypatch):
        """Sinon on renvoie l'opérateur configurer ce qui l'est déjà."""
        monkeypatch.setenv("ODDS500_PROXY", "http://p:1")
        msg = net.describe_failure("odds500", OSError("Connection refused"))
        assert "malgré le proxy" in msg


class TestModeRelais:
    """Le relais (Cloudflare Worker) réécrit l'URL ; le proxy, lui, tunnelise.

    Ce sont DEUX mécanismes, pas deux réglages du même : un Worker ne parle
    pas CONNECT, `ProxyHandler` ne sait donc pas s'en servir.
    """

    @pytest.fixture(autouse=True)
    def _memo_neuf(self, monkeypatch):
        net.reset()
        monkeypatch.setattr("core.secret_store.get_secret",
                            lambda name, **_k: os.environ.get(name) or None)
        for v in ("FREE_SOURCES_RELAY", "FREE_SOURCES_RELAY_TOKEN",
                  "ODDS500_RELAY", "SEVENM_RELAY", "ODDS500_RELAY_TOKEN"):
            monkeypatch.delenv(v, raising=False)
        yield
        net.reset()

    def test_inerte_sans_relais(self):
        """Cas nominal : URL et en-têtes rendus tels quels."""
        u, h = net.prepare("odds500", "https://odds.500.com/", {"User-Agent": "X"})
        assert u == "https://odds.500.com/"
        assert h == {"User-Agent": "X"}

    def test_url_cible_encodee_et_jeton_ajoute(self, monkeypatch):
        monkeypatch.setenv("ODDS500_RELAY", "https://w.example.workers.dev/")
        monkeypatch.setenv("FREE_SOURCES_RELAY_TOKEN", "s3cr3t")
        u, h = net.prepare("odds500", "https://odds.500.com/fenxi/ouzhi-1.shtml",
                           {"User-Agent": "X"})
        # La cible est encodée : sinon son propre chemin casserait la query.
        assert u == ("https://w.example.workers.dev"
                     "?u=https%3A%2F%2Fodds.500.com%2Ffenxi%2Fouzhi-1.shtml")
        assert h["X-Relay-Token"] == "s3cr3t"
        assert h["User-Agent"] == "X"          # l'UA honnête est préservé

    def test_les_entetes_appelants_ne_sont_pas_mutes(self, monkeypatch):
        """`_HEADERS` est un dict de MODULE partagé : le muter contaminerait
        tous les appels suivants, y compris hors relais."""
        monkeypatch.setenv("FREE_SOURCES_RELAY", "https://w.example.dev")
        monkeypatch.setenv("FREE_SOURCES_RELAY_TOKEN", "t")
        origine = {"User-Agent": "X"}
        net.prepare("odds500", "https://odds.500.com/", origine)
        assert origine == {"User-Agent": "X"}

    def test_le_worker_garde_sa_liste_blanche_et_son_jeton(self):
        """Un relais sans ces deux gardes EST un proxy ouvert.

        Vérifié sur la source du Worker : c'est le seul endroit où ces gardes
        vivent, et les retirer « pour tester » est exactement ce qu'il ne faut
        pas pouvoir faire sans que la suite le dise.
        """
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent
               / "scripts" / "cloudflare_relay_worker.js").read_text(encoding="utf-8")
        assert "ALLOWED_HOSTS" in src
        for host in ("odds.500.com", "www.7msport.com", "px-analyse.7mdt.com"):
            assert host in src, host
        assert "X-Relay-Token" in src and "RELAY_TOKEN" in src
        # Le corps doit rester des OCTETS : `.text()` transcoderait le GB18030
        # de 500.com en UTF-8 et rendrait tous les noms chinois illisibles.
        #
        # Vérifié sur le CODE, commentaires retirés : ce fichier explique
        # justement pourquoi il ne faut PAS appeler `.text()`, et une
        # recherche sur le texte brut se déclencherait sur cette explication
        # (même piège que les noms de modèles morts dans le registre IA).
        import re as _re
        code = _re.sub(r"/\*.*?\*/", "", src, flags=_re.S)
        code = _re.sub(r"^\s*//.*$", "", code, flags=_re.M)
        assert "upstream.body" in code
        assert ".text()" not in code


# ── 2. 7M : ne jamais repayer un match déjà joué ────────────────────────

def _fx(gid, kickoff):
    return Fixture(source="sevenm", match_id=str(gid), kickoff=kickoff,
                   league="L", home="H", away="A", team_ids=("1", "2"), lang="en")


class TestMemoireDesMatchsJoues:

    @pytest.fixture(autouse=True)
    def _stub(self, monkeypatch):
        monkeypatch.setattr(sevenm.time, "sleep", lambda _s: None)
        monkeypatch.setattr(sevenm.daily_quota, "spent", lambda _b: 0)
        monkeypatch.setattr(sevenm.daily_quota, "add", lambda _b, _n=1: None)

    def test_les_matchs_passes_sont_signales_a_l_appelant(self, monkeypatch):
        now = datetime.now(timezone.utc)
        table = {
            "joue1": now - timedelta(hours=30),
            "joue2": now - timedelta(hours=10),
            "avenir": now + timedelta(hours=5),
        }
        monkeypatch.setattr(sevenm, "fetch_fixture",
                            lambda gid: _fx(gid, table[gid]))
        past = []
        out = sevenm.fetch_fixtures(match_ids=list(table), max_matches=10,
                                    past_out=past)
        assert [f.match_id for f in out] == ["avenir"]
        assert sorted(past) == ["joue1", "joue2"]

    def test_sans_past_out_le_comportement_est_inchange(self, monkeypatch):
        """Compat ascendante : l'argument est optionnel."""
        now = datetime.now(timezone.utc)
        monkeypatch.setattr(sevenm, "fetch_fixture",
                            lambda gid: _fx(gid, now + timedelta(hours=2)))
        assert len(sevenm.fetch_fixtures(match_ids=["a", "b"], max_matches=10)) == 2

    def test_learn_aliases_retire_les_joues_avant_de_depenser(self, monkeypatch):
        """Le gain est là : les identifiants mémorisés ne coûtent plus RIEN.

        Sans ce filtre, le curseur les repayait à chaque passage — 27 requêtes
        sur 30 pour des matchs terminés, mesuré le 2026-08-26.
        """
        monkeypatch.setattr(free_sources, "_past_get", lambda: {"joue1", "joue2"})
        monkeypatch.setattr(free_sources, "_past_set", lambda _s: None)
        monkeypatch.setattr(free_sources, "_cursor_get", lambda: 0)
        monkeypatch.setattr(free_sources, "_cursor_set", lambda _v: None)
        monkeypatch.setattr(sevenm, "fetch_match_ids",
                            lambda: ["joue1", "joue2", "frais1", "frais2"])
        vus = {}

        def _fetch(max_matches=None, offset=0, match_ids=None, past_out=None):
            vus["ids"] = list(match_ids or [])
            return []
        monkeypatch.setattr(sevenm, "fetch_fixtures", _fetch)
        monkeypatch.setattr(free_sources.team_aliases, "canonical",
                            lambda *_a, **_k: None)

        free_sources.learn_aliases([_fx("x", datetime.now(timezone.utc))])
        assert vus["ids"] == ["frais1", "frais2"], vus


# ── 3. Kalshi/Polymarket branchés ───────────────────────────────────────

class TestConsensusBranche:

    def test_le_harvester_appelle_bien_la_mesure(self, monkeypatch):
        """La panne d'origine : le module n'était importé NULLE PART.
        Ce test échoue si quelqu'un débranche à nouveau l'appel."""
        from core import harvester
        appels = []
        monkeypatch.setattr(free_sources, "measure_slate_consensus",
                            lambda sid, ms: appels.append((sid, len(ms))))
        harvester._measure_consensus(1, [{"id": "a"}])
        assert appels == [(1, 1)]

    def test_ne_modifie_jamais_le_slate(self, monkeypatch):
        """Rôle `consensus` : il mesure, il n'émet pas et ne reprice pas."""
        monkeypatch.setattr(free_sources, "load_scorecard", lambda _n: {})
        monkeypatch.setattr(free_sources, "save_scorecard", lambda _c: None)
        monkeypatch.setattr(free_sources, "consensus_fixtures",
                            lambda _sid: [_fx("k1", datetime.now(timezone.utc))])
        monkeypatch.setattr(free_sources, "pair_fixtures", lambda _l, _r: [])
        slate = [{"id": "a", "home": "H", "away": "A",
                  "odds_1xbet": {"1": 2.0, "X": 3.0, "2": 4.0}}]
        avant = [dict(m) for m in slate]
        free_sources.measure_slate_consensus(1, slate)
        assert slate == avant

    def test_un_sport_non_couvert_ne_coute_aucun_appel(self, monkeypatch):
        """Kalshi/Polymarket ne cotent qu'EPL/UCL/NFL/NBA."""
        def _boom(_sid):
            raise AssertionError("appel réseau pour un sport non couvert")
        monkeypatch.setattr(free_sources, "consensus_fixtures", _boom)
        assert free_sources.measure_slate_consensus(99, [{"id": "a"}]) == 0

    def test_une_panne_des_marches_ne_casse_rien(self, monkeypatch):
        """Best-effort, comme toute source de ce dépôt."""
        monkeypatch.setattr(free_sources, "load_scorecard", lambda _n: {})
        monkeypatch.setattr(free_sources, "save_scorecard", lambda _c: None)
        monkeypatch.setattr(free_sources, "record_observation",
                            lambda card, **_k: card)

        def _boom(_sid):
            raise RuntimeError("API morte")
        monkeypatch.setattr(free_sources, "consensus_fixtures", _boom)
        assert free_sources.measure_slate_consensus(1, [{"id": "a"}]) == 0
