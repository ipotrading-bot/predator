"""
scripts/calibration_report.py — où l'argent se gagne, par bande.

Lecture seule sur `ai_learning_ledger`. N'écrit rien, ne décide rien.

Pourquoi ce rapport : la couche d'apprentissage ne dispose que d'un seuil par
sport (plus un par famille de marché). Quand un sport perd, elle monte ce
seuil, et au plafond (_THRESHOLD_MAX = 6%) le sport devient muet. C'est
correct mais grossier : ça jette l'information la plus utile, à savoir que les
pertes d'un sport ne sont presque jamais réparties uniformément. Un sport qui
perd −13% en moyenne peut très bien perdre −40% sous 1,5% d'edge et gagner
au-dessus de 3%. Un mur unique à 6% supprime autant les paris rentables que
les autres.

Deux découpages, parce qu'ils répondent à deux questions différentes :

  BANDE D'EDGE — « à partir de quel edge le signal devient-il vrai ? »
  C'est ce qui doit fixer le seuil. L'hypothèse fondatrice du système est
  qu'un edge plus grand est plus souvent réel ; elle n'est vérifiable que là.

  BANDE DE COTE — « sur quels favoris gagne-t-on ? »
  Le seuil de rentabilité est 1/cote (avant taxe) : à 1,50 il faut 66,7% de
  réussite, à 2,50 il n'en faut que 40%. Un taux de réussite brut ne veut donc
  rien dire hors de sa bande de cote — 55% est excellent à 2,20 et ruineux à
  1,45. C'est la seule vue qui répond directement à « quels favoris, à quelles
  cotes ».

Lancé par .github/workflows/rank_sports.yml (secrets Supabase en lecture).
"""
import logging
import os

logging.basicConfig(level=logging.WARNING, format="%(levelname)s | %(message)s")

from core.constants import TAX_RATE                       # noqa: E402
from core.db import get_db                                # noqa: E402
from core.learning_layer import (SPORT_DEFAULTS, _DECISIVE_OUTCOMES,  # noqa: E402
                                 _LEDGER_SELECT)
from core.stats_utils import p_breakeven, wilson_ci       # noqa: E402

LIMIT = int(os.environ.get("RANK_LEDGER_LIMIT", "500"))
MIN_BAND = int(os.environ.get("CALIB_MIN_BAND", "5"))   # sous ce n, on n'affiche pas

EDGE_BANDS = [(0.0, 1.5), (1.5, 2.5), (2.5, 4.0), (4.0, 6.0), (6.0, 100.0)]
ODDS_BANDS = [(1.0, 1.50), (1.50, 1.80), (1.80, 2.20), (2.20, 3.00), (3.00, 99.0)]


def _band_stats(rows: list[dict]) -> dict | None:
    """n, réussite, seuil requis, ROI, borne Wilson — pour un sous-ensemble."""
    dec = [r for r in rows if r.get("outcome") in _DECISIVE_OUTCOMES]
    if not dec:
        return None
    n = len(dec)
    wins = sum(1 for r in dec if r["outcome"] == "WIN")
    hit = wins / n
    wl, wh = wilson_ci(wins, n)

    odds_vals = [r["odds"] for r in dec if r.get("odds")]
    avg_odds = sum(odds_vals) / len(odds_vals) if odds_vals else None
    # TAX_RATE explicite, jamais le défaut nu de p_breakeven : l'opérateur l'a
    # mis à 0, et le défaut (0.20) durcirait le seuil d'environ 25%.
    be = p_breakeven(avg_odds, TAX_RATE) if avg_odds else None

    staked = [r for r in dec if r.get("kelly_pct") and r.get("odds")]
    roi = None
    if staked:
        numer = sum(r["kelly_pct"] * (r["odds"] - 1) if r["outcome"] == "WIN"
                    else -r["kelly_pct"] for r in staked)
        denom = sum(r["kelly_pct"] for r in staked)
        roi = numer / denom if denom else None

    return {"n": n, "hit": hit, "be": be, "roi": roi,
            "avg_odds": avg_odds, "wl": wl, "wh": wh}


