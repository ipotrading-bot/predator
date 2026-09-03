"""
tests/test_free_sources.py — branchement des sources gratuites (mission 3).

CE QUI EST VÉRIFIÉ
------------------
Les trois garanties SANS lesquelles ce branchement serait dangereux :

  1. un libellé chinois n'atteint JAMAIS le moteur — un match dont une équipe
     ne se résout pas est écarté, pas émis avec son nom brut ;
  2. le MODE OMBRE tient : tant que la source n'est pas promue, elle rend []
     quoi qu'elle ait collecté ;
  3. rien de tout cela ne peut faire tomber un scan — panne réseau, base
     absente, 7M muet : on rend [], jamais une exception.

Aucun réseau (tests/conftest.py) : odds500/sevenm/la base sont stubbés.
"""
import pytest

from core import free_sources as FS
from core import odds500, sevenm, team_aliases
from core.source_adapter import Fixture

PINNACLE = [9.19, 5.14, 1.36]


def _match(mid="o500_1", home="曼联", away="赫尔城", ids=("1075", "872"),
           league="英超", when="2026-08-22T19:30:00Z", odds=None, needs=True):
    o = odds or PINNACLE
    return {"id": mid, "match": f"{home} vs {away}", "home": home, "away": away,
            "league": league, "sport": "soccer", "sport_id": 1,
            "commence_time": when,
            "odds_1xbet": {"1": o[0], "X": o[1], "2": o[2]},
            "odds_pinnacle": {"1": o[0], "X": o[1], "2": o[2]},
            "_needs_alias": needs, "_alias_team_ids": ids,
            "_alias_source": "odds500", "_freshness_s": 120}


@pytest.fixture
def dico(monkeypatch):
    """Dictionnaire d'alias en mémoire."""
    store = {}

    def canonical(source, alias, team_id=None, league=None, min_confidence=None):
        return store.get(alias)
    monkeypatch.setattr(team_aliases, "canonical", canonical)
    monkeypatch.setattr(team_aliases, "apply_pairing",
                        lambda *a, **k: {"appris": 0, "confirmés": 0, "contredits": 0})
    # L'IA ne décide jamais : par défaut elle ne propose rien.
    store_ai = []
    monkeypatch.setattr(team_aliases, "resolve_with_ai",
                        lambda source, alias, league="", **kw: store_ai.append(alias) or None)
    store["_ia"] = store_ai
    return store


@pytest.fixture
def carte(monkeypatch):
    """Scorecard en mémoire ; par défaut la source est en ombre."""
    card = {"source": "odds500", "matched": 0, "errors": 0, "requests": 0,
            "divergence_samples": [], "median_divergence_pts": None, "shadow": True}
    monkeypatch.setattr(FS, "load_scorecard", lambda n: card)
    monkeypatch.setattr(FS, "save_scorecard", lambda c: card.update(c))
    return card


class TestResolutionDesNoms:
    def test_les_noms_chinois_deviennent_canoniques(self, dico):
        dico.update({"曼联": "Manchester United", "赫尔城": "Hull City"})
        out, dropped = FS.resolve_names([_match()])
        assert dropped == 0
        assert (out[0]["home"], out[0]["away"]) == ("Manchester United", "Hull City")
        assert out[0]["match"] == "Manchester United vs Hull City"
        assert out[0]["_needs_alias"] is False

    def test_un_alias_manquant_ECARTE_le_match(self, dico):
        """Le comportement qui protège tout le reste : un libellé chinois qui
        atteindrait `signals` casserait le settlement six heures plus tard."""
        dico.update({"曼联": "Manchester United"})        # l'adversaire manque
        out, dropped = FS.resolve_names([_match()])
        assert out == [] and dropped == 1

    def test_aucun_libelle_chinois_ne_survit_jamais(self, dico):
        dico.update({"曼联": "Manchester United", "赫尔城": "Hull City"})
        out, _ = FS.resolve_names([_match(), _match(mid="o500_2", home="未知队")])
        for m in out:
            assert m["_needs_alias"] is False
            assert all(ord(c) < 0x2E80 for c in m["home"] + m["away"]), m

    def test_un_match_deja_resolu_passe_tel_quel(self, dico):
        m = _match(home="Arsenal", away="Chelsea", needs=False)
        out, dropped = FS.resolve_names([m])
        assert out == [m] and dropped == 0


