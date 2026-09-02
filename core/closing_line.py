"""
core/closing_line.py — closing-line capture off the scan feed itself.

WHY THIS EXISTS
---------------
core/audit_engine.py's capture_closing_lines() prices signals with the
web-search oracle (core/oracle.py), which only ever returns ONE number: the
ML/DNB favourite's price. So totals_over/totals_under/spreads_home/
spreads_away were skipped before spending any budget — they could never get
a `clv_pct_real`, on any schedule. That is exactly backwards from where the
money goes: measured 2026-08-01, totals+spreads win 38.9% (n=95) against
58.1% for h2h (n=74), z=2.48. The markets that most need the closing line to
confirm or kill them were the only ones we could not measure.

The fix is not a second data source. Every scan (golden_hour/engine/
deep_scan) ALREADY downloads Pinnacle+Circa+CRIS h2h, totals AND spreads for
every event it looks at — core/odds_api.py parses all three and run_engine.py
throws away everything it doesn't emit a signal on. Golden Hour's window is
T-120min, i.e. squarely inside the closing-line neighbourhood. So capture is
a free rider on data already paid for: no extra OddsAPI request, no extra
quota, no extra scheduler to be unreliable. Whichever scan happens to be the
last one before kickoff leaves behind the closing price, per market, on the
exact line we bet.

CONTRACT
--------
  - clv_pct_real = (xbet_odd / closing_price_of_the_SAME_side_and_line − 1)
    × 100. Positive = the price we got beat the close. Same formula and sign
    convention as the oracle path, so core/learning_layer.py's _clv_stats()
    consumes both without knowing which produced a row.
  - The closing price is built with the SAME maths as the entry price in
    run_engine.py's _process_* (consensus over pinnacle/circa/cris; Power-
    devigged DNB for soccer h2h). A closing price computed differently from
    the entry price would put drift in the formula that isn't market
    movement.
  - A market whose LINE moved (Over 2.5 → Over 2.75, −1.5 → −2.0) is not the
    bet we made. We refuse to grade it rather than compare two different
    bets: nothing is written, and a LINEMOVE line is logged. Quantifying a
    line move as a price delta needs a model this repo doesn't have.
  - Writes go through update_signal_fields() (plain UPDATE). Never
    update_signal_fields() — this runs repeatedly on a still-live signal and
    delete-then-insert would expose the row to loss, and hand it a new id,
    on every single refresh.
"""
import logging
import re
from datetime import datetime, timedelta, timezone

from core.constants import (CLOSING_LINE_COLS, CLOSING_LINE_WINDOW_MIN,
                            CLOSING_SRC_EXCHANGE, CLOSING_SRC_ODDSAPI)
from core.exchange_match import lookup_exchange
from core.db import update_signal_fields
from core.math_engine import calc_dnb
from core.paim_engine import calculate_consensus_price, resolve_selection_side

log = logging.getLogger("PREDATOR.closing")

# A closing price only counts if it is for the same line. 0.01 absorbs float
# noise from the "2.5" → 2.5 round trip, nothing else: a 0.25 line move is a
# different bet.
LINE_TOLERANCE = 0.01

# PostgREST `in.(...)` goes in the query string; chunk the id list so a scan
# returning many near-kickoff events can't build a URL the server rejects.
_ID_CHUNK = 40

_TRAILING_NUMBER = re.compile(r"(-?\+?\d+(?:\.\d+)?)\s*$")


def _parse_time(value) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _selection_point(selection_name: str) -> float | None:
    """The line embedded in a selection: "Over 2.5" → 2.5, "PSG -1.5" → -1.5.

    run_engine._emit stores the line only inside this string (there is no
    dedicated column), so this is the only way to tell whether the closing
    market is still quoting the bet we made. Returns None when no number is
    present — the caller must then refuse to grade rather than assume.
    """
    m = _TRAILING_NUMBER.search(selection_name or "")
    if not m:
        return None
    try:
        return float(m.group(1).replace("+", ""))
    except ValueError:
        return None


def _consensus(sources: dict, sport: str) -> float:
    """Weighted sharp consensus, falling back to Pinnacle alone.

    run_engine discards a VOLATILE market (sources disagree beyond the CV
    limit) because it will not BET into one. Here the bet already exists and
    we are only measuring where it closed — Pinnacle on its own is still a
    valid closing reference, and dropping the measurement would silently
    reintroduce the "no CLV for this market" hole this module exists to fill.
    """
    price, _, is_volatile, _ = calculate_consensus_price(sources, sport)
    if price > 1.01 and not is_volatile:
        return price
    pin = float(sources.get("pinnacle") or 0.0)
    return pin if pin > 1.01 else 0.0


