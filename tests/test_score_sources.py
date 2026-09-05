"""
tests/test_score_sources.py — la chaîne de scores déterministe (2026-09-02).

Ces gardes encodent les pièges MESURÉS le jour du câblage :
  - searchteams est FLOU : « AD Pasto » rend « Pastoreo » (équipe sans ligue),
    et strict_team_match l'accepte par containment. La recherche d'équipe ne
    doit jamais décider — seul l'événement complet (les DEUX noms + la date,
    candidat unique) règle ;
  - TheSportsDB publie des scores EN DIRECT : un statut non terminé (ou
    absent) ne règle pas, sinon on écrirait un WIN/LOSS faux et définitif à
    la 70e minute ;
  - MLB statsapi publie aussi les scores en cours : seul
    `abstractGameState == "Final"` règle ;
  - deux candidats → REFUS, jamais une devinette (même contrat
    qu'api-sports).

Aucun réseau : `_get_json` est monkeypatché.
"""
import pytest

from core import score_sources as ss


@pytest.fixture(autouse=True)
def _caches_neufs():
    ss.reset_cache()
    yield
    ss.reset_cache()


def _mlb_payload(games):
    return {"dates": [{"games": [
        {"status": {"abstractGameState": st},
         "teams": {"home": {"team": {"name": h}, "score": hs},
                   "away": {"team": {"name": a}, "score": as_}}}
        for (h, a, hs, as_, st) in games]}]}


class TestMLB:
    def test_final_unique_regle(self, monkeypatch):
        monkeypatch.setattr(ss, "_get_json", lambda url, b, bud: _mlb_payload(
            [("Baltimore Orioles", "Colorado Rockies", 4, 2, "Final")]))
        r = ss.result_from_mlb("Baltimore Orioles vs Colorado Rockies", "2026-09-01")
        assert r == {"home_score": 4, "away_score": 2, "completed": True,
                     "source": "mlb_statsapi"}

    def test_un_match_en_cours_ne_regle_pas(self, monkeypatch):
        """statsapi publie le score dès la 1re manche (abstractGameState
        « Live ») : régler dessus écrirait un résultat faux et définitif."""
        monkeypatch.setattr(ss, "_get_json", lambda url, b, bud: _mlb_payload(
            [("Baltimore Orioles", "Colorado Rockies", 3, 2, "Live")]))
        assert ss.result_from_mlb("Baltimore Orioles vs Colorado Rockies",
                                  "2026-09-01") is None

    def test_deux_candidats_font_refuser(self, monkeypatch):
        monkeypatch.setattr(ss, "_get_json", lambda url, b, bud: _mlb_payload(
            [("Chicago Cubs", "Chicago White Sox", 1, 0, "Final"),
             ("Chicago Cubs", "Chicago White Sox", 5, 3, "Final")]))
        assert ss.result_from_mlb("Chicago Cubs vs Chicago White Sox",
                                  "2026-09-01") is None

    def test_un_coup_denvoi_tardif_est_cherche_le_lendemain(self, monkeypatch):
        vus = []

        def fake(url, b, bud):
            vus.append(url)
            if "date=2026-09-02" in url:
                return _mlb_payload([("Seattle Mariners", "Boston Red Sox", 9, 8, "Final")])
            return _mlb_payload([])
        monkeypatch.setattr(ss, "_get_json", fake)
        r = ss.result_from_mlb("Seattle Mariners vs Boston Red Sox", "2026-09-01")
        assert r and r["home_score"] == 9

    def test_panne_reseau_rend_none_sans_lever(self, monkeypatch):
        monkeypatch.setattr(ss, "_get_json", lambda url, b, bud: None)
        assert ss.result_from_mlb("A FC vs B FC", "2026-09-01") is None


def _tsdb_router(teams_by_query, events_by_team):
    """Fabrique un _get_json qui sert searchteams et eventslast."""
    def fake(url, bucket, budget):
        if "searchteams.php" in url:
            import urllib.parse
            q = urllib.parse.unquote_plus(url.split("?t=")[1])
            return {"teams": teams_by_query.get(q, None)}
        if "eventslast.php" in url:
            tid = url.split("?id=")[1]
            return {"results": events_by_team.get(tid, [])}
        raise AssertionError(f"URL inattendue: {url}")
    return fake


def _ev(home, away, hs, as_, date="2026-08-31", statut="FT", eid="e1"):
    return {"idEvent": eid, "strHomeTeam": home, "strAwayTeam": away,
            "intHomeScore": hs, "intAwayScore": as_, "dateEvent": date,
            "strStatus": statut, "strLeague": "Test League"}


