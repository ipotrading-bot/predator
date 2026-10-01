"""
tests/test_euroleague_scores.py — l'Euroleague se règle par l'API officielle
de la ligue (2026-10-01).

Elle était ACHETÉE à OddsAPI et chacun de ses matchs sortait « NON RÉGLABLE »
: le scoreboard ESPN `basketball/euroleague` répond 200 avec zéro événement.
`api-live.euroleague.net` rend la saison entière en une requête ; chaque match
est remis sous la FORME d'un événement ESPN et servi par `_espn_jour`, pour
que tout l'étage ESPN (deux noms stricts, candidat unique, terminé seulement,
couverture au périmètre) s'applique sans copie.

Contrat gardé ici :
  1. la source est au registre, avec son budget (règle 13) ;
  2. un match non joué ne règle jamais ; un score nul non plus ;
  3. chaque nom d'équipe d'OddsAPI désigne UN SEUL club (tableau réel du
     2026-10-01) — un nom court d'un mot n'est pas exposé (« Žalgiris »
     s'appariait à « Paris ») ;
  4. le périmètre exige les deux camps ; le résultat dit sa source ;
  5. une requête par run, [] sur panne.

Aucun réseau : `_get_json` est monkeypatché.
"""
import pytest

import run_engine as eng
from core import odds_api, score_sources as ss

# Les 20 clubs tels que l'API les rend le 2026-10-01 : (name, abbreviatedName,
# editorialName).
CLUBS = {
    "BAR": ("FC Barcelona", "FC Barcelona", "Barca"),
    "PAR": ("Partizan Mozzart Bet Belgrade", "Partizan", "Partizan"),
    "OLY": ("Olympiacos Piraeus", "Olympiacos", "Olympiacos"),
    "PAM": ("Valencia Basket", "Valencia", "Valencia"),
    "BES": ("Besiktas Istanbul", "Besiktas", "Besiktas"),
    "VIR": ("Virtus Bologna", "Virtus Bologna", "Virtus"),
    "DUB": ("Dubai Basketball", "Dubai", "Dubai"),
    "ASV": ("LDLC ASVEL Villeurbanne", "LDLC ASVEL", "ASVEL"),
    "PRS": ("Paris Basketball", "Paris", "Paris"),
    "MUN": ("FC Bayern Munich", "Bayern Munich", "Bayern"),
    "PAN": ("Panathinaikos AKTOR Athens", "Panathinaikos", "Panathinaikos"),
    "MAD": ("Real Madrid", "Real Madrid", "Real"),
    "TEL": ("Maccabi Rapyd Tel Aviv", "Maccabi", "Maccabi"),
    "BAS": ("Kosner Baskonia Vitoria-Gasteiz", "Baskonia", "Baskonia"),
    "RED": ("Crvena Zvezda Meridianbet Belgrade", "Crvena Zvezda", "Crvena"),
    "MIL": ("Armani Olimpia Milan", "Milan", "Milan"),
    "IST": ("Anadolu Efes Istanbul", "Anadolu Efes", "Efes"),
    "HTA": ("Hapoel IBI Tel Aviv", "Hapoel TLV", "Hapoel"),
    "ZAL": ("Zalgiris Kaunas", "Zalgiris", "Zalgiris"),
    "ULK": ("Fenerbahce Tarfin Istanbul", "Fenerbahce", "Fenerbahce"),
}
# Le nom sous lequel OddsAPI désigne chacun (flux `events`, 2026-10-01).
NOMS_ODDSAPI = {
    "ASVEL Lyon Villeurbanne": "ASV", "Anadolu Efes": "IST", "Beşiktaş J.K.": "BES",
    "Dubai Basketball": "DUB", "FC Barcelona Bàsquet": "BAR", "FC Bayern München": "MUN",
    "Fenerbahce SK": "ULK", "Hapoel Tel Aviv": "HTA", "KK Crvena zvezda": "RED",
    "KK Partizan NIS": "PAR", "Maccabi Tel Aviv": "TEL", "Olympiacos": "OLY",
    "Pallacanestro Olimpia Milano": "MIL", "Panathinaikos": "PAN",
    "Paris Basketball": "PRS", "Real Madrid": "MAD", "Saski Baskonia": "BAS",
    "Valencia Basket": "PAM", "Virtus Segafredo Bologna": "VIR", "Žalgiris": "ZAL",
}


def _match(local, road, date, played=False, score=(0, 0), phase="Regular Season", ident=None):
    def camp(code, pts):
        nom, court, edito = CLUBS[code]
        return {"club": {"code": code, "name": nom, "abbreviatedName": court,
                         "editorialName": edito}, "score": pts}
    return {"identifier": ident or f"E2026_{local}{road}", "utcDate": date, "played": played,
            "phaseType": {"name": phase}, "local": camp(local, score[0]),
            "road": camp(road, score[1])}


@pytest.fixture(autouse=True)
def _cache_vide():
    ss.reset_cache()
    yield
    ss.reset_cache()


