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

5. `monthly_summary()`, `league_breakdown()`, `pick_month()` (2026-09-03) —
   le résumé par mois (rétabli), la ventilation par ligue « perdantes
   d'abord », et le mois choisi dans le menu déroulant de l'historique,
   validé contre `shown_months()` : le menu ne rouvre jamais juillet.

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


# ── Résumé par mois, ligues, sélection d'un mois (2026-09-03) ─────────────
#
# Demande opérateur du 2026-09-03 : « le résumé par mois comme avant »,
# la liste des matchs limitée au mois courant (août archivé, accessible par
# menu déroulant), et savoir « quelles ligues perdent plus souvent ».
# Tout ce qui suit est PUR : api/index.py charge les lignes et appelle.

_DECISIF = frozenset({"WIN", "LOSS"})

_MOIS_FR = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet",
            "août", "septembre", "octobre", "novembre", "décembre"]

# Valeur du menu déroulant qui rouvre la fenêtre entière (tous les mois
# affichés) — ce n'est PAS un mois, `pick_month` la reconnaît à part.
ALL_MONTHS = "tout"

# Ligue absente de la ligne : on la regroupe sous un libellé explicite plutôt
# que de la perdre — c'est justement là que l'appariement échoue le plus.
SANS_LIGUE = "(sans ligue)"


def month_label(mois: str) -> str:
    """'2026-09' → 'septembre 2026'. Une chaîne inattendue est rendue telle
    quelle : le libellé est un confort, jamais une raison de planter la page."""
    try:
        y, m = mois.split("-")
        return f"{_MOIS_FR[int(m) - 1]} {int(y)}"
    except (ValueError, IndexError, AttributeError):
        return str(mois)


def pick_month(requested: str | None, months: list[str]) -> str | None:
    """Le mois à afficher dans l'historique : celui demandé s'il est dans la
    fenêtre, `ALL_MONTHS` si demandé explicitement, sinon le plus récent.

    Une valeur hors fenêtre (faute de frappe, mois archivé, juillet) retombe
    sur le mois courant au lieu d'ouvrir une page vide ou de contourner
    l'époque : le menu déroulant est la SEULE porte, et il ne propose que
    `shown_months()`. Rend None quand la fenêtre est vide."""
    if not months:
        return None
    if requested == ALL_MONTHS or requested in months:
        return requested
    return months[0]


def rows_of_month(rows: list[dict], mois: str | None) -> list[dict]:
    """Sous-ensemble d'un mois ('YYYY-MM') ; `ALL_MONTHS`/None rend tout."""
    if mois in (None, ALL_MONTHS):
        return list(rows)
    return [r for r in rows if (r.get("created_at") or "")[:7] == mois]


def playable_zone(r: dict) -> str:
    """« zone » (T-2h … T-24h, ce que le système RECOMMANDE), « golden »
    (< T-2h : émis en fantôme, jamais envoyé), « hors » (> T-24h) ou « nc »
    (colonne vide — l'inconnu n'est pas classé). Bornes IMPORTÉES de
    core.learning_layer (règle n°6) : le dashboard et la couche
    d'apprentissage découpent le ledger au même endroit."""
    from core.learning_layer import _PLAYABLE_MAX_MINUTES, _PLAYABLE_MIN_MINUTES
    t = r.get("time_to_match_minutes")
    if t is None:
        return "nc"
    try:
        t = float(t)
    except (TypeError, ValueError):
        return "nc"
    if t < _PLAYABLE_MIN_MINUTES:
        return "golden"
    if t > _PLAYABLE_MAX_MINUTES:
        return "hors"
    return "zone"