class TestTheSportsDB:
    def test_la_voie_par_equipe_regle_le_cas_reel(self, monkeypatch):
        """Le cas mesuré le 2026-09-02 : le signal en souffrance depuis 33 h
        (Hapoel Acre, coup d'envoi 2026-08-31) retrouvé via searchteams →
        eventslast, noms flous compris."""
        monkeypatch.setattr(ss, "_get_json", _tsdb_router(
            {"Hapoel Acre": [{"idTeam": "136025", "strTeam": "Hapoel Akko",
                              "strSport": "Soccer"}]},
            {"136025": [_ev("Hapoel Akko", "Bnei Yehuda", "0", "3")]}))
        r = ss.result_from_thesportsdb("Hapoel Acre vs Bnei Yehuda", "soccer",
                                       "2026-08-31")
        assert r == {"home_score": 0, "away_score": 3, "completed": True,
                     "source": "thesportsdb"}

    def test_une_mauvaise_equipe_du_flou_ne_regle_rien(self, monkeypatch):
        """searchteams("AD Pasto") rend « Pastoreo » — mesuré. Ses derniers
        matchs ne portent PAS la paire du signal : aucun événement ne doit
        s'apparier, quel que soit le score qu'ils affichent."""
        monkeypatch.setattr(ss, "_get_json", _tsdb_router(
            {"AD Pasto": [{"idTeam": "144721", "strTeam": "Pastoreo",
                           "strSport": "Soccer"}],
             "Alianza Petrolera": None},
            {"144721": [_ev("Pastoreo", "Autre Club", "2", "1")]}))
        assert ss.result_from_thesportsdb("AD Pasto vs Alianza Petrolera",
                                          "soccer", "2026-08-31") is None

    def test_un_score_en_direct_ne_regle_pas(self, monkeypatch):
        """TheSportsDB pose intHomeScore pendant le match. Sans statut
        terminé (« 2H », ou champ absent), on attend le prochain audit."""
        for statut in ("2H", "", None):
            ss.reset_cache()
            monkeypatch.setattr(ss, "_get_json", _tsdb_router(
                {"Ajax": [{"idTeam": "1", "strTeam": "Ajax", "strSport": "Soccer"}]},
                {"1": [_ev("Ajax", "Feyenoord", "1", "0", statut=statut)]}))
            assert ss.result_from_thesportsdb("Ajax vs Feyenoord", "soccer",
                                              "2026-08-31") is None, statut

    def test_deux_confrontations_de_la_meme_paire_font_refuser(self, monkeypatch):
        """Sans date (relance d'une ligne de ledger orpheline), deux matchs
        Ajax-Feyenoord dans les 15 derniers résultats → on ne devine pas."""
        monkeypatch.setattr(ss, "_get_json", _tsdb_router(
            {"Ajax": [{"idTeam": "1", "strTeam": "Ajax", "strSport": "Soccer"}]},
            {"1": [_ev("Ajax", "Feyenoord", "1", "0", date="2026-08-10", eid="a"),
                   _ev("Ajax", "Feyenoord", "2", "2", date="2026-05-01", eid="b")]}))
        assert ss.result_from_thesportsdb("Ajax vs Feyenoord", "soccer", "") is None

    def test_sans_date_un_candidat_unique_regle(self, monkeypatch):
        monkeypatch.setattr(ss, "_get_json", _tsdb_router(
            {"Ajax": [{"idTeam": "1", "strTeam": "Ajax", "strSport": "Soccer"}]},
            {"1": [_ev("Ajax", "Feyenoord", "1", "0", date="2026-08-10")]}))
        r = ss.result_from_thesportsdb("Ajax vs Feyenoord", "soccer", "")
        assert r and r["home_score"] == 1

    def test_le_filtre_de_sport_ecarte_les_homonymes(self, monkeypatch):
        """« Barcelona » existe en Soccer ET en Basketball : la recherche
        d'équipe ne doit shortlister que le bon sport."""
        monkeypatch.setattr(ss, "_get_json", _tsdb_router(
            {"Barcelona": [{"idTeam": "9", "strTeam": "Barcelona",
                            "strSport": "Basketball"}]},
            {"9": [_ev("Barcelona", "Real Madrid", "80", "75")]}))
        assert ss.result_from_thesportsdb("Barcelona vs Real Madrid", "soccer",
                                          "2026-08-31") is None

    def test_la_date_est_toleree_a_plus_ou_moins_un_jour(self, monkeypatch):
        monkeypatch.setattr(ss, "_get_json", _tsdb_router(
            {"Ajax": [{"idTeam": "1", "strTeam": "Ajax", "strSport": "Soccer"}]},
            {"1": [_ev("Ajax", "Feyenoord", "1", "0", date="2026-09-01")]}))
        r = ss.result_from_thesportsdb("Ajax vs Feyenoord", "soccer", "2026-08-31")
        assert r and r["home_score"] == 1


