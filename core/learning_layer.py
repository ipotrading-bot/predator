"""
core/learning_layer.py — PAIM v9.5 — Adaptive Thresholds (Bayesian Learning)
Lit les 120 dernières lignes de ledger par sport, n'en garde que la ZONE
JOUABLE (playable_rows : 2-24h avant le coup d'envoi, la seule que le système
recommande encore), et ajuste les planchers MIN_EDGE sur le win-rate RÉEL
(colonne `outcome`, WIN/LOSS issus de settle_signal()) — jamais sur clv_final.
clv_final, pour les lignes produites par settle_signal(), est une
re-dérivation de l'edge d'entrée à partir des mêmes prix de scan que
edge_pct : il est ~toujours >= 0 puisque MIN_EDGE a déjà rejeté les edges
négatifs avant émission, donc positif que le pari ait gagné ou perdu.

Le critère d'ajustement est la RENTABILITÉ mesurée du sport (p_breakeven :
cote moyenne + TAX_RATE), pas un taux absolu. Les bornes fixes 60%/82% qui
servaient jusqu'au 2026-08-06 formaient un cliquet à sens unique — la montée
se déclenchait toujours, la descente jamais — qui a envoyé les planchers au
plafond dur puis réduit l'émission à ~1 signal/jour. Voir _decide_threshold.

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
import os
from datetime import datetime, timezone

from core.constants import TAX_RATE as _TAX_RATE, roi_net_of_tax
from core.stats_utils import (bucket_predictions, brier_reference, brier_score,
                              p_breakeven, wilson_ci)

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
    # Sports de combat — flux OddsAPI réel depuis le 2026-08-22 (Phase 1) ;
    # seuil conservateur tant que le ledger n'a pas tranché. eSports/
    # tabletennis/volleyball/handball retirés le même jour (RETIRED_SPORTS,
    # core/constants.py) : plus d'apprentissage pour eux, leurs lignes
    # historiques restent lisibles.
    "mma":         2.0,
    "boxing":      2.0,
    # Phase 2 (2026-08-22) — nouveaux flux, seuil conservateur au départ.
    "americanfootball":      2.0,
    "euroleague_basketball": 2.0,
    # Phase 3 (2026-08-22) — NCAAF + tennis majeur, même prudence.
    "college_football":      2.0,
    "tennis":                2.0,
}
_THRESHOLD_MIN = 1.0   # Floor soccer — permet de capter amicaux et WC avec petit edge
_THRESHOLD_MAX = 6.0   # Hard cap — relevé de 5.0 pour permettre ajustement sur sports bruyants
_STEP_UP       = 0.4   # Pénalisation plus forte si CLV hit-rate < 60% (relevé 0.3→0.4)
_STEP_DOWN     = 0.2   # Récompense inchangée — prudence sur la baisse de seuil
_MIN_SAMPLES   = 20    # Minimum closed signals before any adjustment. Ramené de 30 à 20 le
                       # 2026-08-06 en même temps que le filtre de zone jouable ci-dessous :
                       # celui-ci retire ~55% des lignes, et à 30 il ne restait plus AUCUN
                       # sport ajustable hors soccer (basketball 26, baseball 22) — le
                       # mécanisme se serait tu au lieu de se corriger. L'échantillon
                       # filtré est en outre homogène (même régime de jeu), donc 20 lignes
                       # y valent mieux que 30 lignes mélangeant trois régimes.
_TARGET_LO     = 0.60  # Conservé pour la seule garde de surconfiance (_calibration_flag).
                       # N'est PLUS le critère de montée du seuil — voir _decide_threshold.
_TARGET_HI     = 0.82  # Idem : conservé pour compatibilité, plus utilisé comme critère.

# ── Le CLV réel comme critère de PREMIER rang (2026-08-22) ───────────────
# L'incertitude du CLV à n donné est ~4,2 points contre ~7,1 pour le
# win-rate : il converge ~3x plus vite vers le verdict. D'où une barre
# d'échantillon PLUS BASSE que _SEGMENT_MIN_SAMPLES (20) — c'est voulu, pas
# une incohérence à « harmoniser » : le garde existant sur positive_rate
# (descente bloquée) garde sa barre à 20, ces deux règles-ci jugent
# l'AMPLITUDE (avg_clv), statistiquement plus riche qu'un simple taux.
_CLV_MIN_SAMPLES = 15
_CLV_STRONG      = 1.0   # % — au-delà de ±1, le marché a tranché

# ── Promotion / rétrogradation d'un sport (Phase 4 du recentrage, 2026-08-22) ──
# Critères CHIFFRÉS, loggés dans meta (`sport_verdict_<sport>`), jamais
# appliqués automatiquement à KELLY_FRACTION : la restauration d'une fraction
# Kelly et le retrait d'un sport sont des décisions opérateur. La barre est
# 30 signaux réglés (et non _MIN_SAMPLES=20, qui sert aux seuils) : on ne
# change pas la TAILLE des mises sur l'échantillon qui sert à régler les
# PLANCHERS — plus de preuve pour plus d'argent.
_PROMOTION_MIN_SAMPLES = 30
_VERDICT_KEY_PREFIX    = "sport_verdict_"


def sport_verdict(stats: dict, clv: dict | None = None) -> dict:
    """Verdict d'un sport d'après ses résultats RÉELS (outcome, jamais
    clv_final) et son CLV réel :
      - insuffisant          : n < 30 ;
      - promotion_eligible   : borne basse de Wilson > rentabilité post-taxe
                               → éligible à la restauration PROGRESSIVE de sa
                               fraction Kelly d'origine (voir KELLY_FRACTION) ;
      - perte_prouvee        : borne HAUTE < rentabilité — retrait proposé ;
      - non_demontre         : n ≥ 30 et l'intervalle chevauche encore la
                               rentabilité — edge non démontré, retrait proposé
                               au rapport hebdo (décision opérateur).
    `avg_clv` est joint à titre d'éclairage (le CLV réel converge plus vite)."""
    n = stats.get("n", 0) or 0
    lo, hi, be = stats.get("wilson_lower"), stats.get("wilson_upper"), stats.get("p_breakeven")
    avg_clv = (clv or {}).get("avg_clv")
    out = {"n": n, "wilson_lower": lo, "wilson_upper": hi, "p_breakeven": be,
           "hit_rate": stats.get("hit_rate"), "roi": stats.get("roi"),
           "avg_clv": avg_clv, "clv_n": (clv or {}).get("n", 0)}
    if n < _PROMOTION_MIN_SAMPLES or lo is None or be is None:
        out.update(status="insuffisant", retrait_propose=False,
                   reason=f"{n}/{_PROMOTION_MIN_SAMPLES} signaux réglés")
    elif lo > be:
        out.update(status="promotion_eligible", retrait_propose=False,
                   reason=f"Wilson bas {lo*100:.1f}% > rentabilité {be*100:.1f}% (n={n})")
    elif hi is not None and hi < be:
        out.update(status="perte_prouvee", retrait_propose=True,
                   reason=f"Wilson haut {hi*100:.1f}% < rentabilité {be*100:.1f}% (n={n})")
    else:
        out.update(status="non_demontre", retrait_propose=True,
                   reason=(f"IC [{lo*100:.1f}–{(hi or 0)*100:.1f}%] chevauche la "
                           f"rentabilité {be*100:.1f}% après {n} réglés"))
    return out


