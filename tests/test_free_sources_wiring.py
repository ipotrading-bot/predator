"""
tests/test_free_sources_wiring.py — la sortie réseau (core/net.py) et le
consensus Kalshi/Polymarket, ce qui reste de la « mission 3 ».

1. `core/net.py` — porte de sortie proxy pour les sources filtrées par IP
   (le relais Cloudflare est RETIRÉ le 2026-09-10, voir TestRelaisRetire). Née pour odds.500.com (200 depuis un poste de dev, `Connection
   refused` depuis les runners GitHub : la PLAGE D'IP est refusée, aucune
   correction de code ne lève ça), elle sert aujourd'hui aux sources de
   SCORES du settlement (core/score_sources.py, ESPN…). Le module doit
   rester strictement inerte tant qu'aucun proxy n'est configuré — sinon
   on impose un proxy à des sources qui n'en ont pas besoin.

2. Kalshi/Polymarket — branchés. Le module existait depuis le 2026-08-22 et
   n'était importé NULLE PART hors de ses tests : capacité morte en silence.
   Rôle `consensus` : il mesure, il n'émet jamais et ne modifie aucun prix.

odds500, 7M et le dictionnaire d'alias sont RETIRÉS le 2026-09-03 (mur
anti-bot EdgeOne, décision opérateur) — leurs tests avec eux.

Aucun réseau (tests/conftest.py) : tout ce qui sort est stubbé.
"""
import os
from datetime import datetime, timezone

import pytest

from core import free_sources, net
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
        for var in ("FREE_SOURCES_PROXY", "ESPN_PROXY"):
            monkeypatch.delenv(var, raising=False)
        assert net.proxy_for("espn") == ""
        assert net.opener_for("espn") is None

    def test_override_par_source_bat_le_global(self, monkeypatch):
        """Deux sources ne sont pas hébergées au même endroit : l'une peut
        être bloquée sans l'autre."""
        monkeypatch.setenv("FREE_SOURCES_PROXY", "http://global:1")
        monkeypatch.setenv("ESPN_PROXY", "http://pour-espn:2")
        assert net.proxy_for("espn") == "http://pour-espn:2"
        assert net.proxy_for("thesportsdb") == "http://global:1"
        assert net.opener_for("thesportsdb") is not None

    def test_le_message_distingue_injoignable_et_en_panne(self, monkeypatch):
        """Les deux se ressemblent dans un log de cron et n'appellent PAS la
        même action : « fournis un proxy » vs « le site a changé »."""
        monkeypatch.delenv("FREE_SOURCES_PROXY", raising=False)
        monkeypatch.delenv("ESPN_PROXY", raising=False)

        refus = net.describe_failure("espn", OSError("[Errno 111] Connection refused"))
        assert "INJOIGNABLE" in refus and "ESPN_PROXY" in refus

        panne = net.describe_failure("espn", ValueError("balise absente"))
        assert "INJOIGNABLE" not in panne and "balise absente" in panne

    def test_avec_proxy_le_message_accuse_le_proxy(self, monkeypatch):
        """Sinon on renvoie l'opérateur configurer ce qui l'est déjà."""
        monkeypatch.setenv("ESPN_PROXY", "http://p:1")
        msg = net.describe_failure("espn", OSError("Connection refused"))
        assert "malgré le proxy" in msg