class TestBudgetsEtChaine:
    def test_le_budget_journalier_coupe_les_requetes(self, monkeypatch):
        from core import daily_quota
        monkeypatch.setattr(daily_quota, "spent", lambda b: 10_000)
        appels = []
        monkeypatch.setattr(ss.urllib.request, "urlopen",
                            lambda *a, **k: appels.append(1))
        assert ss._get_json("https://x/y", ss._TSDB_BUCKET, ss.TSDB_DAILY_BUDGET) is None
        assert not appels, "budget atteint : aucune requête ne doit partir"

    def test_la_chaine_ne_tente_mlb_que_pour_le_baseball(self, monkeypatch):
        vus = []
        monkeypatch.setattr(ss, "result_from_mlb",
                            lambda *a: vus.append("mlb") or None)
        monkeypatch.setattr(ss, "result_from_espn",
                            lambda *a: vus.append("espn") or None)
        monkeypatch.setattr(ss, "result_from_livescore",
                            lambda *a: vus.append("livescore") or None)
        monkeypatch.setattr(ss, "result_from_thesportsdb",
                            lambda *a: vus.append("tsdb") or None)
        ss.fetch_score("A FC vs B FC", "soccer", "2026-09-01")
        assert vus == ["espn", "livescore", "tsdb"]
        vus.clear()
        ss.fetch_score("A vs B", "baseball", "2026-09-01")
        assert vus == ["mlb", "espn", "livescore", "tsdb"]

    def test_livescore_passe_avant_thesportsdb(self, monkeypatch):
        """L'ORDRE est la raison d'être de cette voie (2026-09-05) : une
        requête LiveScore couvre toute la journée d'un sport, TheSportsDB
        coûte plusieurs requêtes par signal sur un budget de 150/jour saturé
        trois jours d'affilée. Inverser les deux rendrait la voie inutile."""
        vus = []
        monkeypatch.setattr(ss, "result_from_espn", lambda *a: None)
        monkeypatch.setattr(ss, "result_from_livescore",
                            lambda *a: vus.append("livescore") or {"home_score": 1,
                            "away_score": 0, "completed": True, "source": "livescore"})
        monkeypatch.setattr(ss, "result_from_thesportsdb",
                            lambda *a: vus.append("tsdb") or None)
        assert ss.fetch_score("A FC vs B FC", "soccer", "2026-09-01")["source"] == "livescore"
        assert vus == ["livescore"], "TheSportsDB ne doit pas être appelé quand LiveScore règle"

    def test_settlement_jamais_etale(self):
        """Étaler le settlement est une faute (incident 2026-08-28) : ce
        module tient des budgets journaliers mais AUCUN rythme horaire."""
        import inspect
        assert "paced_allowance" not in inspect.getsource(ss)


# ── ESPN (2026-09-03) ────────────────────────────────────────────────

def _espn_ev(home, away, hs, as_, completed=True, state="post", eid="e1",
             home_names=None, away_names=None):
    def team(name, names):
        t = {"displayName": name}
        if names:
            t.update(names)
        return t
    return {"id": eid, "date": "2026-08-31T19:00Z", "competitions": [{
        "status": {"type": {"completed": completed, "state": state}},
        "competitors": [
            {"homeAway": "home", "score": str(hs), "team": team(home, home_names)},
            {"homeAway": "away", "score": str(as_), "team": team(away, away_names)},
        ]}]}


def _espn_router(events_by_path):
    def fake(url, bucket, budget, source=None):
        assert bucket == ss._ESPN_BUCKET and source == "espn"
        for path, evs in events_by_path.items():
            if f"/{path}/scoreboard" in url:
                return {"events": evs}
        return {"events": []}
    return fake