def _sources_for(event: dict, prefix: str, side_key: str) -> dict:
    """{"pinnacle": 1.95, "circa": 1.97, ...} for one side of one market."""
    out: dict = {}
    for src in ("pinnacle", "circa", "cris"):
        block = event.get(f"{prefix}_{src}") or {}
        price = float(block.get(side_key, 0) or 0)
        if price > 1.01:
            out[src] = price
    return out


def capture_from_exchange(sb, matches: list[dict], exchange_prices: dict,
                          now: datetime | None = None,
                          window_min: int = CLOSING_LINE_WINDOW_MIN) -> int:
    """Price active h2h signals off the exchange prices this scan already
    downloaded. Returns the number of signals updated.

    WHY. capture_from_scan() above rides the OddsAPI payload — dead since
    2026-08-26 (ODDS_API_ENABLED defaults to 0), so the only remaining path
    was audit_engine's web-search oracle: h2h favourite only, on the scans'
    own Groq budget. Meanwhile every scan, REPRICE included, already fetches
    sharp Matchbook prices for the edge computation and throws them away for
    closing purposes. This is a free, exact, hourly closing price.

    ⚠️ IT TAKES THE RAW PRICE DICT, NOT THE ENRICHED MATCHES, AND THAT IS THE
    WHOLE POINT. run_engine._enrich_from_exchange only overwrites
    `odds_pinnacle` on matches that have NO sharp price:

        if pin["1"] > 1.01 and pin["2"] > 1.01 and not m["_estimated"]: continue

    api-sports serves Pinnacle on 100% of its football fixtures, and 100% of
    this repo's signals are football — so on precisely the matches that carry
    signals, `odds_pinnacle` is still the api-sports Pinnacle and `_exchange`
    is never set. Reading the enriched match would therefore store the ENTRY
    price as the closing price: clv_pct_real ≈ 0 on every row, a green run,
    and a silent lie. We look the market up ourselves instead.

    h2h ONLY. Matchbook's totals/spreads do reach `_enrich_from_exchange`, but
    only as a fallback when the soft feed had none, and _line_market_close
    needs the same LINE we bet — which the exchange payload does not carry per
    signal. Grading those would compare two different bets; the oracle path's
    refusal (see _line_market_close) applies here too.
    """
    if not sb or not matches or not exchange_prices:
        return 0
    now = now or datetime.now(timezone.utc)
    horizon = now + timedelta(minutes=window_min)

    # `kickoff >= now` is load-bearing, same as capture_from_scan: once the
    # match has started the exchange quotes an in-play price.
    in_window: dict[str, tuple] = {}
    sans_cote = 0
    for m in matches:
        kickoff = _parse_time(m.get("commence_time"))
        if not kickoff or not (now <= kickoff <= horizon):
            continue
        mid = str(m.get("id") or "")
        if not mid:
            continue
        row = lookup_exchange(m, exchange_prices)
        if not (row and float(row.get("1") or 0) > 1.01 and float(row.get("2") or 0) > 1.01):
            # Branche muette jusqu'au 2026-09-02 : un signal MLB à T-20 min
            # a perdu sa clôture ici sans une ligne de log, alors que le
            # scan venait d'apparier le même match sur Matchbook. Un compte
            # agrégé suffit — logger chaque match sans cote spammerait
            # (l'exchange ne cote pas tout, c'est l'état normal).
            sans_cote += 1
            continue
        in_window[mid] = (m, row, kickoff)
    if sans_cote:
        log.info("CLOSE SKIP | %d match(s) en fenêtre sans cote exchange "
                 "exploitable (appariement raté ou marché non coté)", sans_cote)
    if not in_window:
        return 0

    signals = _fetch_signals(sb, list(in_window))
    if not signals:
        return 0

    captured = 0
    for sig in signals:
        if (sig.get("market_key") or "") != "h2h":
            continue
        entry = in_window.get(str(sig.get("match_id") or ""))
        if not entry:
            continue
        match, row, kickoff = entry
        try:
            price = _exchange_h2h_close(sig, match, row)
        except Exception as e:                          # never let one odd
            log.warning("exchange close [%s]: %s", sig.get("match"), e)   # payload kill the scan
            continue
        if not price:
            continue

        xbet_odd = float(sig.get("xbet_odd") or 0.0)
        if xbet_odd <= 1.01:
            continue
        clv_real = round((xbet_odd / price - 1) * 100, 2)

        ok = update_signal_fields(sb, sig["id"], {
            "closing_pinnacle_price": round(float(price), 4),
            "clv_pct_real":           clv_real,
            "closing_captured_at":    now.isoformat(),
            "closing_source":         CLOSING_SRC_EXCHANGE,
        }, optional_cols=frozenset(CLOSING_LINE_COLS))
        if ok:
            captured += 1
            log.info("CLOSING LINE | %s | h2h %s | bet %.3f -> close %.3f | CLV_real %+.2f%% "
                     "| T-%s | %s", sig.get("match"), sig.get("selection_name"),
                     xbet_odd, price, clv_real, _lead_time(kickoff, now),
                     row.get("_source", "exchange"))
        else:
            log.error("Failed to persist exchange closing line for signal %s", sig.get("id"))

    if captured:
        log.info("📉 Closing line (exchange): %d signal(s) priced — 0 extra request", captured)
    return captured