def _fmt(st: dict) -> str:
    hit = f"{st['hit'] * 100:5.1f}%"
    be = f"{st['be'] * 100:5.1f}%" if st["be"] is not None else "  —  "
    roi = f"{st['roi'] * 100:+6.1f}%" if st["roi"] is not None else "   —  "
    odd = f"{st['avg_odds']:.2f}" if st["avg_odds"] else " — "
    # Le verdict compare la réussite au seuil requis PAR LA COTE de la bande.
    # C'est le seul test qui a un sens : 55% est excellent à 2,20, ruineux à 1,45.
    # ✅/❌ = le point estimé. Le VERDICT est l'intervalle de Wilson : tant
    # qu'il chevauche le seuil, la bande n'a rien prouvé, ni dans un sens ni
    # dans l'autre — et une règle posée dessus serait du bruit. C'est la leçon
    # du 2026-08-02 : la bande de cote 1,80-2,20 avait n=109 et paraissait
    # clairement perdante, mais sa borne haute (52,5%) dépassait encore son
    # seuil (50,0%).
    if st["be"] is None:
        mark, verdict = " ", ""
    elif st["wl"] > st["be"]:
        mark, verdict = "✅", "GAGNE prouvé"
    elif st["wh"] < st["be"]:
        mark, verdict = "❌", "PERD prouvé"
    else:
        mark = "✅" if st["hit"] > st["be"] else "❌"
        verdict = "indéterminé"
    return (f"{st['n']:>4} {hit:>7} {be:>7} {roi:>8} {odd:>6}  {mark} "
            f"[{st['wl']*100:.0f}-{st['wh']*100:.0f}%] {verdict}")


def _table(title: str, bands, key, rows: list[dict], unit: str) -> None:
    print(f"\n  {title}")
    print(f"    {'bande':<14} {'n':>4} {'réuss.':>7} {'requis':>7} "
          f"{'ROI':>8} {'cote':>6}")
    shown = 0
    for lo, hi in bands:
        sub = [r for r in rows
               if r.get(key) is not None and lo <= float(r[key]) < hi]
        st = _band_stats(sub)
        if st is None or st["n"] < MIN_BAND:
            continue
        label = f"{lo:g}-{hi:g}{unit}" if hi < 90 else f"{lo:g}{unit}+"
        print(f"    {label:<14} {_fmt(st)}")
        shown += 1
    if not shown:
        print(f"    (aucune bande n'atteint {MIN_BAND} résultats décisifs)")


def main() -> int:
    sb = get_db(write=False)
    all_rows: list[dict] = []

    print(f"\n{'=' * 78}")
    print(f"CALIBRATION — {LIMIT} dernières lignes de ledger par sport")
    print("réuss. = taux réel | requis = seuil de rentabilité (1/cote, "
          f"taxe={TAX_RATE:.0%})")
    print("[x-y%] = intervalle de Wilson. Tant qu'il CHEVAUCHE le seuil requis,")
    print("la bande n'a rien prouvé — une règle posée dessus serait du bruit.")
    print(f"{'=' * 78}")

    for sport in SPORT_DEFAULTS:
        try:
            res = (sb.table("ai_learning_ledger").select(_LEDGER_SELECT)
                   .eq("sport", sport).order("created_at", desc=True)
                   .limit(LIMIT).execute())
            rows = res.data or []
        except Exception as e:
            print(f"\n{sport}: lecture impossible — {e}")
            continue

        dec = [r for r in rows if r.get("outcome") in _DECISIVE_OUTCOMES]
        if not dec:
            continue
        all_rows.extend(dec)

        glob = _band_stats(dec)
        be_s = f"{glob['be']*100:.1f}%" if glob["be"] is not None else "—"
        roi_s = f"{glob['roi']*100:+.1f}%" if glob["roi"] is not None else "—"
        print(f"\n{'-' * 78}")
        print(f"{sport.upper()}  —  n={glob['n']}  réussite {glob['hit']*100:.1f}%  "
              f"requis {be_s}  ROI {roi_s}")
        _table("Par bande d'EDGE (fixe le seuil)", EDGE_BANDS, "initial_edge", dec, "%")
        _table("Par bande de COTE (quels favoris)", ODDS_BANDS, "odds", dec, "")

    if all_rows:
        print(f"\n{'=' * 78}")
        print(f"TOUS SPORTS CONFONDUS — n={len(all_rows)}")
        print(f"{'=' * 78}")
        _table("Par bande d'EDGE", EDGE_BANDS, "initial_edge", all_rows, "%")
        _table("Par bande de COTE", ODDS_BANDS, "odds", all_rows, "")

    print(f"\n{'=' * 78}")
    print("Lecture : n'agir que sur les bandes marquées PERD prouvé ou GAGNE")
    print("prouvé. Une bande 'indéterminé' peut avoir l'air franche et rester")
    print("du hasard — au 2026-08-02, 1,80-2,20 avait n=109 et n'avait rien")
    print("prouvé. Un seuil se pose au BAS de la meilleure bande, jamais au")
    print("plafond : le plafond supprime les bandes rentables avec les autres.")
    print(f"{'=' * 78}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