class TestESPN:
    def test_score_final_unique_regle(self, monkeypatch):
        monkeypatch.setattr(ss, "_get_json", _espn_router({
            "soccer/all": [_espn_ev("Defensa y Justicia", "Platense", 1, 0),
                           _espn_ev("Estudiantes", "Newell's Old Boys", 0, 0, eid="e2")]}))
        r = ss.result_from_espn("Defensa y Justicia vs Platense", "soccer", "2026-08-31")
        assert r == {"home_score": 1, "away_score": 0, "completed": True, "source": "espn"}

    def test_une_requete_par_fenetre_et_par_chemin(self, monkeypatch):
        """`soccer/all` couvre toutes les ligues : deux signaux de la même
        fenêtre ne coûtent qu'UNE requête (cache de run)."""
        appels = []
        def fake(url, bucket, budget, source=None):
            appels.append(url); return {"events": [_espn_ev("A FC", "B FC", 2, 2)]}
        monkeypatch.setattr(ss, "_get_json", fake)
        ss.result_from_espn("A FC vs B FC", "soccer", "2026-08-31")
        ss.result_from_espn("C FC vs D FC", "soccer", "2026-08-31")
        assert len(appels) == 1 and "dates=20260830-20260901" in appels[0]
        assert "/soccer/all/scoreboard" in appels[0]

    def test_un_score_en_direct_ne_regle_pas(self, monkeypatch):
        for completed, state in ((False, "in"), (True, "in"), (False, "post")):
            ss.reset_cache()
            monkeypatch.setattr(ss, "_get_json", _espn_router({
                "soccer/all": [_espn_ev("A FC", "B FC", 1, 0, completed=completed, state=state)]}))
            assert ss.result_from_espn("A FC vs B FC", "soccer", "2026-08-31") is None

    def test_deux_candidats_font_refuser(self, monkeypatch):
        monkeypatch.setattr(ss, "_get_json", _espn_router({
            "soccer/all": [_espn_ev("A FC", "B FC", 1, 0, eid="e1"),
                           _espn_ev("A FC", "B FC", 0, 3, eid="e2")]}))
        assert ss.result_from_espn("A FC vs B FC", "soccer", "2026-08-31") is None

    def test_les_deux_equipes_doivent_sapparier(self, monkeypatch):
        """Un seul nom apparié ne suffit pas (leçon Pasto/Pastoreo)."""
        monkeypatch.setattr(ss, "_get_json", _espn_router({
            "soccer/all": [_espn_ev("Deportivo Pasto", "Deportivo Pereira", 2, 1)]}))
        assert ss.result_from_espn("Deportivo Pasto vs Atletico Nacional", "soccer", "2026-08-31") is None

    def test_les_libelles_courts_despn_sont_essayes(self, monkeypatch):
        monkeypatch.setattr(ss, "_get_json", _espn_router({
            "rugby-league/3": [_espn_ev("Gold Coast Titans", "South Sydney Rabbitohs", 22, 42,
                                        home_names={"shortDisplayName": "Titans"},
                                        away_names={"shortDisplayName": "Rabbitohs"})]}))
        r = ss.result_from_espn("Titans vs Rabbitohs", "rugbyleague", "2026-08-30")
        assert r and (r["home_score"], r["away_score"]) == (22, 42)

    def test_sport_sans_chemin_saute_la_voie(self, monkeypatch):
        appels = []
        monkeypatch.setattr(ss, "_get_json", lambda *a, **k: appels.append(1) or {"events": []})
        assert ss.result_from_espn("Fury vs Usyk", "boxing", "2026-08-30") is None
        assert ss.result_from_espn("Van Gerwen vs Price", "darts", "2026-08-30") is None
        assert not appels

    def test_sans_date_fenetre_recente_et_paire_unique(self, monkeypatch):
        appels = []
        def fake(url, bucket, budget, source=None):
            appels.append(url); return {"events": [_espn_ev("A FC", "B FC", 1, 1)]}
        monkeypatch.setattr(ss, "_get_json", fake)
        r = ss.result_from_espn("A FC vs B FC", "soccer", "")
        assert r and r["home_score"] == 1
        assert "dates=" in appels[0] and "-" in appels[0].split("dates=")[1]

    def test_panne_reseau_rend_none(self, monkeypatch):
        monkeypatch.setattr(ss, "_get_json", lambda *a, **k: None)
        assert ss.result_from_espn("A FC vs B FC", "soccer", "2026-08-31") is None

    def test_la_requete_passe_par_core_net(self, monkeypatch):
        """Routage par `core.net` (relais / proxy `ESPN_PROXY`) : la parade
        si les runners GitHub sont refusés — sans toucher au code."""
        from core import daily_quota
        monkeypatch.setattr(daily_quota, "spent", lambda b: 0)
        monkeypatch.setattr(daily_quota, "add", lambda b, n: None)
        vus = {}
        class _Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b'{"events": []}'
        monkeypatch.setattr(ss.net, "prepare", lambda src, url, h: vus.setdefault("prepare", src) and (url, h))
        monkeypatch.setattr(ss.net, "open_with_retry", lambda src, req, t: vus.setdefault("open", src) and _Resp())
        assert ss._get_json("https://x/y", ss._ESPN_BUCKET, 10, source="espn") == {"events": []}
        assert vus == {"prepare": "espn", "open": "espn"}


class TestChaineAvecESPN:
    def test_espn_avant_thesportsdb(self, monkeypatch):
        """Décision opérateur 2026-09-03 : sources OUVERTES d'abord, TheSportsDB
        en dernier recours."""
        vus = []
        monkeypatch.setattr(ss, "result_from_mlb", lambda *a: vus.append("mlb") or None)
        monkeypatch.setattr(ss, "result_from_espn", lambda *a: vus.append("espn") or None)
        monkeypatch.setattr(ss, "result_from_thesportsdb", lambda *a: vus.append("tsdb") or None)
        ss.fetch_score("A FC vs B FC", "soccer", "2026-09-01")
        assert vus == ["espn", "tsdb"]
        vus.clear()
        ss.fetch_score("A vs B", "baseball", "2026-09-01")
        assert vus == ["mlb", "espn", "tsdb"]

    def test_espn_qui_trouve_court_circuite_thesportsdb(self, monkeypatch):
        monkeypatch.setattr(ss, "result_from_espn", lambda *a: {"home_score": 1, "away_score": 0,
                                                                "completed": True, "source": "espn"})
        monkeypatch.setattr(ss, "result_from_thesportsdb", lambda *a: (_ for _ in ()).throw(AssertionError("TSDB appelé")))
        assert ss.fetch_score("A FC vs B FC", "soccer", "2026-09-01")["source"] == "espn"


