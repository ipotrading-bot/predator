"""
core/learning_layer.py — PAIM v9.5 — Adaptive Thresholds (Bayesian Learning)
Reads last 50 closed signals per sport. Adjusts MIN_EDGE thresholds based on
the REAL win-rate (outcome column: WIN/LOSS from settle_signal()'s web
score lookup) — never on clv_final. clv_final, for settle_signal()-produced
rows, is a re-derivation of the entry edge from the exact same scan-time
prices used to compute edge_pct — it is ~always >= 0 because MIN_EDGE
already rejected negative-edge signals before they were ever sent, so it is
positive independent of whether the bet won or lost. If fewer than 60% of
decisive (WIN/LOSS) bets win, the sport is too noisy and the threshold
rises; above 82% win-rate it can come back down.

Beyond the per-sport win-rate, this module also cross-checks every
raise/lower decision against two independent signals already sitting
unused in ai_learning_ledger (_decide_threshold): the real closing-line CLV
(_clv_stats, from clv_pct_real) and stated-confidence calibration
(_calibration_flag, Brier score via core/stats_utils.py), and layers a
finer (sport, market-family) threshold on top of the sport-wide one
(load_segment_thresholds/_market_family) — a sport's h2h and totals markets
don't necessarily share the same reliability. _edge_band_diagnostic() logs
(does not gate on) whether higher-edge signals actually win more often, the
core assumption the whole system rests on.
"""
import json
import logging
from datetime import datetime, timezone

from core.constants import TAX_RATE as _TAX_RATE
from core.stats_utils import bucket_predictions, brier_score, p_breakeven, wilson_ci

log = logging.getLogger("LEARN")

_SUMMARY_KEY = "learning_summary"


def load_learning_summary(sb) -> list[str]:
    """
    Human-readable lines describing what the last compute_and_save() run
    changed and why (threshold moves, segment moves, edge-band warnings) —
    persisted once per run so Telegram (run_rapport.py) and the dashboard
    (api/index.py) can both show the same explanation without recomputing
    it themselves. Empty list if nothing notable happened last run, or the
    learning layer hasn't run yet.
    """
    try:
        res = sb.table("meta").select("value").eq("key", _SUMMARY_KEY).limit(1).execute()
        if res.data:
            return json.loads(res.data[0]["value"])
    except Exception as e:
        log.warning("load_learning_summary: %s", e)
    return []

# Per-sport baseline — uniquement les sports actifs, seuil relevé à 2.0 % minimum.
# Objectif : moins de signaux, mais plus fiables (gagner ou ne pas jouer).
SPORT_DEFAULTS: dict[str, float] = {
    "soccer":      1.2,   # WC + Copa Lib + MLS + Brasileirão + Amicaux — abaissé pour capturer plus de signaux
    "basketball":  1.5,   # NBA Finals — abaissé ; prob gate 0.65 bloquait tous les signaux
    "hockey":      2.0,   # NHL Cup Finals
    "baseball":    2.0,   # MLB + KBO + NPB — lag timezone documenté
    "rugbyleague": 2.0,   # NRL
    "aussierules": 2.0,   # AFL
    # Sports recherche-web-only (core/harvester.py fetch_mma_events/fetch_esports_events/
    # fetch_alternative_sports_batch) — pas de source OddsAPI dédiée, moins de
    # données de calibration. Absents d'ici jusqu'ici, ils étaient scannés et
    # génèraient des signaux sans jamais participer à l'apprentissage adaptatif
    # (dyn_thresholds.get() retombait silencieusement sur MIN_EDGE global).
    # Seuil conservateur, aligné sur les sports les plus "bruyants" ci-dessus.
    "mma":         2.0,
    "esports":     2.0,
    "tabletennis": 2.0,
    "volleyball":  2.0,
    "handball":    2.0,
}
_THRESHOLD_MIN = 1.0   # Floor soccer — permet de capter amicaux et WC avec petit edge
_THRESHOLD_MAX = 6.0   # Hard cap — relevé de 5.0 pour permettre ajustement sur sports bruyants
_STEP_UP       = 0.4   # Pénalisation plus forte si CLV hit-rate < 60% (relevé 0.3→0.4)
_STEP_DOWN     = 0.2   # Récompense inchangée — prudence sur la baisse de seuil
_MIN_SAMPLES   = 30    # Minimum closed signals before any adjustment (relevé 10→30, Task 4:
                       # 10 decisive samples is not enough to distinguish a real edge from noise)
