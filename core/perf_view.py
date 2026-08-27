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

4. `resolution_rate()` — le TAUX DE RÉSOLUTION, réglés / (réglés + expired).
   Ce n'est pas un filtre mais une MESURE, et elle vit ici parce que c'est la
   page /performance qui la doit à son lecteur. Voir sa docstring : sans elle,
   la page souffre d'un biais de survie.

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


# Un signal a REÇU un résultat. Le ledger l'exprime par `outcome`, la table
# `signals` par `status` — même fait, deux vocabulaires imposés par le schéma.
_RESOLU = frozenset({"WIN", "LOSS", "PUSH", "settled"})


def resolution_rate(rows: list[dict], field: str = "outcome") -> dict:
    """
    réglés / (réglés + expired) — la part des signaux dont on a SU le résultat.

    POURQUOI CETTE MESURE MANQUAIT, ET CE QU'ELLE CORRIGE
    -----------------------------------------------------
    /performance ne compte que les lignes réglées. Les `expired` — les signaux
    purgés avant qu'un score ait pu être trouvé — sortent de tous les
    agrégats : ni dans le taux de réussite, ni dans le CLV, ni dans le ROI.
    La page mesure donc les paris qu'on a réussi à SUIVRE, et présente ce
    résultat comme celui de tous les paris.

    C'est un BIAIS DE SURVIE, et il n'est pas neutre : le règlement échoue
    plus souvent là où l'appariement de noms échoue, c'est-à-dire sur les
    ligues obscures et les sources douteuses — exactement les lignes dont
    l'edge est le plus suspect. Les écarter embellit la page, dans le sens
    précis qui flatte le moteur.

    Mesuré le 2026-08-27 : 44,8 % côté ledger, et 37,8 % pour le seul
    football. Près de deux signaux sur trois n'ont jamais reçu de résultat,
    et la page n'en disait rien.

    `field` vaut `outcome` sur le ledger et `status` sur `signals`.
    `active`/`closed` n'entrent NULLE PART : ni résultat, ni abandon — des
    états intermédiaires, et les compter au dénominateur ferait passer un run
    récent pour une panne de règlement.

    Rend `rate_pct=None` quand rien n'est mesurable — jamais 0.0, qui se
    lirait « aucun signal résolu ».
    """
    settled = sum(1 for r in rows if str(r.get(field)) in _RESOLU)
    expired = sum(1 for r in rows if str(r.get(field)) == "expired")
    denom = settled + expired
    return {"settled": settled, "expired": expired, "denom": denom,
            "rate_pct": round(settled / denom * 100, 1) if denom else None}
