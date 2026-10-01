"""
tests/test_temps_reglementaire.py — un 1X2 de temps réglementaire n'est pas
une moneyline (2026-10-01).

MESURÉ en base (`meta.cache_soft_slate`, scan de 11:11) : 1xbet servi par
OddsAPI sur la NHL = {"1": 2.51, "X": 4.26, "2": 2.52}, face à un Pinnacle à
deux issues (1.93 / 1.95). Le moteur lisait « 1 » et « 2 » sans voir le nul :
EV +23 à +28 % sur CHAQUE match, écartée par le seul plafond SUSPECT. Les
totaux du même book (réglés eux aussi sur 60 minutes) passaient, eux : 4
signaux NHL sur 4 étaient des Over 1xbet à +7,6..+12 %.

Même piège côté SHARP : Smarkets `WINNER_3_WAY` sur le hockey européen,
dévigué sur deux issues de trois — 1,70 / 4,80 donnait 73,9 % au favori
(signal 10604, Slavia Prague, edge affiché +4,87 %).

Et le filtre « réglable » admettait ce match de 2e division tchèque parce
qu'« Havirov Panthers » ressemble aux Florida Panthers qu'ESPN liste.

Contrat gardé ici :
  1. hors football, un bloc qui cote le nul ne donne AUCUN prix exécutable ;
  2. un prix sharp à trois issues n'entre ni en bouche-trou, ni en
     contre-expertise, ni au consensus d'une moneyline ;
  3. les marchés à ligne d'un book qui règle le sport sur 60 minutes sont
     retirés de la comparaison, sauf les handicaps à |ligne| ≥ 1,5 ;
  4. le book retiré laisse la place au suivant (Bet365 continue de servir) ;
  5. hors football, « réglable » exige les DEUX camps chez ESPN ;
  6. chaque refus est loggé ; le football ne change pas d'un iota.
"""
import logging
from datetime import datetime, timedelta, timezone

import pytest

import run_engine as eng
from core import odds_api
from core.constants import EXECUTION_BOOKS
from core.execution_books import (HANDICAP_EQUIVALENT_MIN, TEMPS_REGLEMENTAIRE,
                                  books_temps_reglementaire, choisir_bloc_h2h,
                                  fusionner_lignes, retirer_temps_reglementaire)
from core.math_engine import executable_price, nul_cote, to_binary

log = logging.getLogger("test")
REF, SECOND = EXECUTION_BOOKS

# Le bloc RÉEL du 2026-10-01 (Columbus–Buffalo, puis New Jersey–Philadelphie).
NHL_1X2 = {"1": 2.07, "X": 4.31, "2": 3.17}
NHL_ML = {"1": 1.58, "X": 0.0, "2": 2.45}
PIN_ML = {"1": 1.62, "X": 0.0, "2": 2.43}


def _now():
    return datetime.now(timezone.utc)


def _dans(heures=6):
    return (_now() + timedelta(hours=heures)).isoformat()


def _messages(caplog):
    return [r.getMessage() for r in caplog.records]


# ── 1. Le prix exécutable ────────────────────────────────────────────────