class TestModeOmbre:
    def _stub_sources(self, monkeypatch, matches):
        monkeypatch.setattr(odds500, "fetch_matches", lambda *a, **k: matches)
        monkeypatch.setattr(FS, "learn_aliases", lambda *a, **k: {})

    def test_en_ombre_la_source_nemet_AUCUN_match(self, monkeypatch, dico, carte):
        dico.update({"曼联": "Manchester United", "赫尔城": "Hull City"})
        self._stub_sources(monkeypatch, [_match()])
        assert FS.fetch_odds500(1, trusted=[]) == []
        assert carte["shadow"] is True

    def test_en_ombre_elle_MESURE_quand_meme(self, monkeypatch, dico, carte):
        """On ne coupe pas la collecte : sans mesure, on ne saurait jamais si
        la source est bonne (logique de test_shadow_mode.py)."""
        dico.update({"曼联": "Manchester United", "赫尔城": "Hull City"})
        self._stub_sources(monkeypatch, [_match()])
        trusted = [_match(mid="mb_1", home="Manchester United", away="Hull City",
                          needs=False, odds=[9.0, 5.1, 1.37])]
        FS.fetch_odds500(1, trusted=trusted)
        assert carte["matched"] == 1
        assert carte["median_divergence_pts"] is not None

    def test_une_source_promue_emet_ses_matchs(self, monkeypatch, dico, carte):
        dico.update({"曼联": "Manchester United", "赫尔城": "Hull City"})
        carte.update({"shadow": False, "matched": 300,
                      "median_divergence_pts": 0.8})
        self._stub_sources(monkeypatch, [_match()])
        out = FS.fetch_odds500(1, trusted=[])
        assert len(out) == 1 and out[0]["home"] == "Manchester United"

    def test_la_promotion_est_loggee_dans_le_scorecard(self, monkeypatch, dico, carte):
        dico.update({"曼联": "Manchester United", "赫尔城": "Hull City"})
        carte.update({"matched": 300, "median_divergence_pts": 0.8, "shadow": True})
        self._stub_sources(monkeypatch, [_match()])
        FS.fetch_odds500(1, trusted=[])
        assert carte["shadow"] is False
        assert carte.get("promoted_at")          # jamais silencieuse


class TestPerimetre:
    def test_seul_le_football_est_concerne(self, monkeypatch):
        appels = []
        monkeypatch.setattr(odds500, "fetch_matches",
                            lambda *a, **k: appels.append(1) or [])
        for sport_id in (3, 4, 5):
            assert FS.fetch_odds500(sport_id, []) == []
        assert appels == []

    def test_le_coupe_circuit_debranche_tout(self, monkeypatch):
        appels = []
        monkeypatch.setattr(FS, "ENABLED", False)
        monkeypatch.setattr(odds500, "fetch_matches",
                            lambda *a, **k: appels.append(1) or [])
        assert FS.fetch_odds500(1, []) == []
        assert appels == []


class TestDegradation:
    def test_une_panne_odds500_ne_leve_jamais(self, monkeypatch, carte):
        def boom(*a, **k):
            raise RuntimeError("reseau")
        monkeypatch.setattr(odds500, "fetch_matches", boom)
        assert FS.fetch_odds500(1, []) == []
        assert carte["errors"] == 1

    def test_une_panne_7M_nempeche_pas_le_reste(self, monkeypatch, dico, carte):
        dico.update({"曼联": "Manchester United", "赫尔城": "Hull City"})
        monkeypatch.setattr(odds500, "fetch_matches", lambda *a, **k: [_match()])

        def boom(*a, **k):
            raise RuntimeError("7M down")
        monkeypatch.setattr(sevenm, "fetch_fixtures", boom)
        assert FS.fetch_odds500(1, []) == []      # ombre, mais pas d'exception

    def test_le_harvester_absorbe_une_panne_du_module(self, monkeypatch):
        """Le branchement lui-même ne doit jamais faire tomber un scan."""
        from core import harvester
        import core.free_sources as fs

        def boom(*a, **k):
            raise RuntimeError("casse")
        monkeypatch.setattr(fs, "fetch_odds500", boom)
        assert harvester._fetch_from_odds500(1, []) == []


