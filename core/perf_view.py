"""
core/perf_view.py — filtres d'affichage de la page /performance (Mission 2, Phase 1).

Pure (aucun accès réseau), pour être testable : api/index.py ne fait que
charger les lignes du ledger et les passer ici.

- `RETIRED_SPORTS` (core/constants.py) disparaît de TOUTES les vues : tableau
  par sport, historique, agrégats globaux (win rate, Brier, calibration), mois.
- `PERF_MONTHS_SHOWN` (env, défaut 2) : la section « Par mois » et les
  agrégats du haut de page ne montrent que les N derniers mois CALENDAIRES
  (mois courant inclus). Rien n'est détruit : c'est un filtre d'affichage,
  les lignes restent dans `ai_learning_ledger` (et l'archivage éventuel des
  sports retirés est un script SQL manuel, sql/archive_retired_sports.sql).
"""
import os
from datetime import datetime, timezone

from core.constants import RETIRED_SPORTS

PERF_MONTHS_SHOWN = int(os.environ.get("PERF_MONTHS_SHOWN", "2"))


def shown_months(now: datetime | None = None, n: int | None = None) -> list[str]:
    """Les n derniers mois calendaires au format 'YYYY-MM', du plus récent au
    plus ancien (mois courant inclus)."""
    now = now or datetime.now(timezone.utc)
    n = PERF_MONTHS_SHOWN if n is None else n
    out: list[str] = []
    y, m = now.year, now.month
    for _ in range(max(n, 0)):
        out.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return out


def filter_rows(rows: list[dict], now: datetime | None = None,
                months_shown: int | None = None) -> list[dict]:
    """Lignes visibles : sports non retirés ET créées dans les mois affichés."""
    months = set(shown_months(now, months_shown))
    return [r for r in rows
            if (r.get("sport") or "") not in RETIRED_SPORTS
            and (r.get("created_at") or "")[:7] in months]