class TestUnNulCoteHorsFootballNestPasUneMoneyline:
    def test_nul_cote_lit_le_bloc(self):
        assert nul_cote(NHL_1X2) is True
        assert nul_cote(NHL_ML) is False
        assert nul_cote({"1": 1.5, "2": 2.5}) is False
        assert nul_cote(None) is False and nul_cote({}) is False
        assert nul_cote({"X": "?"}) is False               # illisible ≠ coté

    @pytest.mark.parametrize("sport", ["hockey", "basketball", "tennis", "mma",
                                       "americanfootball", "rugbyleague"])
    def test_aucun_prix_executable(self, sport):
        assert executable_price(NHL_1X2, sport, "1") == 0.0
        assert executable_price(NHL_1X2, sport, "2") == 0.0
        assert to_binary(NHL_1X2, sport, "A", "B") == (0.0, None, "")

    def test_la_moneyline_a_deux_issues_reste_jouable(self):
        assert executable_price(NHL_ML, "hockey", "1") == 1.58
        prix, libelle, favori = to_binary(NHL_ML, "hockey", "A", "B")
        assert (prix, libelle, favori) == (1.58, "Moneyline", "A")

    def test_le_football_garde_son_dnb_synthetique(self):
        """Le nul EST le marché du football : rien ne change."""
        prix, libelle, favori = to_binary(NHL_1X2, "soccer", "A", "B")
        assert prix > 1.01 and libelle == "AH 0.0 (2 jambes)" and favori == "A"

    def test_le_book_a_trois_issues_laisse_la_place_au_suivant(self):
        book, bloc, prix, favori = choisir_bloc_h2h(
            {REF: NHL_1X2, SECOND: NHL_ML}, "hockey", "A", "B")
        assert (book, prix, favori) == (SECOND, 1.58, "A") and bloc is NHL_ML

    def test_seul_book_a_trois_issues_rien(self):
        assert choisir_bloc_h2h({REF: NHL_1X2}, "hockey", "A", "B") == (None, {}, 0.0, "")


# ── 2. Le moteur, côté h2h ───────────────────────────────────────────────

def _h2h(par_book, pin=PIN_ML, **extra):
    m = {"id": "h1", "commence_time": _dans(), "h2h_par_book": par_book,
         "odds_1xbet": next(iter(par_book.values())), "odds_pinnacle": pin}
    m.update(extra)
    return m


def _run_h2h(m, caplog, sport="hockey"):
    out: list = []
    with caplog.at_level(logging.INFO):
        eng._process_h2h(m, "New Jersey Devils vs Philadelphia Flyers", sport, "NHL",
                         "New Jersey Devils", "Philadelphia Flyers", "🏒",
                         out, None, _now(), log, min_edge=2.0)
    return out


class TestLeH2hRefuseLesTroisIssues:
    def test_le_cas_reel_du_1er_octobre_ne_sort_plus(self, caplog):
        """1xbet 2,07 / 4,31 / 3,17 contre Pinnacle 1,62 / 2,43 : l'« EV » de
        +25 % n'existait que par la confusion des deux marchés."""
        out = _run_h2h(_h2h({REF: NHL_1X2}), caplog)
        assert out == []
        msgs = _messages(caplog)
        assert any("1X2 RÉGL." in m and REF in m for m in msgs), msgs
        # Plus de mesure « OUTSIDER » fantôme à +25 % : aucun prix exécutable.
        assert not any("OUTSIDER" in m for m in msgs)

    def test_le_second_book_a_deux_issues_continue_de_servir(self, caplog):
        soft = {"1": 1.75, "X": 0.0, "2": 2.20}            # EV ≈ +5 % sur ~60 %
        out = _run_h2h(_h2h({REF: NHL_1X2, SECOND: soft}), caplog)
        assert len(out) == 1 and out[0]["soft_book"] == SECOND
        assert out[0]["executable_odd"] == 1.75

    def test_un_sharp_a_trois_issues_ne_fait_pas_de_moneyline(self, caplog):
        """Signal 10604 : Smarkets 1,70 / nul / 4,80 posé en « Pinnacle »,
        Bet365 à 1,42. Dévigué sur deux issues : 73,9 %, edge +4,87 %."""
        sharp_3 = {"1": 1.70, "X": 4.50, "2": 4.80}
        out = _run_h2h(_h2h({SECOND: {"1": 1.42, "X": 0.0, "2": 2.70}}, pin=sharp_3), caplog)
        assert out == []
        assert any("1X2 RÉGL." in m and "sharp" in m for m in _messages(caplog))

    def test_un_exchange_a_trois_issues_reste_hors_du_consensus(self, caplog):
        """`odds_exchange` venu d'un slate d'avant le correctif : il ne pèse
        plus sur la probabilité d'une moneyline."""
        soft = {"1": 1.75, "X": 0.0, "2": 2.20}
        sans = _run_h2h(_h2h({SECOND: soft}), caplog)
        avec = _run_h2h(_h2h({SECOND: soft},
                             odds_exchange={"1": 1.95, "X": 4.2, "2": 3.6}), caplog)
        assert len(sans) == len(avec) == 1
        assert avec[0]["sharp_prob"] == sans[0]["sharp_prob"]