def _save_sport_verdicts(sb, stats_by_sport: dict[str, dict],
                         clv_by_sport: dict[str, dict], now: str) -> list[str]:
    """Persiste un verdict par sport dans meta et rend les lignes de résumé
    (reprises par run_rapport.py via learning_summary — c'est l'alerte
    Telegram rapide ; le rapport hebdo formalise la proposition)."""
    lines: list[str] = []
    for sport, stats in stats_by_sport.items():
        v = sport_verdict(stats, clv_by_sport.get(sport))
        v["computed_at"] = now
        try:
            sb.table("meta").upsert({
                "key":        f"{_VERDICT_KEY_PREFIX}{sport}",
                "value":      json.dumps(v),
                "updated_at": now,
            }).execute()
        except Exception as e:
            log.warning("sport_verdict[%s]: %s", sport, e)
        if v["status"] == "promotion_eligible":
            lines.append(f"{sport}: ✅ edge validé ({v['reason']}) — éligible à la "
                         f"restauration progressive de sa fraction Kelly")
        elif v["retrait_propose"]:
            lines.append(f"{sport}: ⚠️ retrait proposé — {v['reason']} "
                         f"(décision opérateur, rien d'automatique)")
    return lines


def load_sport_verdicts(sb) -> dict[str, dict]:
    """{sport: verdict} depuis meta — pour le rapport hebdo et le dashboard."""
    out: dict[str, dict] = {}
    try:
        res = sb.table("meta").select("key,value").like("key", f"{_VERDICT_KEY_PREFIX}%").execute()
        for row in res.data or []:
            try:
                out[row["key"][len(_VERDICT_KEY_PREFIX):]] = json.loads(row["value"])
            except (ValueError, TypeError):
                continue
    except Exception as e:
        log.warning("load_sport_verdicts: %s", e)
    return out

# ── Zone jouable — ce sur quoi le moteur apprend ─────────────────────
#
# L'apprentissage ne doit porter que sur les paris que le système RECOMMANDE
# encore. Mesuré le 2026-08-06 sur les 204 paris réglés du ledger :
#
#   avance 2-24h  : 91 paris,  60,4% de réussite pour 56,0% requis, ROI  +9,4%
#   hors 2-24h    : 113 paris, 41,6% pour 56,0% requis, ROI -28,5%, p=0,002
#
# Le hors-zone est le SEUL segment significatif du ledger, et il n'est plus
# jouable : >24h ne sort plus du scan (fenêtre ramenée à 24h, commit 1552f1d)
# et <2h part en fantôme (SHADOW_GOLDEN_HOUR dans run_engine.py). Le laisser
# dans l'apprentissage faisait donc monter les seuils à cause de pertes que le
# système ne subit plus — soccer était jugé sur 50,0% de réussite alors que sa
# zone jouable en fait 65,1%.
#
# Bornes alignées sur run_engine.py (SHADOW_GOLDEN_HOUR à T-2h, fenêtre de scan
# à 24h) : si l'une des deux bouge, bouger l'autre.
_PLAYABLE_MIN_MINUTES = 120     # T-2h — en deçà, c'est la golden hour, en fantôme
_PLAYABLE_MAX_MINUTES = 1440    # T-24h — au-delà, le scan ne va plus chercher

# Écart de calibration toléré (probabilité annoncée − taux réalisé), en points.
# Voir _calibration_flag pour la mesure qui a fixé cette valeur.
_CALIBRATION_MAX_GAP = 0.10

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


def load_edge_ceilings(sb) -> dict[str, float]:
    """Plafonds d'edge appris par sport (`edge_ceiling_<sport>` dans `meta`).

    Au-dessus de ce plafond, l'edge mesuré n'est pas une inefficience de marché
    mais un prix mal apparié ou périmé : la bande haute perd davantage que les
    bandes basses (voir _top_band_verdict). Un sport absent n'a pas de plafond —
    l'appelant garde alors les bornes globales de core/constants.py.
    """
    result: dict[str, float] = {}
    try:
        res = sb.table("meta").select("key,value").like("key", "edge_ceiling_%").execute()
        for row in (res.data or []):
            sport = row["key"].replace("edge_ceiling_", "", 1)
            try:
                result[sport] = float(row["value"])
            except (TypeError, ValueError):
                pass
    except Exception as e:
        log.warning("load_edge_ceilings: %s — aucun plafond appris", e)
    return result


