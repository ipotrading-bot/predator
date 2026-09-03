"""
tests/test_api_sports.py — core/api_sports.py (famille api-sports.io).

Contexte : après l'incident 10→20 août 2026 (dix jours sans signal, toutes
les sources filtrées par IP), cette famille devient le repli principal parce
qu'elle authentifie par CLÉ. Ce que ces tests verrouillent :

- le COÛT : 1 requête calendrier + ≤ MAX_ODDS_PAGES requêtes cotes PAR DATE
  (jamais une requête par match — c'est ce qui tuait le quota) ;
- le prix SHARP (Pinnacle/exchange) est extrait comme `odds_pinnacle` et
  n'entre pas dans le line shopping soft ;
- le nul n'existe que pour le foot (X=0 ailleurs, jamais une cote inventée) ;
- les formes de réponse divergentes (fixture.id vs game.id, timestamp vs
  date ISO) sont toutes acceptées ;
- un 200 porteur d'un objet `errors` (clé non abonnée à ce sport) est un
  ÉCHEC, pas un succès vide ;
- chaque échec est loggé — le silence est ce qui a caché la panne dix jours ;
- sans clé : aucun appel réseau.
"""
import logging
from datetime import datetime, timedelta, timezone

import pytest

import core.api_sports as aps
from core.math_engine import synthetic_dnb


class _Resp:
    def __init__(self, payload, status=200, remaining="50"):
        self._payload, self.status_code = payload, status
        self.headers = {"x-ratelimit-requests-remaining": remaining}

    def json(self):
        return self._payload


def _when(hours=5):
    return datetime.now(timezone.utc) + timedelta(hours=hours)


def _game(gid, home, away, *, style="fixture", hours=5):
    """style='fixture' (foot) ou 'game' (basket/baseball/hockey)."""
    dt = _when(hours)
    if style == "fixture":
        return {"fixture": {"id": gid, "timestamp": int(dt.timestamp())},
                "teams": {"home": {"name": home}, "away": {"name": away}},
                "league": {"name": "Ligue Test"}}
    return {"id": gid, "date": dt.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
            "teams": {"home": {"name": home}, "away": {"name": away}},
            "league": {"name": "Ligue Test"}}


def _bk(name, o1, o2, ox=None, bet="Match Winner"):
    values = [{"value": "Home", "odd": str(o1)}, {"value": "Away", "odd": str(o2)}]
    if ox is not None:
        values.insert(1, {"value": "Draw", "odd": str(ox)})
    return {"name": name, "bets": [{"name": bet, "values": values}]}


def _odds_row(gid, bookmakers, *, style="fixture"):
    key = "fixture" if style == "fixture" else "game"
    return {key: {"id": gid}, "bookmakers": bookmakers}


def _wire(monkeypatch, schedule, odds_pages, *, sched_status=200, odds_status=200,
          remaining="50", sched_errors=None):
    """Stub calqué sur la VRAIE API (vérifiée le 2026-08-20) :
    - calendrier : `date=` (un appel par date de la fenêtre) ;
    - cotes foot : `date=` + `page=` ;
    - cotes autres sports : `game=<id>`, un appel par match.
    `odds_pages` est indexé par (date, page) pour le foot, par ("game", id)
    pour les autres."""
    calls = {"schedule": [], "odds": []}

    def fake_get(url, headers=None, params=None, timeout=None):
        if url.endswith("/odds"):
            if "game" in params:
                key = ("game", int(params["game"]))
                calls["odds"].append(key)
                if odds_status != 200:
                    return _Resp({}, status=odds_status, remaining=remaining)
                return _Resp({"response": odds_pages.get(key, [])}, remaining=remaining)
            day, page = params["date"], params["page"]
            calls["odds"].append((day, page))
            if odds_status != 200:
                return _Resp({}, status=odds_status, remaining=remaining)
            items = odds_pages.get((day, page), [])
            total = max((p for (d, p) in odds_pages if d == day and isinstance(p, int)),
                        default=1)
            return _Resp({"response": items, "paging": {"current": page, "total": total}},
                         remaining=remaining)
        calls["schedule"].append(params.get("date"))
        return _Resp({"response": schedule, "errors": sched_errors or []},
                     status=sched_status, remaining=remaining)

    monkeypatch.setattr(aps.requests, "get", fake_get)
    return calls