class TestEconomieDuDictionnaire:
    def test_un_slate_deja_connu_ne_coute_aucune_requete_7M(self, monkeypatch, dico):
        """Le dictionnaire se construit ; il ne se retraduit jamais. Quand il
        couvre le slate, ce module cesse d'appeler 7M."""
        dico.update({"曼联": "Manchester United", "赫尔城": "Hull City"})
        appels = []
        monkeypatch.setattr(sevenm, "fetch_fixtures",
                            lambda *a, **k: appels.append(1) or [])
        fx = [Fixture(source="odds500", match_id="1",
                      kickoff="2026-08-22T19:30:00Z", league="英超",
                      home="曼联", away="赫尔城", odds=PINNACLE,
                      team_ids=("1075", "872"))]
        FS.learn_aliases(fx)
        assert appels == []

    def test_un_nom_inconnu_declenche_une_interrogation_7M(self, monkeypatch, dico):
        monkeypatch.setattr(team_aliases, "apply_pairing",
                            lambda *a, **k: {"appris": 2})
        appels = []
        monkeypatch.setattr(sevenm, "fetch_fixtures",
                            lambda *a, **k: appels.append(1) or [])
        fx = [Fixture(source="odds500", match_id="1",
                      kickoff="2026-08-22T19:30:00Z", league="英超",
                      home="未知队", away="他队", odds=PINNACLE)]
        FS.learn_aliases(fx)
        assert appels == [1]


class TestCurseurDeBalayage:
    """Régression du défaut trouvé EN LIVE le 2026-08-22.

    Sans curseur, chaque run réinterrogeait les 30 premiers identifiants du
    sitemap 7M — des coupes mineures sans recoupement avec le slate 500.com.
    Résultat mesuré : 0 alias appris, 25 matchs écartés, un dictionnaire qui
    ne se serait jamais rempli. Le branchement aurait été inerte en silence.
    """

    def _fx_inconnu(self):
        return [Fixture(source="odds500", match_id="1",
                        kickoff="2026-08-22T19:30:00Z", league="英超",
                        home="未知队", away="他队", odds=PINNACLE)]

    def test_le_curseur_avance_dun_run_a_lautre(self, monkeypatch, dico):
        vu = []
        monkeypatch.setattr(team_aliases, "apply_pairing", lambda *a, **k: {})
        monkeypatch.setattr(sevenm, "fetch_fixtures",
                            lambda **k: vu.append(k.get("offset")) or [])
        curseur = {"v": 0}
        monkeypatch.setattr(FS, "_cursor_get", lambda: curseur["v"])
        monkeypatch.setattr(FS, "_cursor_set", lambda v: curseur.__setitem__("v", v))

        FS.learn_aliases(self._fx_inconnu(), budget=30)
        FS.learn_aliases(self._fx_inconnu(), budget=30)
        FS.learn_aliases(self._fx_inconnu(), budget=30)
        assert vu == [0, 30, 60]          # jamais deux fois la même tranche

    def test_le_curseur_avance_meme_quand_rien_nest_appris(self, monkeypatch, dico):
        """C'est PRÉCISÉMENT quand une tranche ne recoupe pas le slate qu'il
        faut passer à la suivante, pas la réinterroger indéfiniment."""
        monkeypatch.setattr(team_aliases, "apply_pairing", lambda *a, **k: {})
        monkeypatch.setattr(sevenm, "fetch_fixtures", lambda **k: [])
        curseur = {"v": 0}
        monkeypatch.setattr(FS, "_cursor_get", lambda: curseur["v"])
        monkeypatch.setattr(FS, "_cursor_set", lambda v: curseur.__setitem__("v", v))
        FS.learn_aliases(self._fx_inconnu(), budget=25)
        assert curseur["v"] == 25

    def test_loffset_boucle_sur_le_sitemap(self, monkeypatch):
        """Un curseur qui dépasse la taille du sitemap doit revenir au début,
        pas rendre une liste vide à jamais."""
        ids = [str(i) for i in range(10)]
        vus = []
        monkeypatch.setattr(sevenm, "fetch_match_ids", lambda: ids)
        monkeypatch.setattr(sevenm, "fetch_fixture",
                            lambda gid: vus.append(gid) or None)
        monkeypatch.setattr(sevenm.time, "sleep", lambda s: None)
        sevenm.fetch_fixtures(max_matches=3, offset=9)
        assert vus == ["9", "0", "1"]