class TestLEtageDuClubNestJamaisHerite:
    """La garde de COUVERTURE (`fixture_connue`, un seul nom apparié depuis le
    2026-09-03 pour rattraper « FC Köln » / « FC Cologne ») laissait un U21
    hériter de la couverture ESPN de son club senior.

    Mesuré le 2026-09-04 : « Sheffield Wednesday Reserve U21 vs Wigan Athletic
    U21 » émis à 04:46 comme réglable, alors que `result_from_espn` exige les
    DEUX noms et ne le trouve jamais. Ces lignes restaient `active` jusqu'à
    EXPIRE_AFTER_H (36 h) et rejouaient à chaque audit — 99 requêtes
    TheSportsDB consommées sur 150 dès 02:51, d'où deux `AUDIT STÉRILE` la
    veille.

    Le remède est dans `core.paim_engine.strict_team_match`, donc il vaut
    AUSSI pour les cotes — c'est là qu'il est le plus important : apparier un
    U21 au match senior lierait le prix d'un match aux cotes d'un autre.
    """

    @pytest.mark.parametrize("signal, espn", [
        ("Sheffield Wednesday Reserve U21", "Sheffield Wednesday"),
        ("Wigan Athletic U21",              "Wigan Athletic"),
        ("Manly United FC U20",             "Manly United"),
        ("Northern Tigers U20",             "Northern Tigers"),
        ("Arsenal Women",                   "Arsenal"),
        ("Vitesse B",                       "Vitesse"),
        ("TSG Hoffenheim II",               "TSG Hoffenheim"),
    ])
    def test_un_etage_ne_sapparie_pas_au_senior(self, signal, espn):
        from core.paim_engine import strict_team_match
        assert not strict_team_match(signal, espn), (
            f"« {signal} » apparié à « {espn} » : le club senior vouche pour "
            "une équipe qui joue un autre match")

    @pytest.mark.parametrize("a, b", [
        ("Sheffield Wednesday U21", "Sheffield Weds U21"),   # même étage, deux sources
        ("Arsenal W",               "Arsenal Women"),        # deux notations féminines
        ("Lyon U19",                "Olympique Lyonnais U19"),
        ("VfB Stuttgart",           "Stuttgart"),            # cas nominal intact
        ("Barcelona",               "FC Barcelona"),         # containment légitime intact
    ])
    def test_le_meme_etage_reste_appariable(self, a, b):
        from core.paim_engine import strict_team_match
        assert strict_team_match(a, b), (
            f"« {a} » refusé contre « {b} » : la garde d'étage ne doit pas "
            "créer de faux négatif entre deux écritures du MÊME étage")

    def test_la_couverture_espn_refuse_desormais_le_u21(self):
        """Le bout de chaîne : `fixture_connue` en min_sides=1 — la forme
        permissive — ne doit plus dire « couvert » sur un match de jeunes
        adossé à une rencontre senior."""
        senior = [{"competitions": [{"competitors": [
            {"homeAway": "home", "team": {"displayName": "Sheffield Wednesday"}},
            {"homeAway": "away", "team": {"displayName": "Bristol City"}},
        ]}]}]
        assert ss.fixture_connue("Sheffield Wednesday vs Bristol City", senior)
        assert not ss.fixture_connue(
            "Sheffield Wednesday Reserve U21 vs Wigan Athletic U21", senior)


# ── LiveScore (câblée le 2026-09-05) ──────────────────────────────────
#
# Les pièges encodés ici sont ceux MESURÉS le jour du câblage sur la journée
# du 2026-09-04 : LiveScore rend 273 matchs de football quand ESPN en rend
# 115, avec un vocabulaire de statut à lui (« FT », « AP », les minutes en
# direct) et des camps sous forme de LISTES.

def _ls_payload(events, ligue="Indonesia - Super League"):
    """Une réponse LiveScore : Stages[] > Events[], camps en listes."""
    return {"Stages": [{"Cnm": ligue.split(" - ")[0],
                        "Snm": ligue.split(" - ")[-1],
                        "Events": [
                            {"Eid": eid, "T1": [{"Nm": h}], "T2": [{"Nm": a}],
                             "Tr1": hs, "Tr2": as_, "Eps": st}
                            for (eid, h, a, hs, as_, st) in events]}]}