class TestRepriseSurEchecPassager:
    """Un proxy gratuit et partagé rate des requêtes ; la source ne doit pas
    tomber pour autant.

    Mesuré le 2026-08-28 sur le proxy Webshare : trois GET identiques, un
    timeout TLS à 40 s et deux réponses en ~1 s. Sans reprise, cette requête
    ratée coûtait la source entière pour le run.
    """

    def test_deux_reprises_sur_des_echecs_de_transport(self, monkeypatch):
        essais = []

        def _urlopen(req, timeout=None):
            essais.append(1)
            # Les échecs se GROUPENT : mesuré depuis un runner le 2026-08-28,
            # les deux premières tentatives ont échoué sur le même scan.
            if len(essais) < 3:
                raise TimeoutError("handshake")
            return "réponse"

        monkeypatch.setattr(net.urllib.request, "urlopen", _urlopen)
        monkeypatch.setattr(net, "opener_for", lambda source: None)
        assert net.open_with_retry("espn", object(), 5) == "réponse"
        assert len(essais) == 3

    def test_un_403_n_est_PAS_rejoue(self, monkeypatch):
        """Un HTTPError est une RÉPONSE du serveur, pas un aléa réseau. La
        rejouer ne changerait rien et martèlerait la source — ce que le
        budget journalier et robots.txt existent pour éviter."""
        essais = []

        def _urlopen(req, timeout=None):
            essais.append(1)
            raise net.urllib.error.HTTPError("u", 403, "Forbidden", {}, None)

        monkeypatch.setattr(net.urllib.request, "urlopen", _urlopen)
        monkeypatch.setattr(net, "opener_for", lambda source: None)
        with pytest.raises(net.urllib.error.HTTPError):
            net.open_with_retry("espn", object(), 5)
        assert len(essais) == 1, "un 403 a été rejoué"

    def test_un_tunnel_mort_bascule_en_direct_pour_le_reste_du_run(self, monkeypatch):
        """2026-09-09 : le tunnel Webshare répondait « 402 Payment Required »
        (quota du plan) à CHAQUE ouverture ; trois reprises du même tunnel
        faisaient passer ESPN pour muette et deux scans payants sont sortis à
        0 signal. Un refus du TUNNEL n'est pas un refus de la source : on sort
        en direct, une fois pour toutes dans le processus."""
        via_proxy, en_direct = [], []

        class _Opener:
            def open(self, req, timeout=None):
                via_proxy.append(1)
                raise net.urllib.error.URLError(
                    "Tunnel connection failed: 402 Payment Required")

        def _urlopen(req, timeout=None):
            en_direct.append(1)
            return "réponse"

        net.reset()
        monkeypatch.setattr(net, "proxy_for", lambda source: "http://user:pw@proxy:8080")
        monkeypatch.setattr(net.urllib.request, "build_opener", lambda *a, **k: _Opener())
        monkeypatch.setattr(net.urllib.request, "urlopen", _urlopen)
        assert net.open_with_retry("espn", object(), 5) == "réponse"
        assert len(via_proxy) == 1 and len(en_direct) == 1
        # Deuxième appel du même run : plus une seule tentative par le tunnel.
        assert net.open_with_retry("espn", object(), 5) == "réponse"
        assert len(via_proxy) == 1 and len(en_direct) == 2
        assert net.opener_for("espn") is None
        net.reset()

    def test_un_timeout_ordinaire_ne_bascule_pas_en_direct(self, monkeypatch):
        """Un timeout passager du proxy reste une reprise PAR le proxy : la
        bascule directe est réservée aux refus explicites du tunnel."""
        via_proxy = []

        class _Opener:
            def open(self, req, timeout=None):
                via_proxy.append(1)
                if len(via_proxy) < 2:
                    raise TimeoutError("handshake")
                return "réponse"

        net.reset()
        monkeypatch.setattr(net, "proxy_for", lambda source: "http://user:pw@proxy:8080")
        monkeypatch.setattr(net.urllib.request, "build_opener", lambda *a, **k: _Opener())
        monkeypatch.setattr(net.urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("direct interdit")))
        assert net.open_with_retry("espn", object(), 5) == "réponse"
        assert len(via_proxy) == 2
        net.reset()

    def test_l_echec_final_remonte_a_l_appelant(self, monkeypatch):
        """L'appelant garde son `except` et son message : on ne change que le
        nombre d'essais, jamais le contrat d'erreur."""
        monkeypatch.setattr(net.urllib.request, "urlopen",
                            lambda req, timeout=None: (_ for _ in ()).throw(TimeoutError("ko")))
        monkeypatch.setattr(net, "opener_for", lambda source: None)
        with pytest.raises(TimeoutError):
            net.open_with_retry("espn", object(), 5)

    def test_les_sources_de_scores_passent_par_la_reprise(self):
        """Le settlement lit ESPN/TheSportsDB/MLB depuis les runners : c'est
        LE consommateur de core/net.py depuis le retrait d'odds500 et 7M.
        Une source de scores hors de ce chemin serait une liste qui diverge."""
        import inspect
        from core import score_sources
        assert "open_with_retry" in inspect.getsource(score_sources), \
            "core/score_sources.py n'a pas de reprise sur échec de transport"


