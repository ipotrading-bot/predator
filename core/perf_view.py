"""
core/perf_view.py — filtres d'affichage de la page /performance.

Pure (aucun accès réseau), pour être testable : api/index.py ne fait que
charger les lignes du ledger et les passer ici.

TROIS RÈGLES, et elles ne servent pas la même chose :

1. `PERF_START_MONTH` (défaut « 2026-08 ») — **l'époque zéro du système**.
   Décision opérateur du 2026-08-22 : « predator n'était pas au point et
   avait des bugs en juillet, on recommence tout en août ». Une ligne
   antérieure à ce mois ne mesure donc pas le système actuel — la garder
   dans les agrégats reviendrait à juger la version d'aujourd'hui sur les
   erreurs d'une version corrigée depuis.
   Les lignes de juillet ont été ARCHIVÉES en base le même jour
   (`sql/migrate_v10_5_archive_pre_august.sql`, 206 lignes vers
   `ai_learning_ledger_archive`). Cette borne est la CEINTURE qui va avec
   les bretelles : même si des lignes antérieures étaient réinsérées — un
   backfill, une restauration d'archive — elles ne remonteraient pas sur la
   page par accident. Le SQL nettoie la table, le code tient la règle.

2. `RETIRED_SPORTS` (core/constants.py) — disparaît de TOUTES les vues :
   tableau par sport, historique, agrégats globaux, mois.

3. `PERF_MONTHS_SHOWN` (défaut 2) — fenêtre glissante des N derniers mois
   calendaires. C'est un confort de lecture, pas une règle de validité ;
   elle se combine avec la borne (1) par intersection.

Rien n'est détruit ici : ce module ne fait que filtrer un affichage.
"""
import os
from datetime import datetime, timezone

from core.constants import RETIRED_SPORTS

PERF_MONTHS_SHOWN = int(os.environ.get("PERF_MONTHS_SHOWN", "2"))

# Époque zéro. Format « YYYY-MM » — comparable directement en chaîne, ce qui
# est exact tant que le format est à largeur fixe (« 2026-09 » > « 2026-08 »).
PERF_START_MONTH = os.environ.get("PERF_START_MONTH", "2026-08")


def shown_months(now: datetime | None = None, n: int | None = None) -> list[str]:
    """Les n derniers mois calendaires au format 'YYYY-MM', du plus récent au
    plus ancien (mois courant inclus), JAMAIS avant `PERF_START_MONTH`.

    Sans cette borne, la fenêtre glissante ferait réapparaître une carte de
    mois vide pour juillet 2026 — un mois sans aucune ligne, qui n'est pas
    « zéro pari » mais « période exclue ». Afficher 0/0 pour une période
    volontairement écartée est plus trompeur que ne rien afficher.
    """
    now = now or datetime.now(timezone.utc)
    n = PERF_MONTHS_SHOWN if n is None else n
    out: list[str] = []
    y, m = now.year, now.month
    for _ in range(max(n, 0)):
        mois = f"{y:04d}-{m:02d}"
        if mois < PERF_START_MONTH:
            break
        out.append(mois)
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return out


def filter_rows(rows: list[dict], now: datetime | None = None,
                months_shown: int | None = None) -> list[dict]:
    """Lignes visibles : sport non retiré, mois affiché, et pas avant l'époque.

    La condition sur `PERF_START_MONTH` est redondante avec `shown_months()`
    tant que la fenêtre est courte — elle est écrite explicitement quand
    même, parce qu'un `PERF_MONTHS_SHOWN` relevé (pour inspecter un
    historique) ne doit pas rouvrir la porte à juillet sans qu'on le décide.
    """
    months = set(shown_months(now, months_shown))
    return [r for r in rows
            if (r.get("sport") or "") not in RETIRED_SPORTS
            and (r.get("created_at") or "")[:7] in months
            and (r.get("created_at") or "")[:7] >= PERF_START_MONTH]