def load_odds_ceilings(sb) -> dict[str, float]:
    """Plafonds de COTE appris par sport (`odds_ceiling_<sport>` dans `meta`).

    Une sélection cotée au-dessus n'est pas un favori : c'est un quasi
    pile-ou-face que le ledger a prouvé perdant pour ce sport. Sport absent =
    aucun plafond, comportement inchangé.
    """
    result: dict[str, float] = {}
    try:
        res = sb.table("meta").select("key,value").like("key", "odds_ceiling_%").execute()
        for row in (res.data or []):
            sport = row["key"].replace("odds_ceiling_", "", 1)
            try:
                result[sport] = float(row["value"])
            except (TypeError, ValueError):
                pass
    except Exception as e:
        log.warning("load_odds_ceilings: %s — aucun plafond de cote", e)
    return result


_DECISIVE_OUTCOMES = ("WIN", "LOSS")   # PUSH/UNKNOWN/closed/expired carry no real result


def _sport_stats(rows: list[dict]) -> dict:
    """
    Real performance stats from a batch of ai_learning_ledger rows.
    Keyed exclusively off `outcome` — never `clv_final` (see module
    docstring). PUSH/UNKNOWN/'closed'/'expired' rows are excluded from both
    hit_rate and ROI: they carry no decisive WIN/LOSS result.

    Le ROI est NET DE TAXE et vient de `core.constants.roi_net_of_tax` —
    formule unique du dépôt. Il était calculé ICI, en BRUT, jusqu'au
    2026-08-27 : la couche qui décide de monter ou de baisser un seuil jugeait
    donc la rentabilité sur des gains que l'opérateur n'encaisse pas. Sur un
    portefeuille à cote moyenne 1,85, la retenue de 20 % coûte environ 17
    points de ROI — largement de quoi faire passer un sport perdant pour
    rentable, et inversement.

    Une ligne sans `kelly_pct` (avant migration) est écartée du ROI mais
    compte toujours pour le `hit_rate`.
    """
    decisive = [r for r in rows if r.get("outcome") in _DECISIVE_OUTCOMES]
    n = len(decisive)
    if n == 0:
        return {"n": 0, "hit_rate": None, "roi": None, "wilson_lower": None,
                "wilson_upper": None, "p_breakeven": None}

    wins = sum(1 for r in decisive if r["outcome"] == "WIN")
    hit_rate = wins / n
    # La borne HAUTE sert autant que la basse depuis le 2026-08-06 : elle est ce
    # qui prouve une perte (borne haute sous la rentabilité), là où la basse
    # prouve un gain. Voir _decide_threshold.
    wilson_lower, wilson_upper = wilson_ci(wins, n)

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

    roi = roi_net_of_tax(decisive, _TAX_RATE)

    return {
        "n": n,
        "hit_rate": hit_rate,
        "roi": roi,
        "wilson_lower": wilson_lower,
        "wilson_upper": wilson_upper,
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


_CEILING_MIN = 4.0   # % — jamais de plafond en dessous : sous 4% la bande
                     # haute n'est plus « suspecte », c'est le cœur du signal.

# Découpage plus fin que _EDGE_BUCKETS pour cette décision précise. Les quatre
# tranches larges de _EDGE_BUCKETS noient l'information : mesuré le 2026-08-02,
# soccer perdait au-dessus de 6% mais son bucket (4, 8) mélangeait la zone
# gagnante 4-6% avec la zone perdante 6-8%, et le verdict ne voyait rien.
_CALIB_BUCKETS = [(0.0, 1.5), (1.5, 2.5), (2.5, 4.0), (4.0, 6.0), (6.0, 100.0)]


def _top_band_verdict(rows: list[dict]) -> tuple[bool, float | None, int, float | None]:
    """La bande d'edge la plus haute perd-elle plus que les bandes basses ?

    Renvoie (sous_performe, plafond, n, plancher_de_la_meilleure_bande).

    C'est la mesure qui manquait à _decide_threshold. Celui-ci n'a qu'un
    levier — le PLANCHER d'edge — et le monte quand un sport perd. Or si ce
    sont justement les gros edges qui perdent, monter le plancher pousse
    toute l'émission dans la bande perdante et aggrave le résultat, qui fait
    remonter le plancher au tour suivant : une boucle qui finit au plafond.
    Mesuré le 2026-08-02 sur le vrai ledger : soccer à 6%+ affichait 36,7% de
    réussite sur 49 résultats pour 47,8% requis, pendant que 1,5-4% gagnait —
    et le seuil du sport avait justement été poussé à 6,00%.

    Un gros edge qui perd plus qu'un petit n'est pas une inefficience de
    marché : c'est presque toujours un prix mal apparié ou périmé qui gonfle
    l'edge apparent (voir _edge_band_diagnostic).
    """
    decisive = [r for r in rows
                if r.get("outcome") in _DECISIVE_OUTCOMES and r.get("initial_edge") is not None]
    if len(decisive) < _SEGMENT_MIN_SAMPLES:
        return False, None, 0, None

    # Les bandes se comparent sur la MARGE au-dessus du seuil de rentabilité
    # (réussite − 1/cote), jamais sur le taux brut : chaque bande a sa propre
    # cote moyenne, donc son propre seuil. Mesuré le 2026-08-02 sur soccer, la
    # bande 0-1,5% tournait à 65,0% pour 61,6% requis (marge +3,4) et la bande
    # 2,5-4% à 80,0% pour 55,9% requis (marge +24,1) — un tri sur le brut
    # sous-estime la seconde et surestime la première.
    bucket_wr = []
    for lo, hi in _CALIB_BUCKETS:
        in_b = [r for r in decisive if lo <= r["initial_edge"] < hi]
        if len(in_b) < 5:
            continue
        wins = sum(1 for r in in_b if r["outcome"] == "WIN")
        wr = wins / len(in_b)
        odds_vals = [r["odds"] for r in in_b if r.get("odds")]
        avg_odds = sum(odds_vals) / len(odds_vals) if odds_vals else None
        be = p_breakeven(avg_odds, _TAX_RATE) if avg_odds else None
        margin = wr - be if be is not None else wr
        bucket_wr.append((lo, hi, len(in_b), margin))
    if len(bucket_wr) < 2:
        return False, None, 0, None

    best_lo, best_wr = max(((lo, m) for lo, _, _, m in bucket_wr),
                           key=lambda t: t[1])

    # On remonte depuis le haut tant que les bandes sous-performent la
    # meilleure : le plafond se pose au BAS de cette série, pas seulement sous
    # la dernière bande. Sinon une bande perdante intermédiaire reste ouverte —
    # soccer perdait dès 6% alors que seule la bande 8%+ aurait été coupée.
    ceiling_lo = None
    covered_n = 0
    for lo, _hi, n, wr in reversed(bucket_wr):
        if wr < best_wr and lo >= _CEILING_MIN:
            ceiling_lo = lo
            covered_n += n
        else:
            break

    if ceiling_lo is None:
        return False, None, 0, best_lo
    return True, ceiling_lo, covered_n, best_lo


_ODDS_BUCKETS = [(1.0, 1.50), (1.50, 1.80), (1.80, 2.20), (2.20, 3.00), (3.00, 99.0)]
_ODDS_CEILING_MIN = 1.50   # jamais de plafond sous 1,50 : couper les favoris
                           # courts reviendrait à ne plus rien émettre du tout.


def _odds_band_verdict(rows: list[dict]) -> tuple[float | None, int, str | None]:
    """Existe-t-il une cote au-dessus de laquelle le sport perd, PROUVÉ ?

    Renvoie (plafond_de_cote, n_couvert, diagnostic).

    Le test est ABSOLU et strict — borne HAUTE de Wilson sous le seuil de
    rentabilité de la bande — là où _top_band_verdict se contente d'une
    comparaison relative entre bandes. Cette asymétrie est voulue :

      Pour l'edge, l'affirmation « un edge trop gros est un prix mal apparié »
      préexiste dans le code (SUSPECT_EDGE, MAX_EDGE, le docstring de
      _edge_band_diagnostic). Les données ne font que confirmer une hypothèse
      déjà posée, et le test relatif haut-contre-bas la valide (p=0,005 sur
      soccer au 2026-08-02). Une comparaison relative suffit.

      Pour la cote, « on gagne sur les favoris courts » est une affirmation
      NOUVELLE, sans mécanisme préalable, et c'est exactement le genre de motif
      qui ressort d'une fouille de données puis disparaît hors échantillon. Au
      2026-08-02 aucune bande n'était concluante — même celle à n=109, dont la
      borne haute (52,5%) dépassait encore son seuil de 50,0%. Poser une règle
      dessus aurait coupé 58% du volume sur du bruit.

    Le plafond ne s'active donc que le jour où une bande est prouvée perdante.
    D'ici là la fonction renvoie None et rien ne change.
    """
    decisive = [r for r in rows
                if r.get("outcome") in _DECISIVE_OUTCOMES and r.get("odds")]
    if len(decisive) < _SEGMENT_MIN_SAMPLES:
        return None, 0, None

    proven_losing = []
    for lo, hi in _ODDS_BUCKETS:
        if lo < _ODDS_CEILING_MIN:
            continue
        in_b = [r for r in decisive if lo <= float(r["odds"]) < hi]
        if len(in_b) < _SEGMENT_MIN_SAMPLES:
            continue
        wins = sum(1 for r in in_b if r["outcome"] == "WIN")
        _, w_hi = wilson_ci(wins, len(in_b))
        avg_odds = sum(float(r["odds"]) for r in in_b) / len(in_b)
        be = p_breakeven(avg_odds, _TAX_RATE)
        if be is not None and w_hi < be:
            proven_losing.append((lo, len(in_b), wins / len(in_b), be))

    if not proven_losing:
        return None, 0, None

    # La bande prouvée perdante la PLUS BASSE fixe le plafond : tout ce qui est
    # au-dessus est au moins aussi douteux.
    lo, n, wr, be = min(proven_losing, key=lambda t: t[0])
    diag = (f"plafond de cote {lo:.2f} — la bande {lo:.2f}+ perd de façon prouvée "
            f"({wr*100:.0f}% pour {be*100:.0f}% requis, n={n})")
    return lo, sum(t[1] for t in proven_losing), diag


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
    # La surconfiance se mesure par l'ÉCART DE CALIBRATION — probabilité
    # moyenne annoncée moins taux de réussite réel — et non par un Brier
    # comparé à une constante.
    #
    # Le seuil dur `brier > 0.23` utilisé jusqu'au 2026-08-06 était
    # inatteignable : le Brier a un plancher irréductible de p(1-p) (0,2500 à
    # p=0,50 ; 0,2475 à 0,55 ; voir stats_utils.brier_reference), donc un
    # portefeuille de quasi pile-ou-face le dépasse toujours, même parfaitement
    # calibré. Mesuré ce jour-là : le drapeau se levait sur TOUS les sports, y
    # compris football (0,2319 pour une référence de 0,2365) et basket (0,2304
    # pour 0,2463) qui battaient pourtant leur propre référence. Comme il force
    # une hausse de plancher dans _decide_threshold, il formait un second
    # cliquet, indépendant de celui des 60%/82%, montant les seuils de 0,4 à
    # chaque audit.
    #
    # L'écart de calibration n'a pas ce défaut : il est directement lisible en
    # points de pourcentage et ne dépend pas de la difficulté des paris. Marge
    # à 10 points — au-delà du bruit d'échantillonnage sur quelques dizaines de
    # paris, et cohérent avec les mesures du 2026-08-06 : zone jouable -7,3
    # (sous-confiant, sain), football -7,4, basket -20,9, contre baseball +15,2
    # sur son historique complet, le seul réellement surconfiant.
    stated   = sum(p for p, _ in preds) / len(preds)
    realised = sum(o for _, o in preds) / len(preds)
    return (stated - realised) > _CALIBRATION_MAX_GAP


def _decide_threshold(old_t: float, stats: dict, clv: dict, overconfident: bool,
                      top_band_fake: bool = False,
                      best_band_lo: float | None = None) -> tuple[float | None, str]:
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

    Depuis le 2026-08-22, le CLV réel est aussi un critère de PREMIER rang
    (l'amplitude, plus seulement le taux) — il converge ~3x plus vite que le
    win-rate :
      - avg_clv < −_CLV_STRONG sur ≥ _CLV_MIN_SAMPLES lignes → ↑ sans
        attendre que le win-rate décroche (sauf top_band_fake) ;
      - avg_clv > +_CLV_STRONG, majorité de captures positives, hit_rate à
        la rentabilité et pas de surconfiance → ↓ sans attendre la borne de
        Wilson (jamais sous _THRESHOLD_MIN ; EV_EDGE_FLOOR reste le filet).

    Returns (new_threshold_or_None, reason). None means "no change".
    """
    hit_rate = stats["hit_rate"]

    # Monter le plancher n'a de sens que si les gros edges valent mieux que les
    # petits. Quand la bande haute sous-performe, c'est l'inverse : le relèvement
    # concentre l'émission là où le sport perd le plus, ce qui dégrade le
    # résultat et fait remonter le plancher au tour suivant — boucle jusqu'au
    # plafond dur, puis silence total.
    #
    # Le plancher est donc ramené AU BAS DE LA MEILLEURE BANDE MESURÉE, d'un
    # coup et non par pas de _STEP_DOWN : un plancher prouvé à l'intérieur de la
    # bande perdante n'a aucune raison d'y être maintenu 22 audits de plus (à
    # 0,2 par cycle, soccer aurait mis 5 jours à sortir de 6,0%). Le plafond
    # d'edge coupe l'autre extrémité dans le même mouvement.
    if top_band_fake and (hit_rate < _TARGET_LO or overconfident):
        if best_band_lo is not None:
            new_t = max(_THRESHOLD_MIN, min(_THRESHOLD_MAX, round(best_band_lo, 2)))
            if new_t != old_t:
                return new_t, (f"win rate {hit_rate*100:.0f}% faible mais la bande HAUTE "
                               f"sous-performe — plancher ramené sur la meilleure bande "
                               f"mesurée ({new_t:.1f}%), plafond d'edge appliqué")
        return None, (f"win rate {hit_rate*100:.0f}% faible mais la bande d'edge HAUTE "
                      f"sous-performe — relever le plancher pousserait l'émission "
                      f"dans la bande perdante ; plancher tenu, plafond d'edge appliqué")

    if overconfident and not (hit_rate < _TARGET_LO):
        new_t = min(_THRESHOLD_MAX, round(old_t + _STEP_UP, 2))
        return new_t, f"win rate {hit_rate*100:.0f}% ok but overconfident on 80%+ picks → ↑"

    # ── Montée sur CLV réel nettement négatif — sans attendre le win-rate ──
    # Un CLV moyen sous −1% sur ≥15 lignes dit que le marché n'a JAMAIS
    # confirmé ces prix : le win-rate peut encore avoir l'air correct par
    # variance, le verdict CLV arrive ~3x plus vite. Le garde top_band_fake
    # garde la priorité (monter le plancher pousserait l'émission dans la
    # bande mesurée perdante — même logique que le bloc au-dessus).
    if (not top_band_fake and clv.get("avg_clv") is not None
            and clv["n"] >= _CLV_MIN_SAMPLES and clv["avg_clv"] < -_CLV_STRONG):
        new_t = min(_THRESHOLD_MAX, round(old_t + _STEP_UP, 2))
        if new_t != old_t:
            return new_t, (f"CLV réel moyen {clv['avg_clv']:+.2f}% sur {clv['n']} "
                           f"lignes — le marché n'a jamais confirmé ces prix → ↑ "
                           f"(sans attendre le win-rate)")

    # ── Le critère est la RENTABILITÉ mesurée, plus un taux absolu ───────
    #
    # Jusqu'au 2026-08-06 : monter si hit_rate < 60%, descendre si > 82%.
    # Ces deux bornes fixes formaient un cliquet à sens unique. Un pari à cote
    # 1,85 est rentable dès 54,1% de réussite ; exiger 60% condamnait donc des
    # sports profitables à voir leur plancher monter de 0,4 par audit, tandis
    # que la descente attendait 82% — un taux qu'aucun paris sportif tenable ne
    # produit (le meilleur segment mesuré du ledger plafonne à 65%). Résultat
    # constaté en base le 2026-08-06 : threshold_basketball = 6,0% = le plafond
    # dur, pour un sport qui gagne 61,5% dans sa zone jouable là où 54,7%
    # suffisent — et 0 signal basketball émis depuis le 29 juillet. Idem pour
    # threshold_seg_soccer_totals et les deux segments baseball, tous à 6,0.
    #
    # La règle compare désormais le résultat au seuil de rentabilité réel du
    # sport (p_breakeven, qui tient déjà compte de la cote moyenne ET de
    # TAX_RATE), et reste délibérément ASYMÉTRIQUE :
    #   - taux observé SOUS la rentabilité       → ↑   (pas de preuve exigée)
    #   - borne basse de Wilson AU-DESSUS        → ↓   (preuve exigée)
    #   - sinon                                  → hold
    # L'asymétrie est le point : monter le plancher est réversible et ne fait
    # que filtrer davantage, tandis que le baisser expose du capital sur un
    # edge non prouvé. Exiger une preuve des deux côtés laisserait tourner un
    # sport perdant pendant des dizaines d'audits, le temps que l'intervalle se
    # resserre. Ce qui change vraiment par rapport aux 60%/82% fixes, c'est la
    # RÉFÉRENCE : elle suit maintenant la cote du sport au lieu d'un taux
    # absolu qu'un livre à cote basse ne peut pas atteindre.
    be = stats["p_breakeven"]
    lo = stats["wilson_lower"]

    if be is None or lo is None:
        # Pas de cote exploitable (ledger d'avant la migration v9.4) : on
        # retombe sur l'ancien critère absolu plutôt que de ne rien faire.
        if hit_rate < _TARGET_LO:
            return (min(_THRESHOLD_MAX, round(old_t + _STEP_UP, 2)),
                    f"win rate {hit_rate*100:.0f}% < {_TARGET_LO*100:.0f}% → ↑ "
                    f"(pas de cote au ledger, critère absolu de repli)")
        return None, f"win rate {hit_rate*100:.0f}% — pas de cote au ledger, hold"

    if hit_rate < be:
        new_t = min(_THRESHOLD_MAX, round(old_t + _STEP_UP, 2))
        tag = ""
        if clv["positive_rate"] is not None and clv["positive_rate"] > 0.5:
            tag = " (CLV réel toujours positif — probable variance, à surveiller)"
        return new_t, (f"win rate {hit_rate*100:.0f}% < rentabilité {be*100:.0f}% "
                       f"(n={stats['n']}) → ↑{tag}")

    if lo > be:
        if (clv["positive_rate"] is not None and clv["n"] >= _SEGMENT_MIN_SAMPLES
                and clv["positive_rate"] < 0.5):
            return None, (f"gain établi ({lo*100:.0f}% > {be*100:.0f}%) mais CLV réel "
                          f"négatif ({clv['positive_rate']*100:.0f}% positif sur "
                          f"{clv['n']} lignes) — le marché ne confirme pas, hold")
        new_t = max(_THRESHOLD_MIN, round(old_t - _STEP_DOWN, 2))
        return new_t, (f"gain établi : borne basse {lo*100:.0f}% > rentabilité "
                       f"{be*100:.0f}% (n={stats['n']}) → ↓")

    # ── Descente accélérée sur CLV réel nettement positif ────────────────
    # La borne de Wilson exige des dizaines de paris réglés ; un CLV moyen
    # au-dessus de +1% sur ≥15 lignes, avec une majorité de captures
    # positives, est la preuve que le marché confirme — plus rapide et plus
    # robuste au tirage. Jamais sous _THRESHOLD_MIN, et run_engine clampe de
    # toute façon à EV_EDGE_FLOOR (1,5%) au chargement : le filet tient même
    # si cette règle se trompe. Bloquée par la surconfiance (cohérence avec
    # le bloc du haut) et par un hit_rate sous la rentabilité (la montée a
    # déjà primé, la garde rend l'invariant explicite si l'ordre bougeait).
    if (hit_rate >= be and not overconfident
            and clv.get("avg_clv") is not None and clv["n"] >= _CLV_MIN_SAMPLES
            and clv["avg_clv"] > _CLV_STRONG
            and clv.get("positive_rate") is not None and clv["positive_rate"] >= 0.5):
        new_t = max(_THRESHOLD_MIN, round(old_t - _STEP_DOWN, 2))
        if new_t != old_t:
            return new_t, (f"CLV réel moyen {clv['avg_clv']:+.2f}% ({clv['n']} lignes, "
                           f"{clv['positive_rate']*100:.0f}% positives) — le marché "
                           f"confirme → ↓ sans attendre la borne de Wilson")

    return None, (f"win rate {hit_rate*100:.0f}% au-dessus de la rentabilité "
                  f"{be*100:.0f}% mais borne basse {lo*100:.0f}% en dessous "
                  f"(n={stats['n']}) — ne tranche pas, hold")


_LEDGER_SELECT = ("outcome, kelly_pct, odds, market_type, initial_edge, sharp_prob, "
                  "clv_pct_real, time_to_match_minutes, created_at")


# ── ÉPOQUE DE CALIBRATION (2026-08-27) ────────────────────────────────
# Tout ce que cette couche LOGGE peut se mesurer sur n'importe quelle ligne.
# Tout ce qu'elle fait APPLIQUER par le moteur — les plafonds d'edge et de
# cote, lus par run_engine._EDGE_CEILINGS/_ODDS_CEILINGS — ne le peut pas.
#
# Le 2026-08-27, A6 a corrigé le PRIX (prix exécutable) puis le PARI comparé
# (_meme_ligne : égalité exacte de ligne, signe compris). La distribution des
# edges d'AVANT décrit un moteur qui n'existe plus : « soccer au-dessus de
# 6 % perd » a été mesuré le 2026-08-02, dans une unité que la refonte EV du
# 2026-08-22 a changée. A6 a tranché « _EDGE_CEILINGS : rien à poser ».
#
# Cette couche l'a pourtant reposé le soir même (`edge_ceiling_soccer=6.0`,
# écrit à 19:28), parce que rien dans le code ne l'en empêchait : elle
# apprenait sur les 120 dernières lignes, toutes antérieures à la correction.
# Un plafond appris sur l'ancien moteur et appliqué au nouveau est exactement
# ce que la règle 10 de CLAUDE.md interdit — « aucun seuil numérique
# d'émission n'est modifié sans mesure sur des lignes réglées POSTÉRIEURES à
# la correction en cours ».
#
# D'où cette borne. Elle ne masque aucune ligne : le ledger reste entier, les
# diagnostics et les verdicts continuent de tout lire. Elle dit seulement de
# quoi on a le droit de faire une CONSIGNE. Elle se lève d'elle-même dès que
# le moteur corrigé a produit assez de résultats.
CALIBRATION_EPOCH = os.environ.get("CALIBRATION_EPOCH", "2026-08-27")


def post_correction_rows(rows: list[dict]) -> list[dict]:
    """Lignes réglées POSTÉRIEURES à la correction A6 (voir CALIBRATION_EPOCH).

    Une ligne sans `created_at` est ÉCARTÉE — contrairement à playable_rows,
    qui conserve l'inconnu. Le contrat est inverse ici : playable_rows filtre
    ce qu'on OBSERVE (jeter l'inconnu viderait l'historique), celle-ci filtre
    ce qu'on IMPOSE au moteur (garder l'inconnu ferait passer une ligne de
    l'ancien moteur pour une preuve).
    """
    return [r for r in rows if (r.get("created_at") or "")[:10] >= CALIBRATION_EPOCH]


def playable_rows(rows: list[dict]) -> list[dict]:
    """Ne garder que les paris que le système recommande encore.

    Voir _PLAYABLE_MIN_MINUTES/_PLAYABLE_MAX_MINUTES pour la mesure qui
    justifie ces bornes. Une ligne sans `time_to_match_minutes` est CONSERVÉE :
    on ne peut pas prouver qu'elle est hors zone, et la jeter viderait
    l'apprentissage de tout l'historique antérieur à la colonne. Même principe
    que le dashboard avec un signal sans match_time.
    """
    kept = []
    for r in rows:
        t = r.get("time_to_match_minutes")
        if t is None:
            kept.append(r)
            continue
        try:
            t = float(t)
        except (TypeError, ValueError):
            kept.append(r)
            continue
        if _PLAYABLE_MIN_MINUTES <= t <= _PLAYABLE_MAX_MINUTES:
            kept.append(r)
    return kept


def _drop_stale_ceiling(sb, key: str, sport: str, ancienne, summary_lines: list[str]) -> None:
    """Retire un plafond que les lignes postérieures à l'époque ne prouvent plus.

    Ne lève jamais : un plafond qu'on n'arrive pas à effacer reste en place et
    sera re-tenté au prochain audit — on ne fait pas tomber l'apprentissage
    pour ça. Silencieux quand la clé n'existe pas (le cas nominal).
    """
    if ancienne is None:          # rien de posé : le cas nominal, aucun appel
        return
    try:
        sb.table("meta").delete().eq("key", key).execute()
        log.warning("[%s] plafond %s=%s RETIRÉ — mesuré sur des lignes antérieures "
                    "au %s, donc sur un moteur qui n'existe plus (règle 10)",
                    sport, key, ancienne, CALIBRATION_EPOCH)
        summary_lines.append(f"{sport}: plafond {key} retiré (mesure pré-{CALIBRATION_EPOCH})")
    except Exception as e:
        log.warning("[%s] retrait de %s impossible (%s) — il reste appliqué",
                    sport, key, e)


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
    # Plafonds réellement posés aujourd'hui : sert à ne retirer que ce qui
    # existe, sans un SELECT par sport et par clé à chaque audit.
    edge_ceilings_posed = load_edge_ceilings(sb)
    odds_ceilings_posed = load_odds_ceilings(sb)
    segment_current = load_segment_thresholds(sb)
    now     = datetime.now(timezone.utc).isoformat()
    summary_lines: list[str] = []
    ranking_stats: dict[str, dict] = {}   # sport -> stats, pour _save_sport_ranking
    clv_by_sport: dict[str, dict] = {}    # sport -> _clv_stats, pour les verdicts
    all_preds: list[tuple[float, int]] = []   # calibration, tous sports confondus

    for sport in SPORT_DEFAULTS:
        try:
            res = (sb.table("ai_learning_ledger")
                   .select(_LEDGER_SELECT)
                   .eq("sport", sport)
                   .order("created_at", desc=True)
                   .limit(120)
                   .execute())
            raw_rows = res.data or []
            # 120 et non 50 : le filtre de zone jouable retire ~55% des lignes,
            # une fenêtre de 50 n'en laissait plus assez pour atteindre
            # _MIN_SAMPLES sur autre chose que le football.
            rows = playable_rows(raw_rows)
            if len(raw_rows) != len(rows):
                log.info("[%s] apprentissage sur %d/%d lignes (zone jouable %d-%dmin)",
                         sport, len(rows), len(raw_rows),
                         _PLAYABLE_MIN_MINUTES, _PLAYABLE_MAX_MINUTES)

            stats = _sport_stats(rows)
            ranking_stats[sport] = stats
            clv_by_sport[sport] = _clv_stats(rows)
            all_preds.extend(
                (r["sharp_prob"], 1 if r["outcome"] == "WIN" else 0)
                for r in rows
                if r.get("outcome") in _DECISIVE_OUTCOMES
                and r.get("sharp_prob") is not None)

            # Plafond d'edge : au-delà, l'edge mesuré n'est pas une inefficience
            # mais un prix mal apparié. Persisté avant la décision de seuil, qui
            # en dépend.
            # ── Ce qui est APPLIQUÉ se mesure APRÈS la correction ──────
            # Les deux plafonds ci-dessous sont les seules sorties de cette
            # couche que le moteur fait respecter (run_engine._EDGE_CEILINGS
            # et _ODDS_CEILINGS) ; le reste est loggé. Ils ne se calculent
            # donc que sur des lignes postérieures à CALIBRATION_EPOCH.
            appliquables = post_correction_rows(rows)
            if len(appliquables) != len(rows):
                log.info("[%s] plafonds : %d/%d lignes postérieures au %s "
                         "(le reste décrit un moteur qui n'existe plus)",
                         sport, len(appliquables), len(rows), CALIBRATION_EPOCH)

            top_fake, ceiling, band_n, best_band_lo = _top_band_verdict(appliquables)
            if top_fake and ceiling is not None:
                log.warning("[%s] Plafond d'edge %.1f%% — la bande haute perd plus "
                            "que les basses sur %d résultats", sport, ceiling, band_n)
                sb.table("meta").upsert({
                    "key":        f"edge_ceiling_{sport}",
                    "value":      str(ceiling),
                    "updated_at": now,
                }).execute()
                summary_lines.append(
                    f"{sport}: plafond edge {ceiling:.1f}% (bande haute perdante, n={band_n})")
            else:
                # Gater les écritures FUTURES ne suffit pas : un plafond posé
                # avant l'époque resterait appliqué indéfiniment, cette couche
                # ne faisant que des upserts. Il est retiré, pas laissé.
                _drop_stale_ceiling(sb, f"edge_ceiling_{sport}", sport,
                                    edge_ceilings_posed.get(sport), summary_lines)

            odds_cap, odds_n, odds_diag = _odds_band_verdict(appliquables)
            if odds_cap is not None:
                log.warning("[%s] %s", sport, odds_diag)
                sb.table("meta").upsert({
                    "key":        f"odds_ceiling_{sport}",
                    "value":      str(odds_cap),
                    "updated_at": now,
                }).execute()
                summary_lines.append(f"{sport}: {odds_diag}")
            else:
                _drop_stale_ceiling(sb, f"odds_ceiling_{sport}", sport,
                                    odds_ceilings_posed.get(sport), summary_lines)

            if stats["n"] < _MIN_SAMPLES:
                log.info("[%s] %d decisive samples < %d — threshold unchanged (%.1f%%)",
                         sport, stats["n"], _MIN_SAMPLES, current[sport])
            else:
                old_t = current[sport]
                clv = _clv_stats(rows)
                overconfident = _calibration_flag(rows)
                new_t, reason = _decide_threshold(old_t, stats, clv, overconfident,
                                                  top_band_fake=top_fake,
                                                  best_band_lo=best_band_lo)
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

    # Verdicts promotion/rétrogradation (Phase 4) — loggés, jamais appliqués.
    try:
        summary_lines.extend(_save_sport_verdicts(sb, ranking_stats, clv_by_sport, now))
    except Exception as e:
        log.warning("sport verdicts: %s", e)

    try:
        sb.table("meta").upsert({
            "key":        _SUMMARY_KEY,
            "value":      json.dumps(summary_lines[:20]),
            "updated_at": now,
        }).execute()
    except Exception as e:
        log.warning("compute_and_save: failed to persist learning_summary: %s", e)

    _save_sport_ranking(sb, ranking_stats, now)
    _save_calibration_snapshot(sb, all_preds, now)
    return updated


def _save_calibration_snapshot(sb, preds: list[tuple[float, int]], now: str) -> None:
    """Une ligne d'historique de calibration par run, dans `brier_scores`.

    La table existait depuis sa création sans qu'aucun code Python n'y écrive :
    le Brier était recalculé à la volée pour /performance puis jeté, donc
    aucune série temporelle ne permettait de voir une dérive arriver. C'est
    précisément ce qui a laissé le drapeau de surconfiance rester levé pendant
    des semaines sans que personne ne puisse le constater.

    On persiste le score AVEC sa référence : un Brier seul n'est pas
    interprétable, son plancher vaut p(1-p) et dépend de la difficulté des
    paris. L'écart de calibration (annoncé − réalisé) est la mesure lisible.
    Périmètre : la zone jouable uniquement, cohérent avec le reste du module.
    """
    if len(preds) < _SEGMENT_MIN_SAMPLES:
        log.info("Calibration : %d paris < %d — pas d'instantané", len(preds),
                 _SEGMENT_MIN_SAMPLES)
        return
    score = brier_score(preds)
    ref   = brier_reference(preds)
    gap   = (sum(p for p, _ in preds) - sum(o for _, o in preds)) / len(preds)
    try:
        sb.table("brier_scores").insert({
            "brier_score":     score,
            "brier_reference": ref,
            "calibration_gap": round(gap, 4),
            "sample_size":     len(preds),
            "scope":           "playable",
            "computed_at":     now,
        }).execute()
        log.info("Calibration : Brier %.4f pour une référence de %.4f, écart "
                 "%+.1f pts sur %d paris", score, ref, 100 * gap, len(preds))
    except Exception as e:
        # Colonnes absentes = migration v10_2 pas encore appliquée : on
        # réessaie avec le schéma d'origine plutôt que de perdre la mesure.
        log.debug("brier_scores (schéma complet): %s", str(e)[:80])
        try:
            sb.table("brier_scores").insert({
                "brier_score": score,
                "sample_size": len(preds),
                "computed_at": now,
            }).execute()
        except Exception as e2:
            log.warning("_save_calibration_snapshot: %s", str(e2)[:80])


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