def _exchange_h2h_close(sig: dict, match: dict, row: dict) -> float | None:
    """Closing price of the side this h2h signal backs, from an exchange row.

    Same contract as _h2h_close: resolve our selection against the match's own
    team names, then re-derive the price the way _process_h2h did at ENTRY —
    DNB for football, raw ML otherwise. An exchange row carries a single book,
    so there is no consensus to compute; the price IS the reference.

    ⚠️ FOOTBALL WITHOUT A DRAW PRICE IS REFUSED, NOT DOWNGRADED. Matchbook
    quotes 1/X/2 (core/matchbook.py, one_x_two market) but the enrichment path
    tolerates X = 0.0, and calc_dnb returns 0.0 on a missing draw. Falling back
    to the raw moneyline would compare a DNB entry price against an ML close —
    a wrong CLV, silently, on the sport that carries every signal we have.
    """
    side = resolve_selection_side(sig.get("selection_name") or "",
                                  match.get("home", ""), match.get("away", ""))
    if side is None:
        log.info("CLOSE SKIP | %s h2h — selection '%s' resolves to neither side",
                 sig.get("match"), sig.get("selection_name"))
        return None

    ours, theirs = ("1", "2") if side else ("2", "1")
    sport = (sig.get("sport") or match.get("sport") or "").lower()
    if sport == "soccer":
        draw = float(row.get("X") or 0.0)
        if draw <= 1.01:
            log.info("CLOSE SKIP | %s h2h — exchange sans prix de nul, un DNB ne peut "
                     "pas se comparer à un moneyline", sig.get("match"))
            return None
        price = calc_dnb(float(row.get(ours) or 0.0), float(row.get(theirs) or 0.0), draw)
    else:
        price = float(row.get(ours) or 0.0)
    return price if price > 1.01 else None


def _h2h_close(sig: dict, event: dict, sport: str) -> float | None:
    """Closing price of the side this h2h signal actually backs.

    Resolves our selection against the event's own two team names rather
    than trusting position, and re-derives the price the same way
    _process_h2h did at entry (DNB for soccer, raw ML otherwise). Unlike the
    oracle path this does NOT care whether our side is still the favourite —
    both sides are in the payload, so a flipped favourite is priced normally
    instead of yielding clv_pct_real=None.
    """
    side = resolve_selection_side(sig.get("selection_name") or "",
                                  event.get("home", ""), event.get("away", ""))
    if side is None:
        log.info("CLOSE SKIP | %s h2h — selection '%s' resolves to neither side",
                 sig.get("match"), sig.get("selection_name"))
        return None

    ours, theirs = ("1", "2") if side else ("2", "1")
    if sport == "soccer":
        sources = {}
        for src in ("pinnacle", "circa", "cris"):
            block = event.get(f"odds_{src}") or {}
            dnb = calc_dnb(float(block.get(ours, 0) or 0),
                           float(block.get(theirs, 0) or 0),
                           float(block.get("X", 0) or 0))
            if dnb > 1.01:
                sources[src] = dnb
    else:
        sources = _sources_for(event, "odds", ours)
    price = _consensus(sources, sport)
    return price or None


def _line_market_close(sig: dict, event: dict, sport: str,
                       prefix: str, side_key: str, point_key: str) -> float | None:
    """Closing price for a totals/spreads side, refusing a moved line."""
    pin = event.get(f"{prefix}_pinnacle") or {}
    if not pin:
        return None

    bet_point   = _selection_point(sig.get("selection_name") or "")
    close_point = pin.get(point_key)
    if close_point is None and point_key == "away_point":
        # _extract_spreads only records away_point when both sides were
        # present; the away line is the mirror of the home one.
        home_point = pin.get("point")
        close_point = -float(home_point) if home_point is not None else None
    if bet_point is None or close_point is None:
        log.info("CLOSE SKIP | %s %s — line unknown (bet=%s close=%s)",
                 sig.get("match"), sig.get("market_key"), bet_point, close_point)
        return None
    if abs(float(close_point) - bet_point) > LINE_TOLERANCE:
        log.info("LINEMOVE | %s %s — bet %.2f closed %.2f, not the same bet — no CLV",
                 sig.get("match"), sig.get("market_key"), bet_point, float(close_point))
        return None

    price = _consensus(_sources_for(event, prefix, side_key), sport)
    return price or None


