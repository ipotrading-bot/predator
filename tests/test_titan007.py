"""
tests/test_titan007.py — core/titan007.py.

Source ajoutée pour combler le trou mesuré le 2026-08-20 : Matchbook est
riche en Amérique du Sud, odds-api.io ne l'est pas, et seuls 8 matchs
avaient les deux côtés. Titan007 couvre ce gisement et apporte le soft ET
le sharp dans la même réponse.

Ce que ces tests verrouillent — chaque point vient d'une mesure réelle :
- le FUSEAU : le calendrier est en UTC+8 et ne le dit pas. Huit heures
  d'erreur, et tout signal est refusé (« match déjà commencé ») ou réglé sur
  le mauvais match ;
- le PLAFOND ANTI-ABERRATION : sur 157 books, le maximum ramasse le book
  figé — un match colombien sortait à 4,59 soft contre 3,58 sharp, un edge
  de 28 % entièrement faux. Le prix retenu est borné par la médiane ;
- le SHARP est pris par ORDRE de préférence (Pinnacle d'abord), jamais au
  mieux-disant : on veut la référence, pas la cote la plus généreuse ;
- le budget journalier partagé, parce que c'est une source TOLÉRÉE et non
  contractuelle : elle doit rester discrète ou disparaître proprement.
"""
import logging

import pytest

import core.titan007 as t7


def _row(sid="123", league="ARG RESL", home="Boca Reserves", away="Tigre Reserves",
         date="8-21", hhmm="04:00", year="2026"):
    """Ligne du calendrier : 69 champs, seuls certains sont lus."""
    row = [""] * 69
    row[0], row[4], row[7], row[10] = sid, league, home, away
    row[11], row[36], row[43] = hhmm, date, year
    return "^".join(row)


def _fixtures_body(rows):
    return "\n".join(f'A[{i}] = "{r}";' for i, r in enumerate(rows))


def _odds_body(books):
    """books : [(nom, o1, ox, o2)] — champs [10:13] = cotes actuelles."""
    recs = []
    for name, o1, ox, o2 in books:
        parts = [""] * 21
        parts[2] = name
        parts[3], parts[4], parts[5] = "1.5", "3.5", "5.5"      # ouverture
        parts[10], parts[11], parts[12] = str(o1), str(ox), str(o2)
        parts[16] = "93.0"
        recs.append('"' + "|".join(parts) + '"')
    return "var game = Array(" + ",".join(recs) + ");\n"


def _wire(monkeypatch, fixtures=None, odds=None, fail=False):
    calls = {"fixtures": 0, "odds": []}

    def fake_get(url):
        if fail:
            return None
        if "bfdata_ut" in url:
            calls["fixtures"] += 1
            return _fixtures_body(fixtures or [])
        calls["odds"].append(url)
        return _odds_body(odds or [])

    monkeypatch.setattr(t7, "_get", fake_get)
    monkeypatch.setattr(t7.time, "sleep", lambda s: None)
    return calls


@pytest.fixture(autouse=True)
def _no_quota(monkeypatch):
    monkeypatch.setattr(t7.daily_quota, "spent", lambda bucket: 0)
    monkeypatch.setattr(t7.daily_quota, "add", lambda bucket, n: None)


# ── Le fuseau ─────────────────────────────────────────────────────────

def test_kickoff_is_converted_from_utc_plus_8():
    """Calibré contre Matchbook : 8-21 04:00 sur le site = 20:00 UTC la
    veille. 12 concordances exactes sur 14 matchs communs."""
    row = _row(date="8-21", hhmm="04:00").split("^")
    assert t7._kickoff_utc(row).isoformat() == "2026-08-20T20:00:00+00:00"


def test_malformed_kickoff_is_refused():
    row = _row(date="oups", hhmm="04:00").split("^")
    assert t7._kickoff_utc(row) is None


def test_offset_is_configurable(monkeypatch):
    monkeypatch.setattr(t7, "SITE_UTC_OFFSET_H", 0)
    row = _row(date="8-21", hhmm="04:00").split("^")
    assert t7._kickoff_utc(row).hour == 4