@pytest.fixture(autouse=True)
def _no_secret_lookup(monkeypatch):
    """Aucun test ne doit toucher Supabase/l'env réels — ni pour les secrets,
    ni pour le compteur de budget journalier (qui écrit dans `meta`)."""
    monkeypatch.setattr(aps, "get_secret", lambda name, **kw: None)
    monkeypatch.setattr(aps, "_usage_get", lambda sport: 0)
    monkeypatch.setattr(aps, "_usage_add", lambda sport, n: None)
    # Cadence 10/min : réelle en production, nulle ici (sinon 6,5 s par appel).
    monkeypatch.setattr(aps, "REQUEST_SPACING_S", 0.0)
    monkeypatch.setattr(aps, "_suspended_this_process", False)


def test_no_key_means_no_network(monkeypatch):
    touched = []
    monkeypatch.setattr(aps.requests, "get", lambda *a, **k: touched.append(a))
    assert aps.fetch_sport("soccer") == []
    assert aps.fetch_all() == []
    assert touched == []


def test_unknown_sport_is_ignored():
    assert aps.fetch_sport("quidditch", api_key="k") == []


def test_cost_is_one_schedule_call_plus_paged_odds(monkeypatch):
    day = _when().strftime("%Y-%m-%d")
    sched = [_game(1, "PSG", "Lyon"), _game(2, "Nice", "Lens")]
    pages = {(day, 1): [_odds_row(1, [_bk("Bwin", 1.80, 4.40, 3.60)])],
             (day, 2): [_odds_row(2, [_bk("Bet365", 2.10, 3.50, 3.30)])]}
    calls = _wire(monkeypatch, sched, pages)
    out = aps.fetch_sport("soccer", api_key="k")
    assert 1 <= len(calls["schedule"]) <= 2      # aujourd'hui, + demain si la fenêtre déborde
    assert calls["odds"] == [(day, 1), (day, 2)]
    assert sorted(m["match"] for m in out) == ["Nice vs Lens", "PSG vs Lyon"]
    assert all(m["commence_time"].endswith("Z") for m in out)
    assert all(m["sport"] == "soccer" and m["sport_id"] == 1 for m in out)


def test_page_budget_is_capped(monkeypatch):
    day = _when().strftime("%Y-%m-%d")
    sched = [_game(i, f"H{i}", f"A{i}") for i in range(1, 40)]
    pages = {(day, p): [_odds_row(p, [_bk("Bwin", 2.0, 3.5)])] for p in range(1, 12)}
    calls = _wire(monkeypatch, sched, pages)
    aps.fetch_sport("soccer", api_key="k")
    assert len(calls["odds"]) == aps.MAX_ODDS_PAGES


def test_sharp_price_is_split_out_and_excluded_from_soft(monkeypatch):
    day = _when().strftime("%Y-%m-%d")
    sched = [_game(7, "Lille", "Reims")]
    pages = {(day, 1): [_odds_row(7, [
        _bk("Pinnacle", 2.50, 2.90, 3.20),     # sharp — ne doit pas gonfler le soft
        _bk("Bwin",     2.30, 2.80, 3.40),
        _bk("Unibet",   2.20, 3.00, 3.50),
    ])]}
    _wire(monkeypatch, sched, pages)
    (m,) = aps.fetch_sport("soccer", api_key="k")
    assert m["odds_pinnacle"] == {"1": 2.50, "X": 3.20, "2": 2.90}
    # Le bloc soft est celui d'UN SEUL book, jamais un assemblage.
    assert m["odds_1xbet"] in ({"1": 2.30, "X": 3.40, "2": 2.80},
                               {"1": 2.20, "X": 3.50, "2": 3.00})


def test_le_bloc_soft_retenu_est_celui_dun_seul_book(monkeypatch):
    """Le line shopping se fait sur le prix FINAL EXÉCUTABLE, pas issue par
    issue : un « 1 » de Bwin marié à un « X » d'Unibet donne un 1X2 que
    personne n'offre, donc un DNB synthétique qu'on ne peut pas placer."""
    day = _when().strftime("%Y-%m-%d")
    pages = {(day, 1): [_odds_row(7, [
        _bk("Bwin",   2.30, 2.80, 3.40),
        _bk("Unibet", 2.20, 3.00, 3.50),
    ])]}
    _wire(monkeypatch, [_game(7, "Lille", "Reims")], pages)
    (m,) = aps.fetch_sport("soccer", api_key="k")
    soft = m["odds_1xbet"]
    assert soft in ({"1": 2.30, "X": 3.40, "2": 2.80},
                    {"1": 2.20, "X": 3.50, "2": 3.00}), \
        "le bloc doit provenir d'un book réel, pas d'un max par issue"
    # L'ancien comportement fabriquait exactement ceci :
    assert soft != {"1": 2.30, "X": 3.50, "2": 3.00}