def _servir(monkeypatch, matchs):
    appels = []

    def faux(url, bucket, budget, source=None):
        appels.append((url, bucket, budget))
        return {"data": matchs} if matchs is not None else None
    monkeypatch.setattr(ss, "_get_json", faux)
    return appels


class TestLaSourceEstAuRegistre:
    def test_l_euroleague_ne_passe_plus_par_le_scoreboard_espn(self):
        """ESPN `basketball/euroleague` : 200, zéro événement, sur cinq dates
        dont une de la saison passée (2026-10-01). Si ce test tombe parce que
        la voie est retirée, relever d'abord le critère écrit dans le module
        (règle 13) et sortir la ligue de l'achat."""
        assert ss._ESPN_PATHS["euroleague_basketball"] == [ss.EUROLEAGUE_PATH]
        assert not ss.EUROLEAGUE_PATH.startswith("basketball/")

    def test_la_ligue_payee_a_une_voie_de_reglement(self):
        assert odds_api.SPORT_KEYS["basketball_euroleague"] == "euroleague_basketball"
        assert "euroleague_basketball" in ss.sports_reglables()

    def test_un_budget_chiffre_et_un_seau_propre(self):
        assert 0 < ss.EUROLEAGUE_DAILY_BUDGET <= 200
        assert ss._EL_BUCKET != ss._ESPN_BUCKET

    def test_le_critere_de_retrait_est_ecrit_et_date(self):
        import inspect
        source = inspect.getsource(ss)
        bloc = source[source.index("1ter. EuroLeague"): source.index("def _euroleague_saison")]
        assert "RETRAIT" in bloc and "2026-11-15" in bloc


class TestLaSaison:
    @pytest.mark.parametrize("jour,code", [("20261001", "E2026"), ("20261231", "E2026"),
                                           ("20270416", "E2026"), ("20270731", "E2026"),
                                           ("20270801", "E2027"), ("20270924", "E2027")])
    def test_un_match_d_avril_appartient_a_la_saison_ouverte_en_automne(self, jour, code):
        assert ss._euroleague_saison(jour) == code

    def test_une_requete_pour_tout_le_run(self, monkeypatch):
        appels = _servir(monkeypatch, [_match("PRS", "ZAL", "2026-10-01T18:45:00Z")])
        for jour in ("20260930", "20261001", "20261002", "20270416"):
            ss._espn_jour(ss.EUROLEAGUE_PATH, jour)
        assert len(appels) == 1 and "/E2026/games" in appels[0][0]
        assert appels[0][1] == ss._EL_BUCKET and appels[0][2] == ss.EUROLEAGUE_DAILY_BUDGET

    def test_seuls_les_matchs_du_jour_utc_sont_rendus(self, monkeypatch):
        _servir(monkeypatch, [_match("PRS", "ZAL", "2026-10-01T18:45:00Z"),
                              _match("BES", "BAR", "2026-10-02T17:00:00Z")])
        assert len(ss._espn_jour(ss.EUROLEAGUE_PATH, "20261001")) == 1
        assert len(ss._espn_jour(ss.EUROLEAGUE_PATH, "20261003")) == 0

    def test_une_panne_rend_une_liste_vide_sans_lever(self, monkeypatch):
        _servir(monkeypatch, None)
        assert ss._espn_jour(ss.EUROLEAGUE_PATH, "20261001") == []
        assert ss.fixtures_espn("euroleague_basketball", "2026-10-01", "2026-10-01") == []


def _saison_fictive():
    """Chaque club une fois à domicile : de quoi dériver les noms courts."""
    codes = list(CLUBS)
    return [_match(c, codes[(i + 1) % len(codes)], "2026-10-01T18:00:00Z")
            for i, c in enumerate(codes)]


class TestLesNoms:
    def test_chaque_nom_d_oddsapi_designe_un_seul_club(self):
        """Le tableau réel : 20 noms × 20 clubs, avec exactement les noms que
        l'adaptateur expose."""
        matchs = _saison_fictive()
        courts = ss._euroleague_noms_courts(matchs)
        exposes = {}
        for m in matchs:
            ev = ss._euroleague_evenement(m, courts)
            exposes[m["local"]["club"]["code"]] = ss._espn_noms(
                ev["competitions"][0]["competitors"][0])
        assert len(exposes) == 20
        for nom, attendu in NOMS_ODDSAPI.items():
            trouves = [code for code, noms in exposes.items()
                       if any(ss._apparie(nom, n) for n in noms)]
            assert trouves == [attendu], f"{nom!r} → {trouves}, attendu {attendu}"

    def test_le_plus_court_de_deux_noms_qui_se_ressemblent_n_est_pas_expose(self):
        """« Žalgiris » et « Partizan » s'apparient à « Paris » (ratio 0,62) :
        le piège mesuré. « Paris » sort, les deux autres restent."""
        assert ss._apparie("Žalgiris", "Paris") is True          # le danger existe
        courts = ss._euroleague_noms_courts(_saison_fictive())
        assert courts["PRS"] == []
        assert courts["ZAL"] == ["Zalgiris"] and courts["PAR"] == ["Partizan"]

    def test_les_noms_courts_necessaires_restent(self):
        """Sans eux, le sponsor du nom long empêche l'appariement."""
        courts = ss._euroleague_noms_courts(_saison_fictive())
        assert "Milan" in courts["MIL"] and "ASVEL" in courts["ASV"]
        assert ss._apparie("Pallacanestro Olimpia Milano", "Armani Olimpia Milan") is False
        assert ss._apparie("Pallacanestro Olimpia Milano", "Milan") is True

    def test_aucune_liste_de_clubs_dans_le_module(self):
        """La règle se dérive du calendrier lu : un club promu ou un sponsor
        qui change ne demande aucune édition (règle n°6)."""
        import inspect
        source = inspect.getsource(ss._euroleague_noms_courts)
        assert "Paris" not in source and "Zalgiris" not in source