# ── Prix soft : le book d'exécution, borné par la médiane ─────────────

def test_le_prix_soft_est_celui_du_book_d_execution_pas_le_meilleur():
    """Jusqu'au 2026-09-07, le 4.15 de Bwin aurait été retenu : l'opérateur
    ne mise que chez 1xbet, un prix Bwin n'est pas exécutable."""
    books = {"1xBet": {"1": 4.10, "X": 3.4, "2": 1.9},
             "Bet 365": {"1": 4.05, "X": 3.4, "2": 1.9},
             "Bwin": {"1": 4.15, "X": 3.4, "2": 1.9},
             "Betway": {"1": 4.59, "X": 3.4, "2": 1.9}}          # figé
    assert t7._soft_price(books)["1"] == 4.10


def test_un_book_d_execution_fige_ne_donne_pas_de_prix():
    """Le cas mesuré le 2026-08-20 (4.59 quand le marché est à ~4.1), cette
    fois chez le book d'exécution lui-même : mieux vaut rien."""
    books = {"1xBet": {"1": 4.59, "X": 3.4, "2": 1.9},
             "Bet 365": {"1": 4.05, "X": 3.4, "2": 1.9},
             "Bwin": {"1": 4.15, "X": 3.4, "2": 1.9},
             "Betway": {"1": 4.10, "X": 3.4, "2": 1.9}}
    assert t7._soft_price(books) is None


def test_un_meilleur_prix_ailleurs_nest_pas_pris():
    books = {"1xBet": {"1": 2.00, "X": 3.4, "2": 3.5},
             "Bet 365": {"1": 2.05, "X": 3.4, "2": 3.5},
             "Bwin": {"1": 2.10, "X": 3.4, "2": 3.5}}
    assert t7._soft_price(books)["1"] == 2.00


def test_sans_book_d_execution_pas_de_prix_soft():
    books = {"Bet 365": {"1": 2.05, "X": 3.4, "2": 3.5},
             "Bwin": {"1": 2.10, "X": 3.4, "2": 3.5},
             "Betway": {"1": 2.00, "X": 3.4, "2": 3.5}}
    assert t7._soft_price(books) is None


def test_too_few_soft_books_yields_no_price():
    """Sous trois books, la médiane n'est pas fiable — mieux vaut rien."""
    assert t7._soft_price({"1xBet": {"1": 2.0, "X": 3.4, "2": 3.5}}) is None


def test_unknown_books_are_ignored_entirely():
    books = {"Book Obscur A": {"1": 9.9, "X": 9.9, "2": 9.9},
             "Book Obscur B": {"1": 9.9, "X": 9.9, "2": 9.9},
             "Book Obscur C": {"1": 9.9, "X": 9.9, "2": 9.9}}
    assert t7._soft_price(books) is None


# ── Prix sharp : par ordre, pas au mieux-disant ───────────────────────

def test_sharp_follows_the_preference_order():
    books = {"Matchbook": {"1": 2.50, "X": 3.5, "2": 3.0},
             "Pinnacle": {"1": 2.40, "X": 3.4, "2": 2.9},
             "Betfair Exchange": {"1": 2.55, "X": 3.6, "2": 3.1}}
    assert t7._sharp_price(books)["1"] == 2.40                    # Pinnacle, pas le plus généreux


def test_sharp_falls_back_when_pinnacle_is_absent():
    books = {"Matchbook": {"1": 2.50, "X": 3.5, "2": 3.0}}
    assert t7._sharp_price(books)["1"] == 2.50


def test_no_sharp_book_yields_none():
    assert t7._sharp_price({"1xBet": {"1": 2.0, "X": 3.4, "2": 3.5}}) is None


# ── Bout en bout ──────────────────────────────────────────────────────

_BOOKS = [("Pinnacle", 2.40, 3.40, 2.90), ("1xBet", 2.50, 3.45, 3.00),
          ("Bet 365", 2.48, 3.42, 2.98), ("Bwin", 2.52, 3.44, 3.02)]