# ── 3. L'exchange à trois issues ─────────────────────────────────────────

def _pool(o1, ox, o2, source="smarkets"):
    row = {"home": "HC Slavia Prague", "away": "HC Havirov Panthers",
           "1": o1, "X": ox, "2": o2, "_source": source,
           "totals": {"point": 5.5, "over": 1.9, "under": 1.9}}
    return {"hc slavia prague_hc havirov panthers": row}


def _match_exchange(sport, pin=None):
    m = {"match": "HC Slavia Prague vs HC Havirov Panthers", "sport": sport,
         "home": "HC Slavia Prague", "away": "HC Havirov Panthers"}
    if pin:
        m["odds_pinnacle"] = pin
    return m


class TestLExchangeATroisIssuesHorsFootball:
    def test_il_ne_bouche_aucun_trou(self, caplog):
        m = _match_exchange("hockey")
        with caplog.at_level(logging.INFO):
            n = eng._enrich_from_exchange([m], _pool(1.70, 4.50, 4.80), log)
        assert n == 0 and "odds_pinnacle" not in m and "_exchange" not in m
        # Ses totaux sont de la même famille de marchés : pas posés non plus.
        assert "totals_pinnacle" not in m
        assert any("1X2 RÉGL." in x and "smarkets" in x for x in _messages(caplog))

    def test_il_ne_contre_expertise_pas_une_moneyline(self):
        """Comparé à un Pinnacle à deux issues, il divergerait de plusieurs
        points et ferait refuser le match ENTIER à tort."""
        m = _match_exchange("hockey", pin={"1": 1.45, "X": 0.0, "2": 2.85})
        eng._enrich_from_exchange([m], _pool(1.70, 4.50, 4.80), log)
        assert "_sharp_conflict" not in m and "odds_exchange" not in m

    def test_l_exchange_a_deux_issues_reste_un_bouche_trou(self):
        m = _match_exchange("hockey")
        assert eng._enrich_from_exchange([m], _pool(1.45, 0.0, 2.85, "matchbook"), log) == 1
        assert m["odds_pinnacle"] == {"1": 1.45, "X": 0.0, "2": 2.85}

    def test_le_football_garde_son_1x2(self):
        m = _match_exchange("soccer")
        assert eng._enrich_from_exchange([m], _pool(1.70, 4.50, 4.80), log) == 1
        assert m["odds_pinnacle"]["X"] == 4.50


# ── 4. Les marchés à ligne d'un book réglé sur 60 minutes ─────────────────

def _fusion(par_book, marche="totals"):
    return fusionner_lignes(par_book, marche)