class TestRelaisRetire:
    """Le relais Cloudflare est RETIRÉ (2026-09-10) et ne doit pas revenir.

    Posé le 2026-08-26 pour odds500 (retirée le 09-03), prouvé inopérant
    depuis les runners (colo IAD). Resté dans le pool scan après la
    suppression du proxy (09-09 20:27), il a capté ESPN : le Worker, à liste
    blanche VIDE, répondait 403 — lu comme « ESPN muet », 75-90 % des matchs
    écartés à chaque scan standard, 0 signal émis le 10. Un secret oublié ne
    doit plus pouvoir détourner une source : ni code, ni nom transmis, ni
    script à redéployer."""

    def test_aucune_variable_de_relais_ne_reecrit_une_url(self, monkeypatch):
        for v in ("FREE_SOURCES_RELAY", "ESPN_RELAY"):
            monkeypatch.setenv(v, "https://w.example.dev")
        monkeypatch.setenv("FREE_SOURCES_RELAY_TOKEN", "t")
        monkeypatch.setattr("core.secret_store.get_secret",
                            lambda name, **_k: os.environ.get(name) or None)
        net.reset()
        u, h = net.prepare("espn", "https://site.api.espn.com/", {"User-Agent": "X"})
        assert u == "https://site.api.espn.com/"
        assert h == {"User-Agent": "X"}
        net.reset()

    def test_le_module_ne_connait_plus_le_relais(self):
        from pathlib import Path
        assert not hasattr(net, "relay_for")
        code = Path(net.__file__).read_text(encoding="utf-8")
        code = code.split('"""', 2)[2]                 # hors docstring d'en-tête
        for motif in ("relay_for", "X-Relay-Token", "?u=", "_RELAY_ENV"):
            assert motif not in code, f"{motif} : le relais revient dans core/net.py"

    def test_les_scripts_du_relais_sont_partis(self):
        from pathlib import Path
        racine = Path(__file__).resolve().parent.parent
        for f in ("scripts/cloudflare_relay_worker.js", "scripts/relay_smart_placement.py"):
            assert not (racine / f).exists(), f"{f} : script du relais revenu"


# ── 2. Kalshi/Polymarket branchés ───────────────────────────────────────

def _fx(gid, kickoff):
    return Fixture(source="consensus", match_id=str(gid), kickoff=kickoff,
                   league="L", home="H", away="A", team_ids=("1", "2"), lang="en")


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

    def test_le_coupe_circuit_debranche_la_mesure(self, monkeypatch):
        """`FREE_SOURCES=0` : aucun appel, aucune écriture."""
        monkeypatch.setattr(free_sources, "ENABLED", False)

        def _boom(_sid):
            raise AssertionError("appel malgré FREE_SOURCES=0")
        monkeypatch.setattr(free_sources, "consensus_fixtures", _boom)
        assert free_sources.measure_slate_consensus(1, [{"id": "a"}]) == 0

    def test_plus_aucune_source_asiatique_dans_le_depot(self):
        """odds500, 7M et le dictionnaire d'alias sont partis le 2026-09-03 :
        les réintroduire est une décision opérateur, pas un import qui
        traîne."""
        from pathlib import Path
        core = Path(__file__).resolve().parent.parent / "core"
        for name in ("odds500.py", "sevenm.py", "team_aliases.py"):
            assert not (core / name).exists(), f"core/{name} est revenu"
        import inspect
        from core import harvester
        src = inspect.getsource(harvester)
        assert "fetch_odds500" not in src and "_fetch_from_odds500" not in src