def closing_price_for(sig: dict, event: dict) -> float | None:
    """Sharp closing price for this signal's exact market/side/line, or None
    when the scan payload cannot answer for that bet."""
    sport = sig.get("sport") or event.get("sport") or "soccer"
    mkey  = sig.get("market_key") or ""
    if mkey == "h2h":
        return _h2h_close(sig, event, sport)
    if mkey in ("totals_over", "totals_under"):
        return _line_market_close(sig, event, sport, "totals",
                                  mkey.split("_")[1], "point")
    if mkey in ("spreads_home", "spreads_away"):
        side = mkey.split("_")[1]
        return _line_market_close(sig, event, sport, "spreads", side,
                                  "point" if side == "home" else "away_point")
    return None


def _fetch_signals(sb, match_ids: list[str]) -> list[dict]:
    rows: list[dict] = []
    for i in range(0, len(match_ids), _ID_CHUNK):
        chunk = match_ids[i:i + _ID_CHUNK]
        try:
            res = (sb.table("signals")
                   .select("*")
                   .eq("status", "active")
                   .in_("match_id", chunk)
                   .limit(200)
                   .execute())
            rows.extend(res.data or [])
        except Exception as e:
            log.warning("closing-line signal fetch: %s", e)
    return rows


def _lead_time(kickoff: datetime | None, now: datetime) -> str:
    """"37min" / "2h14" — how far ahead of kickoff this price was taken.
    Logged on every capture so a sparse run is visible in the job output
    rather than hidden behind a column called closing_pinnacle_price."""
    if not kickoff:
        return "?"
    mins = int((kickoff - now).total_seconds() // 60)
    if mins < 0:
        return "?"
    return f"{mins}min" if mins < 60 else f"{mins // 60}h{mins % 60:02d}"


def capture_from_scan(sb, events: list[dict], now: datetime | None = None,
                      window_min: int = CLOSING_LINE_WINDOW_MIN) -> int:
    """Price every active signal whose event is in this scan and kicks off
    within `window_min`. Returns the number of signals updated.

    Called from run_engine.run() right after fetch_odds(), on the raw event
    list (not the MAX_MATCHES-truncated one, and not the portfolio-balanced
    signals): measuring a bet we already placed has nothing to do with how
    many new bets this scan is allowed to emit.

    Re-running is the point, not a waste: each scan overwrites the previous
    price, so the last scan before kickoff wins and the stored value
    converges on the true close. `closing_captured_at` records when the
    surviving price was taken, so a consumer can tell a T-10min close from a
    T-3h price instead of trusting the column name.
    """
    if not sb or not events:
        return 0
    now = now or datetime.now(timezone.utc)
    horizon = now + timedelta(minutes=window_min)

    # `kickoff >= now` is load-bearing: once a match has started the feed
    # carries live/in-play prices, which are not a closing line.
    in_window = {}
    for ev in events:
        kickoff = _parse_time(ev.get("commence_time"))
        if not kickoff or not (now <= kickoff <= horizon):
            continue
        if ev.get("id"):
            in_window[str(ev["id"])] = (ev, kickoff)
    if not in_window:
        return 0

    signals = _fetch_signals(sb, list(in_window))
    if not signals:
        return 0

    captured = 0
    for sig in signals:
        entry = in_window.get(str(sig.get("match_id") or ""))
        if not entry:
            continue
        event, kickoff = entry
        try:
            price = closing_price_for(sig, event)
        except Exception as e:                      # never let one odd payload
            log.warning("closing price [%s %s]: %s",                # kill the scan
                        sig.get("match"), sig.get("market_key"), e)
            continue
        if not price:
            continue

        xbet_odd = float(sig.get("xbet_odd") or 0.0)
        if xbet_odd <= 1.01:
            continue
        clv_real = round((xbet_odd / price - 1) * 100, 2)

        ok = update_signal_fields(sb, sig["id"], {
            "closing_pinnacle_price": round(float(price), 4),
            "clv_pct_real":           clv_real,
            "closing_captured_at":    now.isoformat(),
            "closing_source":         CLOSING_SRC_ODDSAPI,
        }, optional_cols=frozenset(CLOSING_LINE_COLS))
        if ok:
            captured += 1
            log.info("CLOSING LINE | %s | %s %s | bet %.3f -> close %.3f | CLV_real %+.2f%% | T-%s",
                     sig.get("match"), sig.get("market_key"), sig.get("selection_name"),
                     xbet_odd, price, clv_real, _lead_time(kickoff, now))
        else:
            log.error("Failed to persist closing line for signal %s", sig.get("id"))

    if captured:
        log.info("📉 Closing line (scan feed): %d signal(s) priced — 0 extra OddsAPI credits",
                 captured)
    return captured