class TestLaTableDuTempsReglementaire:
    def test_la_table_ne_nomme_que_des_books_d_execution(self):
        assert set(TEMPS_REGLEMENTAIRE) <= set(EXECUTION_BOOKS)
        assert books_temps_reglementaire("hockey") == frozenset({"1xbet"})
        assert books_temps_reglementaire("soccer") == frozenset()
        assert books_temps_reglementaire("") == frozenset()

    def test_le_moteur_ne_nomme_aucun_book(self):
        """La règle vit dans execution_books : run_engine l'applique sans
        connaître ni le book ni le sport concernés."""
        corps = open(eng.__file__, encoding="utf-8").read()
        debut = corps.index("def _sans_temps_reglementaire(")
        fonction = corps[debut: corps.index("\ndef ", debut + 10)]
        assert "1xbet" not in fonction and '"hockey"' not in fonction

    def test_total_le_book_regle_sur_60_minutes_est_retire(self):
        f = _fusion({REF: {"point": 6.5, "over": 2.04, "under": 1.78},
                     SECOND: {"point": 6.5, "over": 1.95, "under": 1.87}})
        assert f["over"] == 2.04 and f["books"]["over"] == REF      # avant
        g, retires = retirer_temps_reglementaire(f, "totals", "hockey")
        assert retires == [REF]
        assert g["over"] == 1.95 and g["under"] == 1.87
        assert g["books"] == {"over": SECOND, "under": SECOND}
        assert REF not in g["prix_books"]["over"] and REF not in g["prix_books"]["under"]
        assert g["ladder"][0]["over"] == 1.95
        assert f["over"] == 2.04, "le bloc d'origine ne doit pas être modifié"

    def test_total_sans_autre_book_il_ne_reste_rien(self):
        f = _fusion({REF: {"point": 6.5, "over": 2.04, "under": 1.78}})
        assert retirer_temps_reglementaire(f, "totals", "hockey") == (None, [REF])

    def test_un_barreau_que_seul_ce_book_cote_disparait(self):
        f = _fusion({REF: {"point": 5.5, "over": 1.75, "under": 2.18,
                           "ladder": [{"point": 5.5, "over": 1.75, "under": 2.18},
                                      {"point": 6.5, "over": 2.04, "under": 1.78}]},
                     SECOND: {"point": 6.5, "over": 1.95, "under": 1.87}})
        g, _ = retirer_temps_reglementaire(f, "totals", "hockey")
        assert [r["point"] for r in g["ladder"]] == [6.5] and g["point"] == 6.5

    @pytest.mark.parametrize("point", [1.5, -1.5, 2.5, -2.0])
    def test_handicap_a_partir_de_un_et_demi_le_pari_est_le_meme(self, point):
        f = _fusion({REF: {"point": point, "home": 1.90, "away": 1.94}}, "spreads")
        g, retires = retirer_temps_reglementaire(f, "spreads", "hockey")
        assert g is f and retires == []
        assert abs(point) >= HANDICAP_EQUIVALENT_MIN

    @pytest.mark.parametrize("point", [0.0, 0.5, -0.5, 1.0, -1.0])
    def test_handicap_sous_un_et_demi_le_nul_reglementaire_change_le_pari(self, point):
        f = _fusion({REF: {"point": point, "home": 1.94, "away": 1.94}}, "spreads")
        assert retirer_temps_reglementaire(f, "spreads", "hockey") == (None, [REF])

    @pytest.mark.parametrize("sport", ["soccer", "basketball", "tennis", "americanfootball"])
    def test_les_autres_sports_ne_sont_pas_touches(self, sport):
        f = _fusion({REF: {"point": 2.5, "over": 2.04, "under": 1.78}})
        g, retires = retirer_temps_reglementaire(f, "totals", sport)
        assert g is f and retires == []

    def test_un_bloc_sans_attribution_reste(self):
        """Repli sharp, slate d'avant le 2026-09-08 : on ne retire pas ce
        qu'on ne sait pas attribuer."""
        brut = {"point": 6.5, "over": 2.04, "under": 1.78}
        g, retires = retirer_temps_reglementaire(brut, "totals", "hockey")
        assert g is brut and retires == []
        assert retirer_temps_reglementaire(None, "totals", "hockey") == (None, [])


def _totals(par_book):
    return {"id": "t1", "commence_time": _dans(),
            "totals_1xbet": _fusion(par_book),
            "totals_pinnacle": {"point": 6.5, "over": 1.81, "under": 2.07}}


def _run_totals(m, caplog, sport="hockey"):
    out: list = []
    with caplog.at_level(logging.INFO):
        eng._process_totals(m, "Vancouver Canucks vs Edmonton Oilers", sport, "NHL", "🏒",
                            out, None, _now(), log, min_edge=2.0)
    return out