def test_upcoming_match_carries_both_sides(monkeypatch):
    from datetime import datetime, timedelta, timezone
    ko = datetime.now(timezone.utc) + timedelta(hours=6, minutes=0)
    site = ko + timedelta(hours=t7.SITE_UTC_OFFSET_H)
    _wire(monkeypatch, [_row(date=f"{site.month}-{site.day}",
                             hhmm=site.strftime("%H:%M"), year=str(site.year))], _BOOKS)
    (m,) = t7.fetch_matches(hours_ahead=24)
    assert m["sport"] == "soccer" and m["sport_id"] == 1
    assert m["odds_pinnacle"]["1"] == 2.40
    assert m["odds_1xbet"]["1"] == 2.50      # 1xBet, pas le 2.52 de Bwin (2026-09-07)
    assert m["commence_time"].endswith("Z")
    assert m["_soft_source"] == "titan007"


def test_past_and_far_matches_are_skipped(monkeypatch):
    from datetime import datetime, timedelta, timezone
    past = datetime.now(timezone.utc) - timedelta(hours=3) + timedelta(hours=t7.SITE_UTC_OFFSET_H)
    far = datetime.now(timezone.utc) + timedelta(hours=100) + timedelta(hours=t7.SITE_UTC_OFFSET_H)
    calls = _wire(monkeypatch,
                  [_row(sid="1", date=f"{past.month}-{past.day}", hhmm=past.strftime("%H:%M"),
                        year=str(past.year)),
                   _row(sid="2", date=f"{far.month}-{far.day}", hhmm=far.strftime("%H:%M"),
                        year=str(far.year))], _BOOKS)
    assert t7.fetch_matches(hours_ahead=24) == []
    assert calls["odds"] == []              # aucun appel payé pour rien


def test_match_cap_is_honoured(monkeypatch):
    from datetime import datetime, timedelta, timezone
    site = datetime.now(timezone.utc) + timedelta(hours=2 + t7.SITE_UTC_OFFSET_H)
    rows = [_row(sid=str(i), date=f"{site.month}-{site.day}",
                 hhmm=site.strftime("%H:%M"), year=str(site.year)) for i in range(20)]
    calls = _wire(monkeypatch, rows, _BOOKS)
    t7.fetch_matches(hours_ahead=24, max_matches=5)
    assert len(calls["odds"]) == 5


def test_daily_budget_stops_the_cycle(monkeypatch, caplog):
    monkeypatch.setattr(t7.daily_quota, "spent", lambda bucket: t7.DAILY_BUDGET)
    calls = _wire(monkeypatch, [_row()], _BOOKS)
    with caplog.at_level(logging.WARNING, logger="PREDATOR.titan007"):
        assert t7.fetch_matches() == []
    assert calls["fixtures"] == 0
    assert any("budget journalier" in r.getMessage() for r in caplog.records)


def test_network_failure_is_swallowed(monkeypatch):
    _wire(monkeypatch, fail=True)
    assert t7.fetch_matches() == []
    assert t7.fetch_fixtures() == []
    assert t7.fetch_odds("1") == {}


def test_le_bilan_compte_les_matchs_sans_cotes(monkeypatch, caplog):
    """Règle 13 : le critère de retrait se MESURE dans la ligne de bilan.
    Relevé du 2026-09-07 : `1x2d.titan007.com/<sid>.js` rend 404 sur une
    partie des matchs du calendrier ; sans ce compteur, seul un grep des
    avertissements le disait, sans dénominateur."""
    from datetime import datetime, timedelta, timezone
    rows = []
    for sid, h in (("1", 5), ("2", 6), ("3", 7)):
        site = datetime.now(timezone.utc) + timedelta(hours=h + t7.SITE_UTC_OFFSET_H)
        rows.append(_row(sid=sid, date=f"{site.month}-{site.day}",
                         hhmm=site.strftime("%H:%M"), year=str(site.year)))
    calls = _wire(monkeypatch, rows, _BOOKS)
    real_get = t7._get

    def flaky_get(url):
        if url.endswith("/2.js"):
            return None                      # 404 → _get rend None
        return real_get(url)

    monkeypatch.setattr(t7, "_get", flaky_get)
    with caplog.at_level(logging.INFO, logger="PREDATOR.titan007"):
        out = t7.fetch_matches(hours_ahead=48)
    assert len(out) == 2 and len(calls["odds"]) == 2
    bilan = [r.getMessage() for r in caplog.records if "demandées" in r.getMessage()]
    assert bilan and "1 sans cotes (404/vide) sur 3 demandées" in bilan[0]