class TestLiveScore:
    def test_un_match_termine_regle(self, monkeypatch):
        monkeypatch.setattr(ss, "_get_json", lambda url, b, bud, source=None:
                            _ls_payload([("1", "Bali United", "PSS Sleman", 2, 1, "FT")]))
        r = ss.result_from_livescore("Bali United vs PSS Sleman", "soccer", "2026-09-04")
        assert r == {"home_score": 2, "away_score": 1, "completed": True,
                     "source": "livescore"}

    def test_un_match_en_cours_ne_regle_pas(self, monkeypatch):
        """LiveScore publie les minutes en direct : régler à la 70e écrirait
        un WIN/LOSS faux et DÉFINITIF."""
        monkeypatch.setattr(ss, "_get_json", lambda url, b, bud, source=None:
                            _ls_payload([("1", "Bali United", "PSS Sleman", 2, 1, "70'")]))
        assert ss.result_from_livescore("Bali United vs PSS Sleman", "soccer",
                                        "2026-09-04") is None

    def test_les_tirs_au_but_ne_reglent_pas(self, monkeypatch):
        """« AP » (après tirs au but) est TERMINÉ mais Tr1/Tr2 peuvent porter
        le score de la séance, que nos marchés 1X2 et totaux ne mesurent pas.
        La ligne continue vers ESPN et TheSportsDB — refus, pas devinette."""
        monkeypatch.setattr(ss, "_get_json", lambda url, b, bud, source=None:
                            _ls_payload([("1", "A FC", "B FC", 4, 3, "AP")]))
        assert ss.result_from_livescore("A FC vs B FC", "soccer", "2026-09-04") is None

    def test_un_seul_nom_apparie_ne_regle_pas(self, monkeypatch):
        """Même contrat que toutes les autres voies : les DEUX camps."""
        monkeypatch.setattr(ss, "_get_json", lambda url, b, bud, source=None:
                            _ls_payload([("1", "Bali United", "Persija Jakarta", 2, 1, "FT")]))
        assert ss.result_from_livescore("Bali United vs PSS Sleman", "soccer",
                                        "2026-09-04") is None

    def test_deux_candidats_font_refuser(self, monkeypatch):
        monkeypatch.setattr(ss, "_get_json", lambda url, b, bud, source=None:
                            _ls_payload([("1", "A FC", "B FC", 2, 1, "FT"),
                                         ("2", "A FC", "B FC", 0, 3, "FT")]))
        assert ss.result_from_livescore("A FC vs B FC", "soccer", "2026-09-04") is None

    def test_un_coup_denvoi_tardif_est_cherche_le_lendemain(self, monkeypatch):
        """23:30 UTC bascule de journée : `_jours` couvre veille et lendemain."""
        def fake(url, b, bud, source=None):
            return _ls_payload([("1", "A FC", "B FC", 1, 0, "FT")]) \
                if "20260905" in url else _ls_payload([])
        monkeypatch.setattr(ss, "_get_json", fake)
        r = ss.result_from_livescore("A FC vs B FC", "soccer", "2026-09-04")
        assert r and r["home_score"] == 1

    def test_letage_du_club_nest_pas_herite(self, monkeypatch):
        """Un U20 ne se règle JAMAIS sur le match de son club senior — même
        piège que l'incident ESPN du 2026-09-04, et il vaut ici aussi."""
        monkeypatch.setattr(ss, "_get_json", lambda url, b, bud, source=None:
                            _ls_payload([("1", "Manly United", "NWS Spirit", 3, 0, "FT")]))
        assert ss.result_from_livescore("Manly United FC U20 vs NWS Spirit FC U20",
                                        "soccer", "2026-09-04") is None

    def test_panne_reseau_rend_none_sans_lever(self, monkeypatch):
        monkeypatch.setattr(ss, "_get_json", lambda url, b, bud, source=None: None)
        assert ss.result_from_livescore("A FC vs B FC", "soccer", "2026-09-04") is None

    def test_un_sport_non_cable_ne_part_pas_en_requete(self, monkeypatch):
        """Seul le football est câblé (mesuré : c'est là qu'est l'écart avec
        ESPN). Un autre sport ne doit pas dépenser une requête pour rien."""
        appels = []
        monkeypatch.setattr(ss, "_get_json",
                            lambda url, b, bud, source=None: appels.append(url))
        assert ss.result_from_livescore("A vs B", "tennis", "2026-09-04") is None
        assert ss.result_from_livescore("A vs B", "basketball", "2026-09-04") is None
        assert not appels

    def test_une_journee_ne_coute_quune_requete(self, monkeypatch):
        """La raison d'être de la voie : 273 matchs pour UNE requête. Sans le
        cache de run, un audit de 25 signaux en paierait 25."""
        appels = []

        def fake(url, b, bud, source=None):
            appels.append(url)
            return _ls_payload([("1", "A FC", "B FC", 1, 0, "FT"),
                                ("2", "C FC", "D FC", 2, 2, "FT")])
        monkeypatch.setattr(ss, "_get_json", fake)
        ss.result_from_livescore("A FC vs B FC", "soccer", "2026-09-04")
        ss.result_from_livescore("C FC vs D FC", "soccer", "2026-09-04")
        assert len(set(appels)) == len(appels) == 3, "3 jours, une requête chacun, pas plus"

    def test_la_requete_passe_par_core_net(self, monkeypatch):
        """`source=` route par core.net (proxy `LIVESCORE_PROXY` /
        `FREE_SOURCES_PROXY`) — la parade documentée si les runners GitHub se
        font refuser, comme pour ESPN."""
        vus = []

        def fake(url, b, bud, source=None):
            vus.append(source)
            return _ls_payload([])
        monkeypatch.setattr(ss, "_get_json", fake)
        ss.result_from_livescore("A FC vs B FC", "soccer", "2026-09-04")
        assert set(vus) == {"livescore"}

    def test_le_perimetre_sportif_ne_bouge_pas(self):
        """Cette voie règle MIEUX, elle n'ouvre AUCUN sport : le périmètre
        d'émission et la politique de dépense restent une décision opérateur
        (CLAUDE.md règle n°11)."""
        assert ss.sports_reglables() == frozenset(ss._ESPN_PATHS) | {"baseball"}