_TARGET_LO     = 0.60  # Below this  → raise threshold (too many weak signals)
_TARGET_HI     = 0.82  # Above this  → lower threshold (relevé 0.80→0.82 : plus exigeant)

# ── Market-family segmentation (win/loss error analysis) ─────────────
# ai_learning_ledger.market_type stores the raw market_key emitted by
# run_engine.py's _emit() — an exact "h2h", but "totals_over"/"totals_under"
# / "spreads_home"/"spreads_away" for the other two markets (side is baked
# into the key). run_engine.py only ever applies ONE min_edge per
# _process_h2h/_process_totals/_process_spreads call, so segmentation must
# operate at the market-FAMILY level, not the raw market_key — see
# _market_family() below.
_MARKET_FAMILIES = ("h2h", "totals", "spreads")
_SEGMENT_MIN_SAMPLES = 20   # lower bar than _MIN_SAMPLES: a narrower slice of the same sport


def _market_family(market_type: str | None) -> str:
    if not market_type:
        return ""
    if market_type.startswith("totals"):
        return "totals"
    if market_type.startswith("spreads"):
        return "spreads"
    return market_type   # "h2h"


def load_thresholds(sb) -> dict[str, float]:
    """
    Load sport-specific MIN_EDGE from Supabase `meta` table.
    Falls back to SPORT_DEFAULTS if the keys don't exist yet.
    """
    result = SPORT_DEFAULTS.copy()
    try:
        res = sb.table("meta").select("key,value").like("key", "threshold_%").execute()
        for row in (res.data or []):
            sport = row["key"].replace("threshold_", "")
            if sport in result:
                result[sport] = float(row["value"])
    except Exception as e:
        log.warning("load_thresholds: %s — using defaults", e)
    return result


def load_segment_thresholds(sb) -> dict[str, float]:
    """
    Load (sport, market-family) MIN_EDGE overrides from `meta`, keyed as
    "sport:family" (e.g. "soccer:totals"). A missing key means no
    segment-specific override exists yet — callers must fall back to
    load_thresholds()'s coarser sport-level value.

    Stored under a `threshold_seg_` prefix, deliberately distinct from
    load_thresholds()'s `threshold_%` — a bare `threshold_soccer_totals` key
    would otherwise also match that function's LIKE query and parse to
    sport="soccer_totals" (not in SPORT_DEFAULTS, silently ignored there),
    which works by accident rather than by design. Keeping the two
    namespaces separate makes that unambiguous instead of relying on it.
    """
    result: dict[str, float] = {}
    try:
        res = sb.table("meta").select("key,value").like("key", "threshold_seg_%").execute()
        for row in (res.data or []):
            rest = row["key"].replace("threshold_seg_", "", 1)
            for fam in _MARKET_FAMILIES:
                suffix = f"_{fam}"
                if rest.endswith(suffix):
                    sport = rest[:-len(suffix)]
                    try:
                        result[f"{sport}:{fam}"] = float(row["value"])
                    except (TypeError, ValueError):
                        pass
                    break
    except Exception as e:
        log.warning("load_segment_thresholds: %s — no segment overrides", e)
    return result


_DECISIVE_OUTCOMES = ("WIN", "LOSS")   # PUSH/UNKNOWN/closed/expired carry no real result