def _bloc_decisif(decisifs: list[dict], tax_rate: float) -> dict:
    """Le noyau commun à un mois et à une ligue : réussite AVEC Wilson et point
    mort (règle dure n°7 — jamais un taux nu), cote moyenne, P&L à mise plate.

    Le P&L est recalculé depuis la cote (1 u par pari, gain net taxé) plutôt
    que lu dans `profit_units` : cette colonne est NULL sur une partie des
    lignes réglées, et un total qui saute des lignes ment sans le dire."""
    from core.stats_utils import p_breakeven, wilson_ci
    wins = sum(1 for r in decisifs if r.get("outcome") == "WIN")
    losses = len(decisifs) - wins
    n = len(decisifs)
    odds = [float(r["odds"]) for r in decisifs if r.get("odds")]
    avg_odds = sum(odds) / len(odds) if odds else None
    lo, hi = wilson_ci(wins, n) if n else (0.0, 0.0)
    be = p_breakeven(avg_odds, tax_rate) if avg_odds else None
    pnl = sum((float(r["odds"]) - 1) * (1 - tax_rate) if r.get("outcome") == "WIN" else -1.0
              for r in decisifs if r.get("odds"))
    # Gagnés/perdus par zone : une ligue qui perd sur des fantômes T-2h
    # (« golden ») ou des lignes à plus de 24 h n'appelle pas la même
    # correction qu'une ligue qui perd sur les paris RECOMMANDÉS.
    zones = {z: {"wins": 0, "losses": 0} for z in ("zone", "golden", "hors", "nc")}
    for r in decisifs:
        zones[playable_zone(r)]["wins" if r.get("outcome") == "WIN" else "losses"] += 1
    return {
        "zones": zones,
        "losses_out_of_zone": zones["golden"]["losses"] + zones["hors"]["losses"],
        "n": n, "wins": wins, "losses": losses,
        "win_rate": round(wins / n * 100, 1) if n else None,
        "win_rate_lo": round(lo * 100, 1), "win_rate_hi": round(hi * 100, 1),
        "avg_odds": round(avg_odds, 2) if avg_odds else None,
        "p_breakeven": round(be * 100, 1) if be is not None else None,
        # Trois verdicts, dans l'ordre : l'intervalle ENTIER au-dessus du point
        # mort (rentable), ENTIER en dessous (perdante), sinon rien n'est
        # démontré — la position du point moyen ne compte pas.
        "above_breakeven": be is not None and n > 0 and lo > be,
        "below_breakeven": be is not None and n > 0 and hi < be,
        "pnl_units": round(pnl, 2),
    }


def monthly_summary(rows: list[dict], tax_rate: float) -> list[dict]:
    """Une carte par mois présent dans `rows`, du plus récent au plus ancien.

    Reprend la section « PAR MOIS » retirée le 2026-08-22 (elle n'avait alors
    qu'un seul mois à montrer) et rétablie le 2026-09-03 à la demande de
    l'opérateur, maintenant qu'il y en a deux. Même règle qu'alors sur le CLV :
    la mesure réelle (`clv_pct_real`) prime, l'edge d'entrée n'est qu'un repli,
    et les deux ne sont JAMAIS moyennés ensemble."""
    par_mois: dict[str, list[dict]] = {}
    for r in rows:
        mo = (r.get("created_at") or "")[:7]
        if mo:
            par_mois.setdefault(mo, []).append(r)
    out = []
    for mo in sorted(par_mois, reverse=True):
        lignes = par_mois[mo]
        d = _bloc_decisif([r for r in lignes if r.get("outcome") in _DECISIF], tax_rate)
        clv_real = [r["clv_pct_real"] for r in lignes if r.get("clv_pct_real") is not None]
        clv_fallback = [r["clv_final"] for r in lignes if r.get("clv_final") is not None]
        clv = clv_real or clv_fallback
        edges = [r["initial_edge"] for r in lignes if r.get("initial_edge") is not None]
        d.update({
            "month": mo, "label": month_label(mo),
            "total": len(lignes),
            "pushes": sum(1 for r in lignes if r.get("outcome") == "PUSH"),
            "expired": sum(1 for r in lignes if r.get("outcome") == "expired"),
            "avg_clv": round(sum(clv) / len(clv), 2) if clv else None,
            "clv_is_real": bool(clv_real), "clv_n": len(clv),
            "avg_edge": round(sum(edges) / len(edges), 2) if edges else None,
        })
        out.append(d)
    return out


def league_breakdown(rows: list[dict], tax_rate: float, min_n: int = 5) -> list[dict]:
    """Réussite par (sport, ligue), la PLUS PERDANTE en premier (P&L croissant).

    Ne garde que les ligues à `min_n` paris décisifs (WIN|LOSS) ou plus — sous
    cinq lignes un tableau ne classe que du bruit — mais `expired` est compté
    sur toutes les lignes de la ligue : une ligue qui perd ET dont on ne trouve
    pas les scores est doublement suspecte (biais de survie, voir
    `resolution_rate`). Le verdict par ligne reste celui de `_bloc_decisif` :
    sous n ≥ 30, « perdante » veut dire « intervalle de Wilson entièrement sous
    le point mort », pas « moins de 50 % ».

    Les libellés de ligue viennent tels quels des sources : la même compétition
    peut apparaître sous deux noms (« WNBA » / « USA - WNBA », « Brazil Série A »
    / « BRA D1 ») selon le palier qui a émis le signal. On NE normalise PAS ici
    — ce serait une liste tenue à la main (règle dure n°6) ; le tableau montre
    la divergence au lieu de la cacher."""
    groupes: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        cle = (r.get("sport") or "", (r.get("league") or "").strip() or SANS_LIGUE)
        groupes.setdefault(cle, []).append(r)
    out = []
    for (sport, ligue), lignes in groupes.items():
        decisifs = [r for r in lignes if r.get("outcome") in _DECISIF]
        if len(decisifs) < max(min_n, 1):
            continue
        d = _bloc_decisif(decisifs, tax_rate)
        d.update({"sport": sport, "league": ligue,
                  "expired": sum(1 for r in lignes if r.get("outcome") == "expired")})
        out.append(d)
    out.sort(key=lambda d: (d["pnl_units"], -d["n"], d["league"]))
    return out