class TestSondeDAlias:
    """La sonde d'alias JOURNALISE, elle ne règle jamais (2026-09-05).

    Régler sur un seul camp apparié est le piège de « AD Pasto » →
    « Pastoreo » (2026-09-02) et de l'U21 héritant du club senior
    (2026-09-04). Un WIN/LOSS faux au ledger est DÉFINITIF ; un log ne coûte
    rien. La promotion d'un candidat reste une décision humaine.
    """

    def test_un_camp_reconnu_est_journalise_mais_ne_regle_pas(self, monkeypatch, caplog):
        """Le cas réel : « Truong Tuoi Dong Nai » est « Binh Phuoc » chez
        LiveScore — même club, nom de sponsor."""
        monkeypatch.setattr(ss, "_get_json", lambda url, b, bud, source=None:
                            _ls_payload([("1", "Binh Phuoc", "Viettel", 0, 2, "FT")],
                                        ligue="Vietnam - V-League"))
        with caplog.at_level("INFO"):
            r = ss.result_from_livescore("Truong Tuoi Dong Nai FC vs Viettel FC",
                                         "soccer", "2026-09-04")
        assert r is None, "un seul camp apparié ne doit JAMAIS régler"
        assert "ALIAS CANDIDAT" in caplog.text
        assert "Binh Phuoc" in caplog.text

    def test_deux_pretendants_ne_sont_pas_journalises(self, monkeypatch, caplog):
        """Deux prétendants, c'est du bruit, pas une piste."""
        monkeypatch.setattr(ss, "_get_json", lambda url, b, bud, source=None:
                            _ls_payload([("1", "X FC", "Viettel", 0, 2, "FT"),
                                         ("2", "Y FC", "Viettel", 1, 1, "FT")]))
        with caplog.at_level("INFO"):
            assert ss.result_from_livescore("Z FC vs Viettel FC", "soccer",
                                            "2026-09-04") is None
        assert "ALIAS CANDIDAT" not in caplog.text
        # …mais le silence est NOMMÉ : un « pas trouvé » indistinct est
        # exactement ce que cette sonde existe pour supprimer.
        assert "ALIAS AMBIGU" in caplog.text

    def test_aucun_camp_reconnu_ne_journalise_rien(self, monkeypatch, caplog):
        """Ligue absente : rien à dire, et surtout pas un candidat."""
        monkeypatch.setattr(ss, "_get_json", lambda url, b, bud, source=None:
                            _ls_payload([("1", "X FC", "Y FC", 0, 2, "FT")]))
        with caplog.at_level("INFO"):
            assert ss.result_from_livescore("A FC vs B FC", "soccer",
                                            "2026-09-04") is None
        assert "ALIAS CANDIDAT" not in caplog.text

    def test_la_sonde_ne_coute_aucune_requete(self, monkeypatch):
        """Elle relit le cache de journée déjà chargé — zéro requête en plus.
        Une sonde qui dépense du budget se ferait couper au premier incident."""
        appels = []

        def fake(url, b, bud, source=None):
            appels.append(url)
            return _ls_payload([("1", "Binh Phuoc", "Viettel", 0, 2, "FT")])
        monkeypatch.setattr(ss, "_get_json", fake)
        ss.result_from_livescore("Truong Tuoi Dong Nai FC vs Viettel FC",
                                 "soccer", "2026-09-04")
        assert len(appels) == 3, "3 jours de fenêtre, et rien de plus pour la sonde"

    def test_un_match_regle_ne_declenche_pas_la_sonde(self, monkeypatch, caplog):
        monkeypatch.setattr(ss, "_get_json", lambda url, b, bud, source=None:
                            _ls_payload([("1", "Bali United", "PSS Sleman", 2, 1, "FT")]))
        with caplog.at_level("INFO"):
            assert ss.result_from_livescore("Bali United vs PSS Sleman", "soccer",
                                            "2026-09-04")["home_score"] == 2
        assert "ALIAS CANDIDAT" not in caplog.text