def _sport_stats(rows: list[dict]) -> dict:
    """
    Real performance stats from a batch of ai_learning_ledger rows.
    Keyed exclusively off `outcome` — never `clv_final` (see module
    docstring). PUSH/UNKNOWN/'closed'/'expired' rows are excluded from both
    hit_rate and ROI: they carry no decisive WIN/LOSS result.

    roi = Σ(kelly_pct·(odds-1)) if WIN else -kelly_pct, / Σ(kelly_pct) —
    kelly_pct stands in for stake size (mise); since it's a % of the same
    fixed reference bankroll for every row, it cancels correctly in the
    ratio without needing the absolute € amount. Rows missing kelly_pct
    (pre-migration, or odds<=0) are skipped for ROI but still count for
    hit_rate.
    """
    decisive = [r for r in rows if r.get("outcome") in _DECISIVE_OUTCOMES]
    n = len(decisive)
    if n == 0:
        return {"n": 0, "hit_rate": None, "roi": None, "wilson_lower": None, "p_breakeven": None}

    wins = sum(1 for r in decisive if r["outcome"] == "WIN")
    hit_rate = wins / n
    wilson_lower, _ = wilson_ci(wins, n)

    odds_vals = [r["odds"] for r in decisive if r.get("odds")]
    avg_odds = sum(odds_vals) / len(odds_vals) if odds_vals else None
    # Explicit constants.TAX_RATE, never p_breakeven's own bare default —
    # this breakeven gates _decide_threshold's lowering decision (below),
    # and a caller relying on the default silently re-introduces the exact
    # bug this fixed: TAX_RATE=0.0 (operator-zeroed) vs the default's 0.20
    # froze every lowering behind a ~25%-harder breakeven than the account's
    # real, configured economics. See core/stats_utils.py's p_breakeven()
    # docstring for why the default itself is left alone rather than
    # imported from constants there.
    breakeven = p_breakeven(avg_odds, _TAX_RATE) if avg_odds else None

    staked = [r for r in decisive if r.get("kelly_pct") and r.get("odds")]
    if staked:
        numer = sum(r["kelly_pct"] * (r["odds"] - 1) if r["outcome"] == "WIN" else -r["kelly_pct"]
                    for r in staked)
        denom = sum(r["kelly_pct"] for r in staked)
        roi = numer / denom if denom else None
    else:
        roi = None

    return {
        "n": n,
        "hit_rate": hit_rate,
        "roi": roi,
        "wilson_lower": wilson_lower,
        "p_breakeven": breakeven,
    }


def _clv_stats(rows: list[dict]) -> dict:
    """
    Real closing-line signal — core/audit_engine.py's capture_closing_lines()
    (hourly, pre-kickoff, see run_closing_line.py) writes the genuine
    market-confirmed CLV into `clv_pct_real`, independently of whether the
    bet later won or lost. It does NOT wait for a graded outcome, so its
    sample is often larger than the decisive WIN/LOSS one.

    Deliberately NOT `was_clv_positive`: that ledger column is `clv > 0`
    computed at insert time (core/db.py:log_to_ledger) from whichever `clv`
    the CALLER passed in — for core/settlement.py's settle_signal() that is
    the entry-edge re-derivation, the exact tautology this module's
    docstring already warns clv_final suffers from (~always >= 0, since
    MIN_EDGE rejected negative-edge signals before they were ever sent), and
    for core/audit_engine.py's Pass-2 fallback it's a proxy fetched hours-to-
    days after kickoff — not the true closing line either. Only rows the
    hourly job actually reached before kickoff carry a real `clv_pct_real`;
    everything else is None and is correctly excluded here, not defaulted.
    """
    real = [r["clv_pct_real"] for r in rows if r.get("clv_pct_real") is not None]
    n = len(real)
    if n == 0:
        return {"n": 0, "avg_clv": None, "positive_rate": None}
    positive = sum(1 for c in real if c > 0)
    return {"n": n, "avg_clv": sum(real) / n, "positive_rate": positive / n}


_EDGE_BUCKETS = [(0.0, 2.0), (2.0, 4.0), (4.0, 8.0), (8.0, 100.0)]