def test_urls_never_carry_a_query_string():
    """robots.txt de bf.titan007.com interdit `/*?*` — ajouter un paramètre
    ferait basculer ces endpoints sous un Disallow."""
    assert "?" not in t7.FIXTURES_URL
    assert "?" not in t7.ODDS_URL


# ── Priorité de ligue avant le cap (2026-08-28) ───────────────────────

def test_les_ligues_majeures_passent_avant_le_cap(monkeypatch):
    """Vendredi 2026-08-28 : 58 coups d'envoi à 18:00 (U21, Welsh PR, POL D3…)
    avant Bayern–Stuttgart 18:30 (GER D1) — position 62, cap 40. Le moteur ne
    voyait que des divisions mineures et sortait à EV −3 à −9 % partout."""
    from datetime import datetime, timedelta, timezone
    base = datetime.now(timezone.utc) + timedelta(hours=2 + t7.SITE_UTC_OFFSET_H)
    mineures = [_row(sid=str(i), league="Welsh PR", home=f"H{i}", away=f"A{i}",
                     date=f"{base.month}-{base.day}", hhmm=base.strftime("%H:%M"),
                     year=str(base.year)) for i in range(10)]
    tard = base + timedelta(minutes=30)
    majeur = _row(sid="bayern", league="GER D1", home="Bayern", away="Stuttgart",
                  date=f"{tard.month}-{tard.day}", hhmm=tard.strftime("%H:%M"),
                  year=str(tard.year))
    calls = _wire(monkeypatch, mineures + [majeur], _BOOKS)
    t7.fetch_matches(hours_ahead=24, max_matches=3)
    assert len(calls["odds"]) == 3
    assert "bayern" in calls["odds"][0], "le majeur est servi en PREMIER, pas coupé"


def test_a_priorite_egale_lordre_reste_celui_des_coups_denvoi(monkeypatch):
    from datetime import datetime, timedelta, timezone
    base = datetime.now(timezone.utc) + timedelta(hours=2 + t7.SITE_UTC_OFFSET_H)
    rows = []
    for i, delta in enumerate((90, 30, 60)):
        w = base + timedelta(minutes=delta)
        rows.append(_row(sid=f"m{delta}", league="Welsh PR", home=f"H{i}", away=f"A{i}",
                         date=f"{w.month}-{w.day}", hhmm=w.strftime("%H:%M"), year=str(w.year)))
    calls = _wire(monkeypatch, rows, _BOOKS)
    t7.fetch_matches(hours_ahead=24)
    ordre = [next(s for s in ("m30", "m60", "m90") if s in u) for u in calls["odds"]]
    assert ordre == ["m30", "m60", "m90"]


# ── Règle 13 : critère de retrait daté, tenu dans le module (2026-09-07) ─

def test_le_critere_de_retrait_est_ecrit_et_la_source_est_au_registre():
    """AUDIT.md §3bis : une source n'entre (ni ne reste) qu'avec un critère
    de retrait DATÉ dans sa docstring, et sort du registre le jour où il est
    relevé — avec sa ligne dans INCIDENTS.md. Ce test tombe si l'un des deux
    bouge sans l'autre : retirer titan007 de CALL_ORDER sans consigner la
    mesure, ou effacer le critère en gardant la source."""
    from core.source_adapter import CALL_ORDER
    doc = t7.__doc__ or ""
    assert "CRITÈRE DE RETRAIT" in doc
    assert "2026-09-07" in doc.split("CRITÈRE DE RETRAIT", 1)[1]
    assert "titan007" in CALL_ORDER