class TestLeReglement:
    DATE = "2026-09-24T16:00:00+00:00"

    def test_un_match_joue_se_regle_et_dit_sa_source(self, monkeypatch):
        _servir(monkeypatch, [_match("HTA", "MUN", "2026-09-24T16:00:00Z", True, (84, 86))])
        r = ss.result_from_espn("Hapoel Tel Aviv vs FC Bayern München",
                                "euroleague_basketball", self.DATE)
        assert r == {"home_score": 84, "away_score": 86, "completed": True,
                     "source": "euroleague"}

    def test_l_etage_espn_garde_son_etiquette(self, monkeypatch):
        """Les autres sports ne changent pas de source."""
        monkeypatch.setattr(ss, "_get_json", lambda *a, **k: {"events": [{
            "id": "1", "competitions": [{"id": "1", "competitors": [
                {"homeAway": "home", "team": {"displayName": "Boston Celtics"}, "score": "101"},
                {"homeAway": "away", "team": {"displayName": "Miami Heat"}, "score": "99"}],
                "status": {"type": {"completed": True, "state": "post", "name": "STATUS_FINAL"}}}]}]})
        r = ss.result_from_espn("Boston Celtics vs Miami Heat", "basketball", self.DATE)
        assert r and r["source"] == "espn"

    def test_un_match_non_joue_ne_regle_pas(self, monkeypatch):
        _servir(monkeypatch, [_match("PRS", "ZAL", "2026-10-01T18:45:00Z")])
        assert ss.result_from_espn("Paris Basketball vs Žalgiris", "euroleague_basketball",
                                   "2026-10-01T18:45:00+00:00") is None

    def test_played_sans_score_ne_regle_pas(self, monkeypatch):
        """Jamais un 0-0 de match annoncé « joué » avant que le score soit posé."""
        _servir(monkeypatch, [_match("HTA", "MUN", "2026-09-24T16:00:00Z", True, (0, 0))])
        assert ss.result_from_espn("Hapoel Tel Aviv vs FC Bayern München",
                                   "euroleague_basketball", self.DATE) is None

    def test_les_deux_camps_sont_exiges(self, monkeypatch):
        _servir(monkeypatch, [_match("HTA", "MUN", "2026-09-24T16:00:00Z", True, (84, 86))])
        assert ss.result_from_espn("Hapoel Tel Aviv vs Real Madrid",
                                   "euroleague_basketball", self.DATE) is None

    def test_deux_matchs_de_la_meme_paire_font_refuser(self, monkeypatch):
        _servir(monkeypatch, [
            _match("HTA", "MUN", "2026-09-24T16:00:00Z", True, (84, 86), ident="E2026_1"),
            _match("HTA", "MUN", "2026-09-25T16:00:00Z", True, (70, 60), ident="E2026_2")])
        assert ss.result_from_espn("Hapoel Tel Aviv vs FC Bayern München",
                                   "euroleague_basketball", self.DATE) is None


class TestLePerimetre:
    def test_un_match_a_venir_est_reglable_avec_ses_deux_camps(self, monkeypatch):
        _servir(monkeypatch, [_match("PRS", "ZAL", "2026-10-01T18:45:00Z"),
                              _match("VIR", "OLY", "2026-10-01T18:30:00Z")])
        fx = {"euroleague_basketball":
              ss.fixtures_espn("euroleague_basketball", "2026-10-01", "2026-10-01")}
        assert len(fx["euroleague_basketball"]) == 2

        def m(nom):
            return {"match": nom, "sport": "euroleague_basketball",
                    "league": "Basketball Euroleague"}
        assert eng._reglable(m("Paris Basketball vs Žalgiris"), fx) is True
        assert eng._reglable(m("Virtus Segafredo Bologna vs Olympiacos"), fx) is True
        # Un seul camp connu (affiche qui n'existe pas ce jour-là) : refus.
        assert eng._reglable(m("Paris Basketball vs Olympiacos"), fx) is False

    def test_la_phase_finale_n_est_pas_prise_pour_la_saison_reguliere(self):
        ev = ss._euroleague_evenement(_match("PRS", "ZAL", "2027-05-01T18:00:00Z",
                                             phase="Playoffs"))
        assert ev["season"]["type"] == 3