def _edge_band_diagnostic(sport: str, rows: list[dict]) -> str | None:
    """
    Bucket decisive rows by initial_edge and log each bucket's real win
    rate — the founding assumption of this whole system (a bigger edge is
    more likely to be real, not noise) is otherwise never checked against
    actual outcomes. Diagnostic only, never a gate: per-bucket samples are
    too small to safely auto-act on (same reasoning as the Wilson-CI gate
    in _decide_threshold). This exists to surface a systematic pattern —
    e.g. the top bucket, nearest SUSPECT_EDGE (core/constants.py), losing
    MORE than lower buckets, a classic sign of a data/team-matching error
    inflating the apparent edge rather than a real market inefficiency.

    Returns the warning line (also logged) if the top bucket underperforms,
    else None — the caller folds a non-None return into the operator-facing
    learning summary (see load_learning_summary).
    """
    decisive = [r for r in rows
                if r.get("outcome") in _DECISIVE_OUTCOMES and r.get("initial_edge") is not None]
    if len(decisive) < _SEGMENT_MIN_SAMPLES:
        return None

    bucket_wr = []
    for lo, hi in _EDGE_BUCKETS:
        in_b = [r for r in decisive if lo <= r["initial_edge"] < hi]
        if len(in_b) < 5:
            continue
        wins = sum(1 for r in in_b if r["outcome"] == "WIN")
        bucket_wr.append((lo, hi, len(in_b), wins / len(in_b)))
    if len(bucket_wr) < 2:
        return None

    log.info("[%s] Edge-band win rate: %s", sport,
              " | ".join(f"{lo:.0f}-{hi:.0f}%: {wr*100:.0f}% (n={n})" for lo, hi, n, wr in bucket_wr))

    top = bucket_wr[-1]
    others = bucket_wr[:-1]
    best_other = max(wr for *_, wr in others)
    if top[3] < best_other:
        msg = (f"[{sport}] Edge {top[0]:.0f}-{top[1]:.0f}%+ perd plus souvent ({top[3]*100:.0f}%, "
               f"n={top[2]}) que les tranches inférieures (meilleure: {best_other*100:.0f}%) — "
               f"possible erreur de données/matching gonflant l'edge, pas une vraie inefficience")
        log.warning(msg)
        return msg
    return None


def _calibration_flag(rows: list[dict]) -> bool:
    """
    True if the segment's stated confidence (sharp_prob) is systematically
    overconfident — reuses core/stats_utils.py's bucket_predictions(),
    already computed for the /performance dashboard but never fed back into
    a threshold decision. Catches a blind spot raw win-rate can't: a sport
    can sit inside the healthy 60-82% win-rate band while its highest-
    confidence bucket (80%+ stated) quietly wins far less than 80% of the
    time — real decay in the model's OWN probability estimate, not just in
    whether bets happen to land.
    """
    preds = [(r["sharp_prob"], 1 if r["outcome"] == "WIN" else 0)
             for r in rows if r.get("outcome") in _DECISIVE_OUTCOMES and r.get("sharp_prob") is not None]
    if len(preds) < _SEGMENT_MIN_SAMPLES:
        return False
    buckets = bucket_predictions(preds)
    top = buckets.get("80-100%")
    if top and top["n"] >= 10 and top["win_rate"] is not None and top["win_rate"] < 0.65:
        return True
    overall = brier_score(preds)
    return overall is not None and overall > 0.23


def _decide_threshold(old_t: float, stats: dict, clv: dict, overconfident: bool) -> tuple[float | None, str]:
    """
    Shared raise/lower/hold decision, used for both the per-sport threshold
    and each (sport, market-family) segment. Same core rule as before
    (raising is always safe; lowering needs the Wilson-CI significance
    gate), plus two corrections that use signals the raw win-rate can't see:

      - systematic overconfidence (_calibration_flag) forces a raise even
        inside the healthy win-rate band, or blocks what would otherwise be
        a lowering — a sport can look fine on win-rate alone while its own
        stated probabilities are quietly wrong.
      - a real-CLV disagreement (_clv_stats) blocks a would-be lowering: a
        high win-rate the market itself never confirmed (CLV net negative)
        is the classic signature of a lucky streak, not a real edge.
        Conversely, a real-CLV net POSITIVE alongside a low win-rate still
        raises the threshold (raising stays always-safe) but is tagged as
        likely variance rather than genuine edge decay, so the operator can
        tell the two apart in the rapport.

    Returns (new_threshold_or_None, reason). None means "no change".
    """
    hit_rate = stats["hit_rate"]

    if overconfident and not (hit_rate < _TARGET_LO):
        new_t = min(_THRESHOLD_MAX, round(old_t + _STEP_UP, 2))
        return new_t, f"win rate {hit_rate*100:.0f}% ok but overconfident on 80%+ picks → ↑"

    if hit_rate < _TARGET_LO:
        new_t = min(_THRESHOLD_MAX, round(old_t + _STEP_UP, 2))
        tag = ""
        if clv["positive_rate"] is not None and clv["positive_rate"] > 0.5:
            tag = " (CLV réel toujours positif — probable variance, à surveiller)"
        return new_t, f"win rate {hit_rate*100:.0f}% < {_TARGET_LO*100:.0f}% → ↑{tag}"

    if hit_rate > _TARGET_HI:
        if (stats["wilson_lower"] is not None and stats["p_breakeven"] is not None
                and stats["wilson_lower"] < stats["p_breakeven"]):
            return None, (f"win rate {hit_rate*100:.0f}% > target but Wilson lower bound "
                          f"{stats['wilson_lower']*100:.1f}% < breakeven "
                          f"{stats['p_breakeven']*100:.1f}% (n={stats['n']}) — not significant")
        if (clv["positive_rate"] is not None and clv["n"] >= _SEGMENT_MIN_SAMPLES
                and clv["positive_rate"] < 0.5):
            return None, (f"win rate {hit_rate*100:.0f}% > target but real CLV negative "
                          f"({clv['positive_rate']*100:.0f}% positive over {clv['n']} lines) — "
                          f"market isn't confirming this edge, holding")
        new_t = max(_THRESHOLD_MIN, round(old_t - _STEP_DOWN, 2))
        return new_t, f"win rate {hit_rate*100:.0f}% > {_TARGET_HI*100:.0f}% → ↓"

    return None, f"win rate {hit_rate*100:.0f}% in target — no change"