class TestLeMoteurNeCompareQueLeMemePari:
    def test_le_signal_10632_ne_sort_plus(self, caplog):
        """Vancouver–Edmonton Over 6.5 : 1xbet 2,04 contre Pinnacle 1,81,
        « +9,08 % ». Un 3-3 à la 60e est Under chez 1xbet et Over chez
        Pinnacle : ce n'était pas le même pari."""
        out = _run_totals(_totals({REF: {"point": 6.5, "over": 2.04, "under": 1.78}}), caplog)
        assert out == []
        assert any("TEMPS RÉGL." in m and REF in m and "totals" in m
                   for m in _messages(caplog)), _messages(caplog)

    def test_sans_la_regle_ce_meme_bloc_sortait(self, caplog, monkeypatch):
        """Contre-épreuve : le test ci-dessus ne passe pas par accident."""
        monkeypatch.setattr(eng, "retirer_temps_reglementaire", lambda b, m, s: (b, []))
        out = _run_totals(_totals({REF: {"point": 6.5, "over": 2.04, "under": 1.78}}), caplog)
        assert len(out) == 1 and out[0]["selection_name"] == "Over 6.5"
        assert out[0]["soft_book"] == REF

    def test_le_second_book_sert_la_meme_ligne_a_son_prix(self, caplog):
        out = _run_totals(_totals({REF: {"point": 6.5, "over": 2.04, "under": 1.78},
                                   SECOND: {"point": 6.5, "over": 1.95, "under": 1.87}}), caplog)
        assert len(out) == 1
        assert out[0]["soft_book"] == SECOND and out[0]["executable_odd"] == 1.95

    def test_le_football_emet_comme_avant(self, caplog):
        m = {"id": "s1", "commence_time": _dans(),
             "totals_1xbet": _fusion({REF: {"point": 2.5, "over": 2.04, "under": 1.78}}),
             "totals_pinnacle": {"point": 2.5, "over": 1.81, "under": 2.07}}
        out: list = []
        with caplog.at_level(logging.INFO):
            eng._process_totals(m, "A vs B", "soccer", "L1", "⚽", out, None, _now(), log,
                                min_edge=1.0)
        assert not any("TEMPS RÉGL." in x for x in _messages(caplog))
        assert all(s["soft_book"] == REF for s in out)

    def test_le_handicap_nul_du_book_est_retire_le_un_et_demi_reste(self, caplog):
        base = {"id": "p1", "commence_time": _dans()}
        out: list = []
        with caplog.at_level(logging.INFO):
            eng._process_spreads(
                {**base, "spreads_1xbet": _fusion({REF: {"point": 0.0, "home": 1.94, "away": 1.94}},
                                                  "spreads"),
                 "spreads_pinnacle": {"point": 0.0, "home": 1.80, "away": 2.05}},
                "A vs B", "hockey", "NHL", "A", "B", "🏒", out, None, _now(), log, min_edge=2.0)
        assert out == [] and any("TEMPS RÉGL." in x for x in _messages(caplog))
        caplog.clear()
        with caplog.at_level(logging.INFO):
            eng._process_spreads(
                {**base, "spreads_1xbet": _fusion({REF: {"point": 1.5, "home": 1.50, "away": 2.60}},
                                                  "spreads"),
                 "spreads_pinnacle": {"point": 1.5, "home": 1.48, "away": 2.75}},
                "A vs B", "hockey", "NHL", "A", "B", "🏒", out, None, _now(), log, min_edge=2.0)
        assert not any("TEMPS RÉGL." in x for x in _messages(caplog))


# ── 5. La source : un 1X2 hors football n'est pas un book d'exécution ─────

def _book(cle, h2h, totals=None):
    marches = [{"key": "h2h", "outcomes": [{"name": n, "price": p} for n, p in h2h.items()]}]
    if totals:
        marches.append({"key": "totals", "outcomes": [
            {"name": "Over", "price": totals[1], "point": totals[0]},
            {"name": "Under", "price": totals[2], "point": totals[0]}]})
    return {"key": cle, "markets": marches}


