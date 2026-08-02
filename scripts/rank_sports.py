"""
scripts/rank_sports.py — classement des sports par réussite réelle.

Lecture seule sur `ai_learning_ledger`. N'écrit rien, ne décide rien : il
imprime le classement pour qu'une priorisation soit décidée sur des chiffres
plutôt que sur une intuition.

Le classement est trié par BORNE BASSE DE WILSON, pas par taux de réussite
brut. Sur ces volumes, le brut est du bruit : un sport à 2 gagnés sur 2 affiche
100% et ne prouve rien, là où 30 sur 50 à 60% est un résultat. La borne de
Wilson répond à « quel est le pire taux compatible avec ce que j'ai observé ? »,
donc elle pénalise automatiquement les petits échantillons — c'est exactement
le critère qu'on veut pour arbitrer une ressource rare. core/learning_layer.py
la calcule déjà (`_sport_stats`), on la réutilise telle quelle.

Lancé par .github/workflows/rank_sports.yml (secrets Supabase en lecture).
"""
import logging
import os
import sys

logging.basicConfig(level=logging.WARNING,
                    format="%(levelname)s | %(message)s")

from core.db import get_db                                    # noqa: E402
from core.learning_layer import (SPORT_DEFAULTS, _LEDGER_SELECT,  # noqa: E402
                                 _MIN_SAMPLES, _sport_stats)

LIMIT = int(os.environ.get("RANK_LEDGER_LIMIT", "200"))


def _pct(v) -> str:
    return "  —  " if v is None else f"{v * 100:5.1f}%"


def main() -> int:
    sb = get_db(write=False)

    rows_by_sport: dict[str, list[dict]] = {}
    for sport in SPORT_DEFAULTS:
        try:
            res = (sb.table("ai_learning_ledger")
                   .select(_LEDGER_SELECT)
                   .eq("sport", sport)
                   .order("created_at", desc=True)
                   .limit(LIMIT)
                   .execute())
            rows_by_sport[sport] = res.data or []
        except Exception as e:
            print(f"  ! {sport}: lecture impossible — {e}", flush=True)
            rows_by_sport[sport] = []

    stats = {s: _sport_stats(r) for s, r in rows_by_sport.items()}

    # Un sport sans résultat décisif n'est pas « mauvais », il est INCONNU :
    # le trier avec les autres reviendrait à le condamner sur une absence de
    # données. Il part dans un second bloc.
    known = {s: st for s, st in stats.items() if st["n"] > 0}
    unknown = [s for s, st in stats.items() if st["n"] == 0]

    ranked = sorted(known.items(),
                    key=lambda kv: (kv[1]["wilson_lower"] or 0.0), reverse=True)

    print(f"\n{'=' * 78}")
    print(f"CLASSEMENT DES SPORTS — {LIMIT} dernières lignes de ledger par sport")
    print(f"{'=' * 78}")
    print(f"{'#':<3} {'sport':<14} {'n':>4} {'réussite':>9} {'Wilson-':>9} "
          f"{'ROI':>8} {'seuil-eq':>9}  fiabilité")
    print(f"{'':<3} {'':<14} {'':>4} {'brute':>9} {'borne bas':>9} {'':>8} "
          f"{'requis':>9}")
    print("-" * 78)

    for i, (sport, st) in enumerate(ranked, 1):
        # p_breakeven = taux de réussite minimal pour ne pas perdre d'argent à
        # la cote moyenne observée. Battre ce seuil est le seul vrai test.
        edge_ok = ""
        if st["hit_rate"] is not None and st["p_breakeven"] is not None:
            edge_ok = "✅" if st["hit_rate"] > st["p_breakeven"] else "❌"
        fiab = ("échantillon suffisant" if st["n"] >= _MIN_SAMPLES
                else f"insuffisant (<{_MIN_SAMPLES})")
        print(f"{i:<3} {sport:<14} {st['n']:>4} {_pct(st['hit_rate']):>9} "
              f"{_pct(st['wilson_lower']):>9} {_pct(st['roi']):>8} "
              f"{_pct(st['p_breakeven']):>9} {edge_ok} {fiab}")

    if unknown:
        print("-" * 78)
        print("Aucun résultat décisif (WIN/LOSS) — statut inconnu, pas mauvais :")
        print("  " + ", ".join(sorted(unknown)))

    print(f"\n{'=' * 78}")
    solid = [s for s, st in ranked if st["n"] >= _MIN_SAMPLES]
    if solid:
        print("Ordre de priorité défendable (échantillon suffisant) :")
        print("  " + " > ".join(solid))
    else:
        print(f"AUCUN sport n'atteint {_MIN_SAMPLES} résultats décisifs.")
        print("Toute priorisation par réussite serait du bruit à ce stade —")
        print("c'est le settlement qu'il faut débloquer d'abord, pas l'ordre.")
    print(f"{'=' * 78}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