_LEDGER_SELECT = "outcome, kelly_pct, odds, market_type, initial_edge, sharp_prob, clv_pct_real"


def compute_and_save(sb) -> dict[str, float]:
    """
    Re-compute thresholds from real WIN/LOSS history (plus real CLV and
    calibration, see _decide_threshold) and persist them to `meta`. Returns
    the updated PER-SPORT threshold dict unchanged in shape/keys from
    before (callers/tests keying off plain sport names keep working as-is).

    Also computes and persists a finer (sport, market-family) segment
    threshold layer (see load_segment_thresholds()) and logs an edge-band
    win-rate diagnostic — both additive, neither changes this function's
    return value.
    """
    current = load_thresholds(sb)
    updated = current.copy()
    segment_current = load_segment_thresholds(sb)
    now     = datetime.now(timezone.utc).isoformat()
    summary_lines: list[str] = []
    ranking_stats: dict[str, dict] = {}   # sport -> stats, pour _save_sport_ranking

    for sport in SPORT_DEFAULTS:
        try:
            res = (sb.table("ai_learning_ledger")
                   .select(_LEDGER_SELECT)
                   .eq("sport", sport)
                   .order("created_at", desc=True)
                   .limit(50)
                   .execute())
            rows = res.data or []

            stats = _sport_stats(rows)
            ranking_stats[sport] = stats
            if stats["n"] < _MIN_SAMPLES:
                log.info("[%s] %d decisive samples < %d — threshold unchanged (%.1f%%)",
                         sport, stats["n"], _MIN_SAMPLES, current[sport])
            else:
                old_t = current[sport]
                clv = _clv_stats(rows)
                overconfident = _calibration_flag(rows)
                new_t, reason = _decide_threshold(old_t, stats, clv, overconfident)
                if new_t is not None:
                    updated[sport] = new_t
                    roi_str = f"{stats['roi']*100:+.1f}%" if stats["roi"] is not None else "n/a"
                    log.info("[%s] Threshold %.2f%% → %.2f%% | %s | n=%d | ROI %s",
                             sport, old_t, new_t, reason, stats["n"], roi_str)
                    sb.table("meta").upsert({
                        "key":        f"threshold_{sport}",
                        "value":      str(new_t),
                        "updated_at": now,
                    }).execute()
                    summary_lines.append(f"{sport}: seuil {old_t:.2f}% → {new_t:.2f}% ({reason})")
                else:
                    log.info("[%s] %s (%.1f%%)", sport, reason, old_t)

            edge_warning = _edge_band_diagnostic(sport, rows)
            if edge_warning:
                summary_lines.append(edge_warning)

            # Segment layer: same rows, sliced by market family (h2h/totals/
            # spreads) — a narrower question ("is THIS market within this
            # sport reliable?") than the sport-wide aggregate above.
            by_family: dict[str, list[dict]] = {}
            for r in rows:
                fam = _market_family(r.get("market_type"))
                if fam in _MARKET_FAMILIES:
                    by_family.setdefault(fam, []).append(r)

            for fam, fam_rows in by_family.items():
                fam_stats = _sport_stats(fam_rows)
                if fam_stats["n"] < _SEGMENT_MIN_SAMPLES:
                    continue
                dict_key = f"{sport}:{fam}"
                meta_key = f"threshold_seg_{sport}_{fam}"
                seg_old = segment_current.get(dict_key, updated[sport])
                fam_clv = _clv_stats(fam_rows)
                fam_overconfident = _calibration_flag(fam_rows)
                seg_new, seg_reason = _decide_threshold(seg_old, fam_stats, fam_clv, fam_overconfident)
                if seg_new is not None:
                    log.info("[%s/%s] Segment threshold %.2f%% → %.2f%% | %s | n=%d",
                             sport, fam, seg_old, seg_new, seg_reason, fam_stats["n"])
                    sb.table("meta").upsert({
                        "key":        meta_key,
                        "value":      str(seg_new),
                        "updated_at": now,
                    }).execute()
                    summary_lines.append(f"{sport}/{fam}: seuil {seg_old:.2f}% → {seg_new:.2f}% ({seg_reason})")

        except Exception as e:
            log.error("learning_layer [%s]: %s", sport, e)

    try:
        sb.table("meta").upsert({
            "key":        _SUMMARY_KEY,
            "value":      json.dumps(summary_lines[:20]),
            "updated_at": now,
        }).execute()
    except Exception as e:
        log.warning("compute_and_save: failed to persist learning_summary: %s", e)

    _save_sport_ranking(sb, ranking_stats, now)
    return updated


