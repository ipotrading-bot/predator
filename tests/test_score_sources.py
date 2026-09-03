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
        monkeypatch.setattr(ss, "result_from_thesportsdb",
                            lambda *a: vus.append("tsdb") or None)
        ss.fetch_score("A FC vs B FC", "soccer", "2026-09-01")
        assert vus == ["espn", "tsdb"]
        vus.clear()
        ss.fetch_score("A vs B", "baseball", "2026-09-01")
        assert vus == ["mlb", "espn", "tsdb"]

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
        assert ss.result_from_espn("Alcaraz vs Sinner", "tennis", "2026-08-30") is None
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