class TestLesDeuxChemainsQuiManquaient:
    """2026-08-28 : 27 matchs odds500 avec prix sharp réel, 26 écartés faute
    d'alias, 7M à court de budget (90/80) — alors que (a) le slate de
    confiance du run portait les noms anglais de ces mêmes matchs et que
    (b) `team_aliases.resolve_with_ai` existait depuis le 2026-08-22 sans
    être appelée nulle part."""

    def test_le_slate_de_confiance_enseigne_les_alias_sans_requete(self, monkeypatch, dico):
        seen = {}
        monkeypatch.setattr(team_aliases, "apply_pairing",
                            lambda source, pairs, canonical_source="sevenm":
                            seen.update(source=source, n=len(pairs), via=canonical_source)
                            or {"appris": len(pairs) * 2, "confirmés": 0, "contredits": 0})
        cn = _match()                                     # 曼联 vs 赫尔城, Pinnacle
        trusted = dict(_match(mid="t1", home="Manchester United", away="Hull City",
                              needs=False))
        trusted["_alias_source"] = "titan007"
        cn_fx = [FS._as_fixture(cn, "odds500")]
        bilan = FS.learn_from_trusted(cn_fx, [trusted])
        assert seen == {"source": "odds500", "n": 1, "via": "trusted"}
        assert bilan["appris"] == 2

    def test_sans_slate_de_confiance_rien_n_est_appris_ni_leve(self, dico):
        assert FS.learn_from_trusted([FS._as_fixture(_match(), "odds500")], []) == \
            {"appris": 0, "confirmés": 0, "contredits": 0}

    def test_l_IA_est_sollicitee_pour_un_nom_inconnu_mais_ne_decide_pas(self, dico):
        dico.update({"曼联": "Manchester United"})        # 赫尔城 inconnu
        out, dropped = FS.resolve_names([_match()])
        assert dico["_ia"] == ["赫尔城"], "l'IA doit être consultée pour le nom manquant"
        assert out == [] and dropped == 1, "proposition IA ≠ alias confirmé : le match reste écarté"

    def test_un_nom_connu_ne_sollicite_jamais_l_IA(self, dico):
        dico.update({"曼联": "Manchester United", "赫尔城": "Hull City"})
        FS.resolve_names([_match()])
        assert dico["_ia"] == []

    def test_une_panne_IA_n_ecarte_rien_de_plus(self, monkeypatch, dico):
        def boom(*a, **k):
            raise RuntimeError("quota")
        monkeypatch.setattr(team_aliases, "resolve_with_ai", boom)
        out, dropped = FS.resolve_names([_match()])
        assert out == [] and dropped == 1               # même verdict, pas d'exception


class TestLeSlateDeConfianceExigeLaLigue:
    """2026-08-28 15:48 : 4 alias faux sur 5 appris depuis le slate de
    confiance (拜仁 → UCD…), à 0,7 donc utilisables. `learn_from_trusted`
    doit apparier avec `require_league=True` — c'est le seul chemin qui écrit
    un alias utilisable en un seul passage."""

    def test_learn_from_trusted_exige_la_ligue(self, monkeypatch, dico):
        vu = {}
        def faux_pair(left, right, **kw):
            vu.update(kw)
            return []
        monkeypatch.setattr(FS, "pair_fixtures", faux_pair)
        trusted = dict(_match(mid="t1", home="UCD", away="Finn Harps", needs=False))
        FS.learn_from_trusted([FS._as_fixture(_match(), "odds500")], [trusted])
        assert vu.get("require_league") is True

    def test_une_ligue_inconnue_cote_confiance_napprend_rien(self, monkeypatch, dico):
        ecrit = []
        monkeypatch.setattr(team_aliases, "apply_pairing",
                            lambda source, pairs, canonical_source="sevenm":
                            ecrit.extend(pairs) or {"appris": 0, "confirmés": 0, "contredits": 0})
        cn = _match(league="德甲")
        trusted = dict(_match(mid="t1", home="UCD", away="Finn Harps", needs=False,
                              league="Ireland - First Division"))
        FS.learn_from_trusted([FS._as_fixture(cn, "odds500")], [trusted])
        assert ecrit == []