_RANKING_KEY = "sport_ranking"


def _save_sport_ranking(sb, stats_by_sport: dict[str, dict], now: str) -> None:
    """Persiste l'ordre des sports par réussite réelle, pour arbitrer les
    ressources rares côté scan (budget oracle, ordre du rapport).

    Trié sur `wilson_lower` et non sur `hit_rate` : le taux brut est du bruit
    aux volumes observés — un sport à 5 gagnés sur 5 affiche 100% et ne prouve
    rien face à 40 sur 83. La borne basse de Wilson répond à « quel est le pire
    taux compatible avec ce que j'ai vu ? », donc elle pénalise d'elle-même les
    petits échantillons, ce qui est exactement le critère pour trancher entre
    deux sports quand il n'y a de budget que pour un.

    Seuls les sports atteignant _MIN_SAMPLES entrent : classer un sport sur 5
    résultats produirait un ordre qui change à chaque audit. Les autres restent
    absents, et run_engine les traite en position neutre — un sport sans
    historique est INCONNU, pas mauvais, et le reléguer l'empêcherait
    justement d'acquérir l'historique qui le départagerait.
    """
    ranked = sorted(
        ((s, st) for s, st in stats_by_sport.items()
         if st.get("n", 0) >= _MIN_SAMPLES and st.get("wilson_lower") is not None),
        key=lambda kv: kv[1]["wilson_lower"], reverse=True,
    )
    order = [s for s, _ in ranked]
    try:
        sb.table("meta").upsert({
            "key":        _RANKING_KEY,
            "value":      json.dumps(order),
            "updated_at": now,
        }).execute()
        if order:
            log.info("Classement sports (Wilson bas, n>=%d) : %s",
                     _MIN_SAMPLES, " > ".join(order))
        else:
            log.info("Classement sports : aucun sport n'atteint %d résultats "
                     "décisifs — ordre laissé vide, run_engine garde son défaut.",
                     _MIN_SAMPLES)
    except Exception as e:
        log.warning("_save_sport_ranking: %s", e)


def load_sport_ranking(sb) -> list[str]:
    """Ordre des sports par réussite, écrit par _save_sport_ranking.

    Renvoie [] si absent/illisible : l'appelant doit alors garder son ordre
    par défaut plutôt que de deviner.
    """
    try:
        res = (sb.table("meta").select("value")
               .eq("key", _RANKING_KEY).maybe_single().execute())
        if res and res.data:
            order = json.loads(res.data["value"])
            if isinstance(order, list):
                return [str(s) for s in order]
    except Exception as e:
        log.debug("load_sport_ranking: %s", e)
    return []