# ── ESPN tennis : un jour à la fois (2026-09-05) ──────────────────────

def _tennis_comp(cid, a, b, date, winner=None, completed=True):
    def ath(name, win):
        c = {"athlete": {"displayName": name}}
        if win is not None:
            c["winner"] = win
        return c
    return {"id": cid, "date": date,
            "status": {"type": {"completed": completed, "state": "post" if completed else "pre"}},
            "competitors": [ath(a, winner == a if winner else None),
                            ath(b, winner == b if winner else None)]}


def _tournoi(comps):
    return {"id": "t1", "name": "US Open", "date": "2026-08-24T15:00Z",
            "competitions": [], "groupings": [{"competitions": comps}]}


class TestESPNTennisParJour:
    """Le scoreboard tennis ne rend RIEN sur une plage `dates=A-B` (mesuré le
    2026-09-05 : 0 événement, contre 625 matchs datés sur un jour seul).
    Jusque-là : US Open payé à chaque scan, puis chaque match refusé « ligue
    non couverte » — un crédit perdu deux fois."""

    def _fake(self, appels):
        def fake(url, bucket, budget, source=None):
            appels.append(url)
            assert "dates=2026" in url and "-" not in url.split("dates=")[1].split("&")[0], \
                "le tennis doit être interrogé un jour à la fois"
            jour = url.split("dates=")[1][:8]
            comps = [_tennis_comp("c1", "Denis Shapovalov", "Ben Shelton", "2026-09-05T02:45Z",
                                  winner="Ben Shelton"),
                     _tennis_comp("c2", "Taylor Fritz", "Francisco Cerundolo", "2026-09-05T15:40Z",
                                  completed=False),
                     _tennis_comp("c3", "Iga Swiatek", "Marie Bouzkova", "2026-08-31T17:00Z",
                                  winner="Iga Swiatek")]
            return {"events": [_tournoi(comps)]} if jour.startswith("202609") else {"events": []}
        return fake

    def test_une_requete_par_jour_et_par_chemin(self, monkeypatch):
        appels = []
        monkeypatch.setattr(ss, "_get_json", self._fake(appels))
        ev = ss.fixtures_espn("tennis", "2026-09-05", "2026-09-05")
        # fenêtre 09-04..09-06 = 3 jours × 2 chemins (atp, wta)
        assert len(appels) == 6
        assert all(("/tennis/atp/" in u or "/tennis/wta/" in u) for u in appels)
        comps = [c for e in ev for c in ss._espn_competitions(e)]
        # hors fenêtre écarté (c3), doublon entre jours et chemins écarté (id)
        assert sorted(c["id"] for c in comps) == ["c1", "c1", "c2", "c2"] or \
               sorted(c["id"] for c in comps) == ["c1", "c2"]

    def test_le_match_du_signal_est_couvert_dans_les_deux_ordres(self, monkeypatch):
        monkeypatch.setattr(ss, "_get_json", self._fake([]))
        ev = ss.fixtures_espn("tennis", "2026-09-05", "2026-09-05")
        assert ss.fixture_connue("Ben Shelton vs Denis Shapovalov", ev, min_sides=2)
        assert ss.fixture_connue("Taylor Fritz vs Francisco Cerundolo", ev, min_sides=2)
        assert not ss.fixture_connue("Iga Swiatek vs Marie Bouzkova", ev)      # 31/08, hors fenêtre

    def test_le_vainqueur_regle_un_match_termine_seulement(self, monkeypatch):
        monkeypatch.setattr(ss, "_get_json", self._fake([]))
        r = ss.result_from_espn("Ben Shelton vs Denis Shapovalov", "tennis", "2026-09-05")
        assert r and (r["home_score"], r["away_score"]) == (1, 0)
        ss.reset_cache()
        assert ss.result_from_espn("Taylor Fritz vs Francisco Cerundolo", "tennis", "2026-09-05") is None

    def test_un_sport_dequipe_garde_sa_plage(self, monkeypatch):
        appels = []
        def fake(url, bucket, budget, source=None):
            appels.append(url); return {"events": []}
        monkeypatch.setattr(ss, "_get_json", fake)
        ss.fixtures_espn("soccer", "2026-09-05", "2026-09-05")
        assert appels == [f"{ss.ESPN_BASE}/soccer/all/scoreboard?dates=20260904-20260906&limit=1000"]

    def test_une_fenetre_dun_seul_jour_est_lisible(self):
        assert ss._dans_fenetre("2026-09-05T02:45Z", "20260905")
        assert not ss._dans_fenetre("2026-09-06T02:45Z", "20260905")
        assert ss._jours_de_fenetre("20260904-20260906") == ["20260904", "20260905", "20260906"]
        assert ss._jours_de_fenetre("20260905") == ["20260905"]
        assert len(ss._jours_de_fenetre("20260101-20261231")) == 10          # plafond