def test_le_book_retenu_est_celui_au_meilleur_prix_executable(monkeypatch):
    """Sur le côté qui sera joué (le favori), c'est le DNB synthétique final
    qui départage — pas la cote nue du favori."""
    day = _when().strftime("%Y-%m-%d")
    # Favori = domicile chez les deux books. B a un « 1 » PLUS FAIBLE mais un
    # nul si généreux que son DNB exécutable est meilleur : c'est lui qui doit
    # gagner. Un tri sur la cote nue du favori choisirait A.
    a = {"1": 1.80, "X": 3.20, "2": 4.50}
    b = {"1": 1.78, "X": 8.00, "2": 4.40}
    assert synthetic_dnb(b["1"], b["X"]) > synthetic_dnb(a["1"], a["X"])
    pages = {(day, 1): [_odds_row(7, [_bk("A", a["1"], a["2"], a["X"]),
                                      _bk("B", b["1"], b["2"], b["X"])])]}
    _wire(monkeypatch, [_game(7, "Lille", "Reims")], pages)
    (m,) = aps.fetch_sport("soccer", api_key="k")
    assert m["odds_1xbet"] == b


def test_hors_football_le_bloc_reste_celui_dun_seul_book(monkeypatch):
    """Le pari n'a qu'une jambe, mais rendre un bloc cohérent supprime toute
    recomposition accidentelle en aval."""
    pages = {("game", 11): [_odds_row(11, [_bk("A", 1.60, 2.40),
                                           _bk("B", 1.55, 2.60)],
                                      style="game")]}
    _wire(monkeypatch, [_game(11, "Home", "Away", style="game")], pages)
    (m,) = aps.fetch_sport("basketball", api_key="k")
    assert m["odds_1xbet"] == {"1": 1.60, "X": 0.0, "2": 2.40}


def test_sharp_only_match_is_kept_but_yields_no_edge(monkeypatch):
    """Sans book soft, le prix sharp sert aussi de soft : edge nul par
    construction (jamais de faux signal), mais le match reste prixé."""
    day = _when().strftime("%Y-%m-%d")
    _wire(monkeypatch, [_game(3, "A", "B")],
          {(day, 1): [_odds_row(3, [_bk("Pinnacle", 1.95, 1.95)])]})
    (m,) = aps.fetch_sport("soccer", api_key="k")
    assert m["odds_1xbet"] == m["odds_pinnacle"]


@pytest.mark.parametrize("sport,style,sport_id", [
    ("basketball", "game", 4), ("baseball", "game", 6), ("hockey", "game", 7),
])
def test_non_soccer_uses_game_shape_and_has_no_draw(monkeypatch, sport, style, sport_id):
    """Hors foot, `/odds` n'accepte que `game=<id>` : `date` n'existe pas sur
    ces hôtes et `league`+`season` est fermé au plan gratuit (vérifié en
    direct). L'ancien appel par date échouait sur les trois sports."""
    sched = [_game(11, "Home", "Away", style=style)]
    pages = {("game", 11): [_odds_row(11, [_bk("Bwin", 1.60, 2.40, 12.0)], style=style)]}
    calls = _wire(monkeypatch, sched, pages)
    (m,) = aps.fetch_sport(sport, api_key="k")
    assert calls["odds"] == [("game", 11)]
    assert m["sport"] == sport and m["sport_id"] == sport_id
    assert m["odds_1xbet"]["X"] == 0.0        # jamais de nul hors foot
    assert m["odds_1xbet"]["1"] == 1.60 and m["odds_1xbet"]["2"] == 2.40


def test_matches_outside_the_window_are_dropped(monkeypatch):
    sched = [_game(1, "Passé", "Hier", hours=-3), _game(2, "Trop", "Loin", hours=100),
             _game(3, "Bon", "Match", hours=5)]
    day = _when().strftime("%Y-%m-%d")
    _wire(monkeypatch, sched, {(day, 1): [_odds_row(3, [_bk("Bwin", 2.0, 2.0)])]})
    out = aps.fetch_sport("soccer", api_key="k", hours_ahead=24)
    assert [m["match"] for m in out] == ["Bon vs Match"]