def is_phantom(r: dict) -> bool:
    """Ce pari a-t-il été recommandé, ou seulement mesuré ?

    Le drapeau `is_shadow` (sql/migrate_v10_12, recopié du signal au
    règlement) fait foi quand il est là : il dit ce que le moteur a DÉCIDÉ à
    l'émission. Sans lui (ligne d'avant la migration, fixture), on retombe sur
    la zone horaire — golden (< T-2h) ou > 24 h — qui est une approximation :
    elle se calcule depuis `time_to_match_minutes`, longtemps mesuré au
    DERNIER rafraîchissement et non à la première émission."""
    flag = r.get("is_shadow")
    if flag is not None:
        return bool(flag)
    return playable_zone(r) in ("golden", "hors")


def recommended_rows(rows: list[dict]) -> list[dict]:
    """Les lignes qu'un lecteur a pu JOUER : zone recommandée (T-2h … T-24h)
    et lignes sans `time_to_match_minutes` (l'inconnu est conservé, comme dans
    `learning_layer.playable_rows`). Les fantômes golden hour (< T-2h, jamais
    envoyés) et les lignes à plus de 24 h en sortent.

    C'est la base du bandeau de /performance depuis le 2026-09-03 : mesuré ce
    jour-là, septembre affichait 52 % de réussite dont 4–0 sur les paris
    recommandés et 8–11 sur des fantômes — la page jugeait le système sur des
    paris qu'il n'avait conseillés à personne."""
    return [r for r in rows if not is_phantom(r)]


def phantom_rows(rows: list[dict]) -> list[dict]:
    """Le complément de `recommended_rows` — voir `is_phantom`."""
    return [r for r in rows if is_phantom(r)]


def market_breakdown(rows: list[dict], tax_rate: float, min_n: int = 5) -> list[dict]:
    """Réussite par (sport, marché), la plus PERDANTE en premier — même contrat
    que `league_breakdown`. `market_type` d'abord, `market` en repli : les deux
    colonnes coexistent dans le ledger selon l'époque d'écriture.

    Mesuré le 2026-09-03 sur août+septembre : les pertes se concentraient par
    MARCHÉ plus que par ligue (spreads extérieur et unders basket 3–6 chacun,
    spreads extérieur soccer 9–9 à cote 1,94) — un tableau par ligue seul
    aurait fait chercher le levier au mauvais endroit."""
    groupes: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        mkt = (r.get("market_type") or r.get("market") or "").strip() or "(sans marché)"
        groupes.setdefault((r.get("sport") or "", mkt), []).append(r)
    out = []
    for (sport, mkt), lignes in groupes.items():
        decisifs = [r for r in lignes if r.get("outcome") in _DECISIF]
        if len(decisifs) < max(min_n, 1):
            continue
        d = _bloc_decisif(decisifs, tax_rate)
        d.update({"sport": sport, "market": mkt,
                  "expired": sum(1 for r in lignes if r.get("outcome") == "expired")})
        out.append(d)
    out.sort(key=lambda d: (d["pnl_units"], -d["n"], d["market"]))
    return out


def sport_breakdown(rows: list[dict], tax_rate: float) -> list[dict]:
    """Réussite par sport, même bloc que les mois et les ligues (Wilson, point
    mort, unités à mise plate), trié par unités croissantes. Remplace le calcul
    qui vivait en ligne dans api/index.py : une seule formule pour toute la page."""
    groupes: dict[str, list[dict]] = {}
    for r in rows:
        if r.get("outcome") in _DECISIF and r.get("sport"):
            groupes.setdefault(r["sport"], []).append(r)
    out = []
    for sport, decisifs in groupes.items():
        d = _bloc_decisif(decisifs, tax_rate)
        d["sport"] = sport
        out.append(d)
    out.sort(key=lambda d: (d["pnl_units"], d["sport"]))
    return out
