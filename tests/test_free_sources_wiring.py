"""
tests/test_free_sources_wiring.py — la sortie réseau (core/net.py) et le
consensus Kalshi/Polymarket, ce qui reste de la « mission 3 ».

1. `core/net.py` — porte de sortie proxy/relais pour les sources filtrées
   par IP. Née pour odds.500.com (200 depuis un poste de dev, `Connection
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
import logging
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


class TestDiagnostic403Relais:
    """Un 403 en mode relais a deux causes qui n'appellent pas la même action.

    Le Worker pose `X-Relay-By` sur ce qu'il a relayé ; ses propres refus ne
    le portent pas. Mesuré le 2026-08-26 : même jeton, 200 depuis un poste de
    dev (colo LHR), 403 depuis les runners GitHub — sans ce diagnostic, on
    accuse le jeton alors que c'est l'amont qui refuse l'edge."""

    @staticmethod
    def _http403(headers: dict):
        import email.message
        import io as _io
        import urllib.error
        h = email.message.Message()
        for k, v in headers.items():
            h[k] = v
        return urllib.error.HTTPError("https://x/?u=y", 403, "Forbidden", h, _io.BytesIO(b"forbidden"))

    def test_403_relaye_accuse_l_amont_et_nomme_le_colo(self, monkeypatch):
        monkeypatch.setenv("FREE_SOURCES_RELAY", "https://r.example")
        net.reset()
        msg = net.describe_failure("espn", self._http403({"X-Relay-By": "predator", "cf-ray": "a314-IAD"}))
        assert "AMONT" in msg and "IAD" in msg and "jeton" in msg

    def test_403_du_worker_accuse_le_jeton(self, monkeypatch):
        monkeypatch.setenv("FREE_SOURCES_RELAY", "https://r.example")
        net.reset()
        msg = net.describe_failure("espn", self._http403({"cf-ray": "a314-LHR"}))
        assert "RELAIS lui-même" in msg and "LHR" in msg and "RELAY_TOKEN" in msg

    def test_sans_relais_un_403_reste_un_403_ordinaire(self, monkeypatch):
        # `_secret` lit secret_store puis l'environnement (et un fichier de
        # credentials local peut porter FREE_SOURCES_RELAY) : on coupe la
        # résolution à la source, pas seulement les variables.
        monkeypatch.setattr(net, "_secret", lambda name: "")
        net.reset()
        msg = net.describe_failure("espn", self._http403({}))
        assert "AMONT" not in msg and "RELAIS" not in msg


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
                  "ESPN_RELAY", "ESPN_RELAY_TOKEN", "ESPN_PROXY"):
            monkeypatch.delenv(v, raising=False)
        yield
        net.reset()

    def test_inerte_sans_relais(self):
        """Cas nominal : URL et en-têtes rendus tels quels."""
        u, h = net.prepare("espn", "https://site.api.espn.com/", {"User-Agent": "X"})
        assert u == "https://site.api.espn.com/"
        assert h == {"User-Agent": "X"}

    def test_url_cible_encodee_et_jeton_ajoute(self, monkeypatch):
        monkeypatch.setenv("ESPN_RELAY", "https://w.example.workers.dev/")
        monkeypatch.setenv("FREE_SOURCES_RELAY_TOKEN", "s3cr3t")
        u, h = net.prepare("espn", "https://site.api.espn.com/apis/v2/scoreboard",
                           {"User-Agent": "X"})
        # La cible est encodée : sinon son propre chemin casserait la query.
        assert u == ("https://w.example.workers.dev"
                     "?u=https%3A%2F%2Fsite.api.espn.com%2Fapis%2Fv2%2Fscoreboard")
        assert h["X-Relay-Token"] == "s3cr3t"
        assert h["User-Agent"] == "X"          # l'UA honnête est préservé

    def test_un_proxy_pose_l_emporte_sur_le_relais(self, monkeypatch):
        """La panne la plus coûteuse serait SILENCIEUSE.

        Le relais est PROUVÉ inopérant depuis les runners GitHub : un Worker
        s'exécute au colo le plus proche de l'APPELANT (IAD), et l'amont
        peut refuser cette IP de sortie. Avec l'ancienne précédence, un
        opérateur qui pose un proxy pour débloquer la source voyait le relais
        capter l'URL malgré tout — capacité payée, jamais utilisée, et pas
        une ligne de log pour le dire.
        """
        monkeypatch.setenv("FREE_SOURCES_RELAY", "https://w.example.dev")
        monkeypatch.setenv("FREE_SOURCES_RELAY_TOKEN", "t")
        monkeypatch.setenv("ESPN_PROXY", "http://u:p@eu-proxy.example:8080")
        net.reset()
        u, h = net.prepare("espn", "https://site.api.espn.com/", {"User-Agent": "X"})
        assert u == "https://site.api.espn.com/", "le relais a capté l'URL malgré le proxy"
        assert "X-Relay-Token" not in h
        # Et le proxy est bien celui qui sera emprunté.
        assert net.proxy_for("espn") == "http://u:p@eu-proxy.example:8080"

    def test_le_message_proxy_n_est_logge_qu_une_fois(self, monkeypatch, caplog):
        """`prepare()` est appelé à chaque requête : sans mémoire, un run
        sortait quinze lignes identiques. Un log qu'on ne lit plus ne sert à
        rien."""
        monkeypatch.setenv("FREE_SOURCES_RELAY", "https://w.example.dev")
        monkeypatch.setenv("ESPN_PROXY", "http://u:p@eu.example:8080")
        net.reset()
        with caplog.at_level(logging.INFO, logger="PREDATOR.net"):
            for _ in range(5):
                net.prepare("espn", "https://site.api.espn.com/", {})
        lignes = [r for r in caplog.records if "proxy configuré" in r.getMessage()]
        assert len(lignes) == 1, f"{len(lignes)} lignes au lieu d'une"

    def test_sans_proxy_le_relais_reprend_la_main(self, monkeypatch):
        """L'inversion ne doit pas désactiver le relais pour tout le monde :
        il reste le chemin par défaut quand aucun proxy n'est posé."""
        monkeypatch.setenv("FREE_SOURCES_RELAY", "https://w.example.dev")
        monkeypatch.delenv("FREE_SOURCES_PROXY", raising=False)
        net.reset()
        u, _h = net.prepare("espn", "https://site.api.espn.com/", {})
        assert u.startswith("https://w.example.dev?u=")

    def test_les_entetes_appelants_ne_sont_pas_mutes(self, monkeypatch):
        """`_HEADERS` est un dict de MODULE partagé : le muter contaminerait
        tous les appels suivants, y compris hors relais."""
        monkeypatch.setenv("FREE_SOURCES_RELAY", "https://w.example.dev")
        monkeypatch.setenv("FREE_SOURCES_RELAY_TOKEN", "t")
        origine = {"User-Agent": "X"}
        net.prepare("espn", "https://site.api.espn.com/", origine)
        assert origine == {"User-Agent": "X"}

    def test_le_worker_garde_sa_liste_blanche_et_son_jeton(self):
        """Un relais sans ces deux gardes EST un proxy ouvert.

        Vérifié sur la source du Worker : c'est le seul endroit où ces gardes
        vivent, et les retirer « pour tester » est exactement ce qu'il ne faut
        pas pouvoir faire sans que la suite le dise. Depuis le retrait
        d'odds500/7M la liste blanche est VIDE : le Worker ne relaie rien tant
        qu'une source n'y inscrit pas son hôte — c'est le comportement sûr.
        """
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent
               / "scripts" / "cloudflare_relay_worker.js").read_text(encoding="utf-8")
        assert "ALLOWED_HOSTS" in src
        assert "X-Relay-Token" in src and "RELAY_TOKEN" in src
        liste = src.split("const ALLOWED_HOSTS")[1].split("]);")[0]
        for host in ("odds.500.com", "7msport.com", "7mdt.com"):
            assert host not in liste, \
                f"{host} : source retirée le 2026-09-03, hôte encore relayé"
        # Le corps doit rester des OCTETS : `.text()` transcoderait un corps
        # non UTF-8 et rendrait les libellés illisibles.
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