def test_applicative_error_on_http_200_is_a_failure(monkeypatch, caplog):
    """api-sports répond 200 + {"errors": …} quand la clé n'est pas abonnée
    à CE sport — le prendre pour un succès vide masquerait la panne."""
    calls = _wire(monkeypatch, [], {}, sched_errors={"token": "not subscribed"})
    with caplog.at_level(logging.WARNING, logger="PREDATOR.api_sports"):
        assert aps.fetch_sport("baseball", api_key="k") == []
    assert calls["odds"] == []
    assert any("refus applicatif" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("status,needle", [(401, "auth"), (429, "429"), (500, "HTTP 500")])
def test_schedule_http_errors_are_logged(monkeypatch, caplog, status, needle):
    _wire(monkeypatch, [], {}, sched_status=status)
    with caplog.at_level(logging.WARNING, logger="PREDATOR.api_sports"):
        assert aps.fetch_sport("soccer", api_key="k") == []
    assert any(needle.lower() in r.getMessage().lower() for r in caplog.records)


def test_odds_http_error_stops_cleanly_and_is_logged(monkeypatch, caplog):
    calls = _wire(monkeypatch, [_game(1, "A", "B")], {}, odds_status=429)
    with caplog.at_level(logging.WARNING, logger="PREDATOR.api_sports"):
        assert aps.fetch_sport("soccer", api_key="k") == []
    assert len(calls["odds"]) == 1            # une erreur = arrêt, pas de boucle
    assert any("HTTP 429" in r.getMessage() for r in caplog.records)


def test_quota_guard_skips_paid_calls(monkeypatch, caplog):
    calls = _wire(monkeypatch, [_game(1, "A", "B")], {}, remaining="3")
    with caplog.at_level(logging.INFO, logger="PREDATOR.api_sports"):
        assert aps.fetch_sport("soccer", api_key="k") == []
    assert calls["odds"] == []
    assert any("garde quota" in r.getMessage() for r in caplog.records)


def test_fetch_all_isolates_a_failing_sport(monkeypatch):
    """Un sport qui explose ne doit pas emporter les autres."""
    def flaky(sport, hours_ahead=24):
        if sport == "basketball":
            raise RuntimeError("boom")
        return [{"match": f"{sport} match"}]

    monkeypatch.setattr(aps, "fetch_sport", flaky)
    out = aps.fetch_all()
    assert {m["match"] for m in out} == {"soccer match", "baseball match", "hockey match"}


def test_available_sports_reports_configured_keys(monkeypatch):
    monkeypatch.setattr(aps, "get_secret",
                        lambda name, **kw: "k" if name in ("API_FOOTBALL_KEY", "API_SPORTS_KEY") else None)
    # API_SPORTS_KEY est la clé commune : elle débloque les quatre sports.
    assert aps.available_sports() == ["soccer", "basketball", "baseball", "hockey"]

    monkeypatch.setattr(aps, "get_secret",
                        lambda name, **kw: "k" if name == "API_FOOTBALL_KEY" else None)
    assert aps.available_sports() == ["soccer"]


# ── Budget journalier ─────────────────────────────────────────────────

def test_daily_budget_stops_the_cycle_before_any_call(monkeypatch, caplog):
    """100 req/jour au plan gratuit ; ~40 scans quotidiens y passeraient
    trois fois si rien ne comptait. Le compte du projet a été trouvé
    SUSPENDU le 2026-08-20 — ce garde-fou existe pour ne pas y revenir."""
    touched = []
    monkeypatch.setattr(aps.requests, "get", lambda *a, **k: touched.append(a))
    # Depuis le 2026-08-26 les SCANS s'arrêtent à SCAN_BUDGET, pas au plafond :
    # les dernières requêtes sont la réserve du settlement, qui lit les scores
    # finaux (core/api_sports.fetch_results). Les scans sont amputés, jamais la
    # réserve — même remède que le cloisonnement Groq du 2026-08-02.
    monkeypatch.setattr(aps, "_usage_get", lambda sport: aps.SCAN_BUDGET)
    with caplog.at_level(logging.WARNING, logger="PREDATOR.api_sports"):
        assert aps.fetch_sport("soccer", api_key="k") == []
    assert touched == []
    assert any("budget de SCAN" in r.getMessage() for r in caplog.records)


class TestRythmeDeDepense:
    """Le budget de scan partait PREMIER ARRIVÉ, PREMIER SERVI.

    Mesuré le 2026-08-27 : api-sports s'éteignait à 19:20 (64/64) et le slate
    sharp de Tier 2 tombait de 42 matchs à 28 puis 25, précisément quand le
    slate européen entre dans la zone jouable. Le budget n'est pas augmenté
    (compte SUSPENDU pour dépassement le 2026-08-20) — il est étalé.
    """

    @staticmethod
    def _at(h, m=0):
        return datetime(2026, 8, 27, h, m, tzinfo=timezone.utc)

    def test_l_ouverture_croit_avec_la_journee(self):
        matin = aps.scan_allowance(self._at(6))
        soir  = aps.scan_allowance(self._at(20))
        assert matin < soir, "le matin ne doit pas pouvoir dépenser autant que le soir"

    def test_plancher_un_cycle_le_premier_scan_du_jour_part_toujours(self):
        assert aps.scan_allowance(self._at(0, 3)) >= aps.CYCLE_COST

    def test_l_ouverture_ne_depasse_jamais_le_budget_de_scan(self):
        assert aps.scan_allowance(self._at(23, 59)) <= aps.SCAN_BUDGET

    def test_le_matin_ne_peut_pas_bruler_le_budget_du_soir(self, monkeypatch, caplog):
        """La régression exacte du 2026-08-27 : huit cycles matinaux
        épuisaient la seule source qui porte un prix sharp sur 100 % de ses
        matchs, et les scans du soir repartaient sans elle."""
        touched = []
        monkeypatch.setattr(aps.requests, "get", lambda *a, **k: touched.append(a))
        monkeypatch.setattr(aps, "scan_allowance", lambda now=None: aps.CYCLE_COST)
        # Déjà dépensé plus que l'ouverture de cette heure, mais TRÈS loin du
        # budget total : l'ancienne garde laissait passer.
        spent = aps.CYCLE_COST + 1
        assert spent < aps.SCAN_BUDGET
        monkeypatch.setattr(aps, "_usage_get", lambda sport: spent)
        with caplog.at_level(logging.INFO, logger="PREDATOR.api_sports"):
            assert aps.fetch_sport("soccer", api_key="k") == []
        assert touched == [], "une requête est partie malgré le rythme de dépense"
        assert any("rythme de dépense" in r.getMessage() for r in caplog.records)

    def test_sous_l_ouverture_le_cycle_part(self, monkeypatch):
        """Le rythme freine, il ne ferme pas : sous l'ouverture, on scanne."""
        day = _when().strftime("%Y-%m-%d")
        _wire(monkeypatch, [_game(1, "PSG", "Lyon")],
              {(day, 1): [_odds_row(1, [_bk("Bwin", 1.80, 4.40, 3.60)])]})
        monkeypatch.setattr(aps, "scan_allowance", lambda now=None: aps.SCAN_BUDGET)
        monkeypatch.setattr(aps, "_usage_get", lambda sport: 0)
        monkeypatch.setattr(aps, "_usage_add", lambda sport, n: None)
        assert aps.fetch_sport("soccer", api_key="k"), "le rythme a fermé un cycle qu'il devait laisser passer"

    def test_la_reserve_du_settlement_ignore_le_rythme(self, monkeypatch):
        """fetch_results n'est pas un scan : un résultat manquant sort un
        signal en `expired` et n'apprend rien à personne."""
        import inspect
        src = inspect.getsource(aps.fetch_results)
        assert "scan_allowance" not in src


def test_requests_actually_spent_are_counted(monkeypatch):
    day = _when().strftime("%Y-%m-%d")
    sched = [_game(1, "PSG", "Lyon")]
    pages = {(day, 1): [_odds_row(1, [_bk("Bwin", 1.80, 4.40, 3.60)])]}
    _wire(monkeypatch, sched, pages)
    spent = []
    monkeypatch.setattr(aps, "_usage_add", lambda sport, n: spent.append((sport, n)))
    aps.fetch_sport("soccer", api_key="k")
    (sport, n), = spent
    assert sport == "soccer" and 2 <= n <= 3   # calendrier(s) + 1 page de cotes


def test_a_429_burns_the_local_budget_for_the_day(monkeypatch):
    """Sans cela, les 39 scans suivants relanceraient un 429 chacun — le
    martèlement qui fait suspendre un compte."""
    _wire(monkeypatch, [], {}, sched_status=429)
    spent = []
    monkeypatch.setattr(aps, "_usage_add", lambda sport, n: spent.append(n))
    assert aps.fetch_sport("soccer", api_key="k") == []
    # Cale au budget de SCAN et non au plafond total : un 429 pendant un scan
    # ne doit pas emporter la réserve du settlement avec lui.
    assert spent and spent[0] >= aps.SCAN_BUDGET
    assert spent[0] < aps.DAILY_BUDGET, "le 429 d'un scan a condamné la réserve du settlement"


def test_suspended_account_is_counted_and_reported(monkeypatch, caplog):
    """Cas réel du 2026-08-20 : compte suspendu → HTTP 200 + objet errors.
    L'ancienne implémentation le lisait comme un succès vide, sans un log."""
    _wire(monkeypatch, [], {}, sched_errors={"access": "Your account is suspended"})
    spent = []
    monkeypatch.setattr(aps, "_usage_add", lambda sport, n: spent.append(n))
    with caplog.at_level(logging.WARNING, logger="PREDATOR.api_sports"):
        assert aps.fetch_sport("soccer", api_key="k") == []
    assert spent == [1]
    assert any("suspended" in r.getMessage() for r in caplog.records)


def test_suspended_account_stops_every_call_for_the_day(monkeypatch, caplog):
    """2026-09-03 : le compte du 2026-09-02 suspendu en < 24 h, et chaque run
    (scan ET audit) le réinterrogeait quatre fois. Première réponse
    « suspended » → compartiment partagé posé, plus aucun appel du jour,
    tous sports et fetch_results compris."""
    calls = _wire(monkeypatch, [], {}, sched_errors={"access": "Your account is suspended"})
    posted = []
    monkeypatch.setattr(aps.daily_quota, "add", lambda bucket, n: posted.append((bucket, n)))
    assert aps.fetch_sport("soccer", api_key="k") == []
    assert (aps._SUSPENDED_BUCKET, 1) in posted
    assert aps.is_suspended()
    # Plus rien ne part : ni un autre sport, ni le settlement.
    n = len(calls["schedule"])
    assert aps.fetch_sport("basketball", api_key="k") == []
    assert aps.fetch_results("2026-09-03", "soccer", api_key="k") == []
    assert len(calls["schedule"]) == n


def test_suspension_persistee_entre_runs_via_daily_quota(monkeypatch):
    monkeypatch.setattr(aps.daily_quota, "spent",
                        lambda bucket: 1 if bucket == aps._SUSPENDED_BUCKET else 0)
    assert aps.is_suspended()
    calls = _wire(monkeypatch, [], {})
    assert aps.fetch_sport("soccer", api_key="k") == []
    assert calls["schedule"] == []


def test_la_cadence_espace_les_requetes(monkeypatch):
    """Plan gratuit : 10 requêtes/minute. Deux appels consécutifs sont
    espacés de REQUEST_SPACING_S, quel que soit l'appelant."""
    slept = []
    monkeypatch.setattr(aps, "REQUEST_SPACING_S", 6.5)
    monkeypatch.setattr(aps.time, "sleep", lambda s: slept.append(s))
    clock = iter([100.0, 100.0, 101.0, 101.0])
    monkeypatch.setattr(aps.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(aps, "_last_request_at", 0.0)
    aps._throttle()
    aps._throttle()
    assert slept and abs(slept[-1] - 5.5) < 1e-6


def test_game_odds_are_capped(monkeypatch):
    """Une requête par match : sans plafond, un slate NBA chargé viderait
    les 100 requêtes/jour en un seul cycle."""
    sched = [_game(i, f"H{i}", f"A{i}", style="game") for i in range(30)]
    pages = {("game", i): [_odds_row(i, [_bk("Bwin", 2.0, 2.0)], style="game")]
             for i in range(30)}
    calls = _wire(monkeypatch, sched, pages)
    monkeypatch.setattr(aps, "MAX_GAME_ODDS", 4)
    aps.fetch_sport("basketball", api_key="k")
    assert len(calls["odds"]) == 4


def test_next_day_is_also_fetched_when_the_window_spills_over(monkeypatch):
    """`date=` ne rend qu'une journée ; une fenêtre de 24h en chevauche deux,
    et sans le second appel la moitié tardive du slate manque."""
    calls = _wire(monkeypatch, [], {})
    aps.fetch_sport("soccer", api_key="k", hours_ahead=24)
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    expected = {now.strftime("%Y-%m-%d"), (now + timedelta(hours=24)).strftime("%Y-%m-%d")}
    assert set(calls["schedule"]) == expected