def _brut(h2h_1xbet, sport_title="NHL"):
    return {"id": "e1", "home_team": "Columbus Blue Jackets", "away_team": "Buffalo Sabres",
            "sport_title": sport_title, "commence_time": "2026-10-01T23:10:00Z",
            "bookmakers": [
                _book(odds_api.ODDS_API_BOOK_KEYS[REF], h2h_1xbet, (5.5, 1.75, 2.18)),
                _book(odds_api.PINNACLE_KEY,
                      {"Columbus Blue Jackets": 1.93, "Buffalo Sabres": 1.95}, (6.0, 1.9, 1.95))]}


class TestOddsApiNeCompteplusLe1X2CommeExecution:
    TROIS = {"Columbus Blue Jackets": 2.51, "Draw": 4.26, "Buffalo Sabres": 2.52}
    DEUX = {"Columbus Blue Jackets": 1.86, "Buffalo Sabres": 1.95}

    def test_hockey_bloc_a_trois_issues_match_non_exploitable(self):
        """`noter_execution` voit alors 0 match exploitable : la ligue passe
        en achat DIFFÉRÉ au lieu d'être payée pour rien à chaque créneau."""
        assert odds_api._parse_event(_brut(self.TROIS), "hockey") is None

    def test_son_prix_sharp_est_garde_pour_les_matchs_d_odds_api_io(self):
        row = odds_api.sharp_seul(_brut(self.TROIS), "hockey")
        assert row and (row["1"], row["X"], row["2"]) == (1.93, 0.0, 1.95)
        assert row["totals"]["point"] == 6.0

    def test_hockey_bloc_a_deux_issues_exploitable(self):
        ev = odds_api._parse_event(_brut(self.DEUX), "hockey")
        assert ev and ev["h2h_par_book"] == {REF: {"1": 1.86, "X": 0.0, "2": 1.95}}

    def test_football_le_nul_est_attendu(self):
        brut = _brut(self.TROIS, "MLS")
        brut["bookmakers"][1] = _book(odds_api.PINNACLE_KEY, {
            "Columbus Blue Jackets": 2.45, "Draw": 3.6, "Buffalo Sabres": 2.9})
        ev = odds_api._parse_event(brut, "soccer")
        assert ev and ev["h2h_par_book"][REF]["X"] == 4.26


# ── 6. Réglable : les deux camps hors football ───────────────────────────

def _ev(home, away):
    return {"competitions": [{"competitors": [
        {"homeAway": "home", "team": {"displayName": home}},
        {"homeAway": "away", "team": {"displayName": away}}],
        "status": {"type": {"state": "pre", "completed": False}}}]}


def _m(match, sport):
    home, away = match.split(" vs ")
    return {"match": match, "home": home, "away": away, "sport": sport, "league": "L"}


class TestReglableExigeLesDeuxCampsHorsFootball:
    NHL = {"hockey": [_ev("San Jose Sharks", "Florida Panthers"),
                      _ev("Columbus Blue Jackets", "Buffalo Sabres")]}

    def test_un_homonyme_ne_rend_pas_un_match_reglable(self):
        """2e division tchèque admise le 30/09 grâce aux Florida Panthers."""
        assert eng._reglable(_m("HC Slavia Prague vs HC Havirov Panthers", "hockey"),
                             self.NHL) is False

    def test_le_vrai_match_passe(self):
        assert eng._reglable(_m("Columbus Blue Jackets vs Buffalo Sabres", "hockey"),
                             self.NHL) is True
        assert eng._reglable(_m("San Jose Sharks vs Florida Panthers", "hockey"),
                             self.NHL) is True

    def test_le_football_reste_tolerant_sur_un_camp(self):
        """« VfB Stuttgart vs 1. FC Köln » : ESPN écrit « FC Cologne », et
        LiveScore règle ce qu'ESPN nomme autrement (mesure du 2026-09-03)."""
        fx = {"soccer": [_ev("VfB Stuttgart", "FC Cologne")]}
        assert eng._reglable(_m("VfB Stuttgart vs 1. FC Köln", "soccer"), fx) is True
