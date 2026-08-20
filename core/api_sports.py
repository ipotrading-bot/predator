"""
core/api_sports.py — famille api-sports.io : foot, basket, baseball, hockey.

POURQUOI CE MODULE
------------------
Incident 10→20 août 2026 : dix jours sans un seul signal. Toutes les sources
de repli étaient mortes en même temps, et surtout mortes POUR LA MÊME RAISON
— elles étaient toutes atteintes sans clé, donc filtrées par IP :

  1xbet/Melbet/22bet LineFeed  → timeout/blocage depuis les runners GitHub
  ESPN (site.api.espn.com)     → 403 Akamai depuis une IP datacenter
  SofaScore (api.sofascore.com)→ 403
  recherche web (Groq/Tavily)  → quotas gratuits épuisés

Vérifié le 2026-08-20 depuis une IP datacenter : les quatre hôtes ci-dessous
répondent HTTP 200 (`/status`, sans clé, erreur « missing application key »).
Une API authentifiée PAR CLÉ ne filtre pas par IP — c'est la propriété qui
manquait à toutes les autres sources, et la raison de préférer celle-ci.

UNE CLÉ, QUATRE SPORTS
----------------------
api-sports.io vend un abonnement par sport mais sur UN compte : la même clé
`x-apisports-key` est présentée aux quatre hôtes. `API_SPORTS_KEY` sert donc
de clé commune, et chaque sport peut la surcharger (`API_BASKETBALL_KEY`…)
si un jour ils vivent sur des comptes différents. `API_FOOTBALL_KEY`, déjà
configurée dans ce projet, reste prioritaire pour le foot.

Chaque sport a son propre quota gratuit (100 req/jour), donc les couper
n'est pas mutualisé : un baseball épuisé n'empêche pas le foot de tourner.

BUDGET
------
Un cycle = 1 requête « calendrier » + au plus MAX_ODDS_PAGES requêtes
« cotes » par date de la fenêtre. Les cotes sont demandées PAR DATE, pas par
match : c'est ce qui fait tenir un scan dans 100 req/jour là où l'ancienne
version (1 requête par match) épuisait le quota avant midi.

SHARP + SOFT DANS LA MÊME RÉPONSE
---------------------------------
`/odds` renvoie tous les bookmakers du plan. On en tire DEUX prix :
  - `odds_pinnacle` — le book sharp s'il est présent (voir SHARP_NAMES) ;
  - `odds_1xbet`    — meilleur prix parmi les books soft (line shopping).
Un match qui a les deux produit un signal SANS aucune recherche web. C'est
exactement ce qui manquait quand Groq/Tavily étaient à sec.

⚠️ FORMES DE RÉPONSE — les quatre APIs ne sont pas identiques (foot :
`/fixtures` + `fixture.id` ; les autres : `/games` + `game.id`), et seul le
foot a un match nul. Les extracteurs ci-dessous sont donc TOLÉRANTS : ils
acceptent plusieurs emplacements pour l'identifiant et l'horodatage, et
ignorent proprement ce qu'ils ne reconnaissent pas. Les formes exactes
n'ont pas pu être vérifiées live faute de clé dans le sandbox — la première
exécution le dira, chaque cycle loggant une ligne « api-sports[<sport>] ».
"""
import logging
import os
from datetime import datetime, timedelta, timezone

import requests

from core.secret_store import get_secret

log = logging.getLogger("PREDATOR.api_sports")

# sport interne -> (hôte, endpoint calendrier, id harvester, a un nul ?, clés d'env)
PROVIDERS: dict[str, dict] = {
    "soccer": {
        "host": "v3.football.api-sports.io", "schedule": "fixtures",
        "sport_id": 1, "draw": True,
        "keys": ("API_FOOTBALL_KEY", "API_SPORTS_KEY"),
    },
    "basketball": {
        "host": "v1.basketball.api-sports.io", "schedule": "games",
        "sport_id": 4, "draw": False,
        "keys": ("API_BASKETBALL_KEY", "API_SPORTS_KEY"),
    },
    "baseball": {
        "host": "v1.baseball.api-sports.io", "schedule": "games",
        "sport_id": 6, "draw": False,
        "keys": ("API_BASEBALL_KEY", "API_SPORTS_KEY"),
    },
    "hockey": {
        "host": "v1.hockey.api-sports.io", "schedule": "games",
        "sport_id": 7, "draw": False,
        "keys": ("API_HOCKEY_KEY", "API_SPORTS_KEY"),
    },
}

MAX_ODDS_PAGES = 3
QUOTA_GUARD    = 8      # requêtes restantes en dessous desquelles on rend la main

# BUDGET JOURNALIER — le garde-fou qui manquait.
#
# Le plan gratuit donne 100 requêtes/jour PAR SPORT. Quand le Tier 1 est mort,
# les ~40 scans quotidiens atteignent tous le Tier 2 : à 7 requêtes le cycle,
# cela ferait ~280 requêtes/jour, soit près du triple du plan — puis des 429 en
# rafale. Le compte api-sports de ce projet a d'ailleurs été trouvé SUSPENDU le
# 2026-08-20, alors que l'ancienne implémentation dépensait 1 requête PAR MATCH.
# On tient donc un compteur par sport et par jour dans la table Supabase `meta`,
# partagé entre tous les runs.
#
# Sans Supabase (tests, sandbox), le compteur est simplement inopérant : la
# source reste utilisable, on perd juste le partage entre runs.
DAILY_BUDGET = int(os.environ.get("API_SPORTS_DAILY_BUDGET", "80"))

# Reconnaissance par NOM (insensible à la casse) et non par id numérique :
# les ids de bookmakers de la doc n'ont pas pu être vérifiés, et un id faux
# renverrait silencieusement zéro prix sharp.
SHARP_NAMES = ("pinnacle", "betfair", "smarkets", "matchbook")

# Noms de marché « vainqueur du match » selon les sports/bookmakers.
WINNER_BETS = {"match winner", "home/away", "1x2", "full time result",
               "winner", "moneyline", "money line", "to win match",
               "game winner", "3way result", "1x2 full time"}

_HOME = {"home", "1", "home team"}
_AWAY = {"away", "2", "away team"}
_DRAW = {"draw", "x", "tie"}


def _usage_key(sport: str) -> str:
    return f"api_sports_usage_{sport}_{datetime.now(timezone.utc):%Y%m%d}"


def _usage_get(sport: str) -> int:
    """Requêtes déjà dépensées aujourd'hui pour ce sport (0 si inconnu)."""
    try:
        from core.db import get_db
        sb = get_db(write=True)
        if sb is None:
            return 0
        row = sb.table("meta").select("value").eq("key", _usage_key(sport)).maybe_single().execute()
        return int((row.data or {}).get("value") or 0) if row and row.data else 0
    except Exception as e:
        log.debug("api-sports[%s]: compteur illisible (%s)", sport, e)
        return 0


def _usage_add(sport: str, n: int) -> None:
    if n <= 0:
        return
    try:
        from core.db import get_db
        sb = get_db(write=True)
        if sb is None:
            return
        sb.table("meta").upsert(
            {"key": _usage_key(sport), "value": str(_usage_get(sport) + n),
             "updated_at": datetime.now(timezone.utc).isoformat()},
            on_conflict="key").execute()
    except Exception as e:
        log.debug("api-sports[%s]: compteur non écrit (%s)", sport, e)


def _key_for(sport: str) -> str | None:
    for name in PROVIDERS[sport]["keys"]:
        val = get_secret(name)
        if val:
            return val
    return None


def _odd(val) -> float:
    try:
        f = float(val)
        return f if f > 1.01 else 0.0
    except (TypeError, ValueError):
        return 0.0


def _first(d: dict, *paths, default=None):
    """Première valeur non nulle parmi des chemins « a.b.c » — absorbe les
    divergences de forme entre les quatre APIs sans quatre parseurs."""
    for path in paths:
        cur = d
        for part in path.split("."):
            if not isinstance(cur, dict):
                cur = None
                break
            cur = cur.get(part)
        if cur not in (None, "", {}):
            return cur
    return default


def _parse_dt(raw) -> datetime | None:
    if isinstance(raw, (int, float)):
        try:
            return datetime.fromtimestamp(raw, tz=timezone.utc)
        except (ValueError, OSError):
            return None
    if isinstance(raw, str) and raw:
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _is_sharp(bookmaker: dict) -> bool:
    name = str(bookmaker.get("name", "")).strip().lower()
    return any(s in name for s in SHARP_NAMES)


def _bookmaker_odds(bookmaker: dict, draw: bool) -> dict | None:
    for bet in bookmaker.get("bets", []) or []:
        if str(bet.get("name", "")).strip().lower() not in WINNER_BETS:
            continue
        o1 = ox = o2 = 0.0
        for v in bet.get("values", []) or []:
            label = str(v.get("value", "")).strip().lower()
            price = _odd(v.get("odd"))
            if label in _HOME:
                o1 = price
            elif label in _AWAY:
                o2 = price
            elif label in _DRAW:
                ox = price
        if o1 and o2:
            return {"1": o1, "X": ox if draw else 0.0, "2": o2}
    return None


def extract_prices(bookmakers: list, draw: bool) -> tuple[dict | None, dict | None]:
    """(soft, sharp) — soft = meilleur prix par issue hors books sharp ;
    sharp = le premier book sharp trouvé (pas de line shopping côté sharp :
    on veut LE prix de référence, pas le plus généreux)."""
    soft: dict = {}
    sharp: dict | None = None
    for bk in bookmakers or []:
        odds = _bookmaker_odds(bk, draw)
        if not odds:
            continue
        if _is_sharp(bk):
            if sharp is None:
                sharp = odds
            continue
        if not soft:
            soft = dict(odds)
        else:
            for k in ("1", "X", "2"):
                if odds.get(k, 0.0) > soft.get(k, 0.0):
                    soft[k] = odds[k]
    return (soft or None), sharp


def _remaining(resp) -> int | None:
    for h in ("x-ratelimit-requests-remaining", "X-RateLimit-requests-Remaining"):
        try:
            return int(resp.headers.get(h, ""))
        except (TypeError, ValueError):
            continue
    return None


def fetch_sport(sport: str, api_key: str | None = None, hours_ahead: int = 24) -> list[dict]:
    """Matchs à venir + cotes pour UN sport de PROVIDERS.

    Sortie : même forme que core/harvester.py::_parse_xbet_json, plus
    `commence_time` et, quand le plan l'expose, `odds_pinnacle`.
    Ne lève jamais : rend [] et logge la raison.
    """
    prov = PROVIDERS.get(sport)
    if prov is None:
        return []
    key = api_key or _key_for(sport)
    if not key:
        log.debug("api-sports[%s]: pas de clé (%s) — source ignorée",
                  sport, "/".join(prov["keys"]))
        return []

    spent = _usage_get(sport)
    if spent >= DAILY_BUDGET:
        log.warning("api-sports[%s]: budget journalier atteint (%d/%d) — cycle ignoré "
                    "(le plan gratuit fait 100 req/jour et un dépassement soutenu "
                    "fait suspendre le compte)", sport, spent, DAILY_BUDGET)
        return []

    base    = f"https://{prov['host']}"
    headers = {"x-apisports-key": key}
    now     = datetime.now(timezone.utc)
    until   = now + timedelta(hours=hours_ahead)
    used    = 0
    remaining = None

    # ── 1. Calendrier (1 requête, par plage de dates) ───────────────────
    try:
        r = requests.get(f"{base}/{prov['schedule']}", headers=headers, timeout=15,
                         params={"from": now.strftime("%Y-%m-%d"),
                                 "to": until.strftime("%Y-%m-%d"),
                                 "timezone": "UTC"})
        used += 1
        remaining = _remaining(r)
        if r.status_code in (401, 403):
            _usage_add(sport, used)
            log.error("api-sports[%s]: auth refusée (HTTP %d) — vérifier %s",
                      sport, r.status_code, prov["keys"][0])
            return []
        if r.status_code == 429:
            # Quota atteint côté API : on cale le compteur local au plafond
            # pour ne pas retenter 40 fois dans la journée.
            _usage_add(sport, max(used, DAILY_BUDGET))
            log.warning("api-sports[%s]: HTTP 429 — quota journalier épuisé", sport)
            return []
        if r.status_code != 200:
            log.warning("api-sports[%s] %s: HTTP %d", sport, prov["schedule"], r.status_code)
            return []
        body = r.json() or {}
        # api-sports répond 200 avec un objet `errors` non vide quand la clé
        # est absente/non abonnée à CE sport — un 200 n'est pas un succès ici.
        errs = body.get("errors")
        if errs and (isinstance(errs, dict) and errs or isinstance(errs, list) and errs):
            # Un compte suspendu ou non abonné répond 200 avec ce corps.
            # L'ancienne implémentation le lisait comme un succès vide : dix
            # jours sans une seule ligne de log (constaté le 2026-08-20).
            _usage_add(sport, used)
            log.warning("api-sports[%s]: refus applicatif %s", sport, str(errs)[:160])
            return []
        schedule = body.get("response", []) or []
    except Exception as e:
        _usage_add(sport, used)
        log.error("api-sports[%s] %s: %s", sport, prov["schedule"], e)
        return []

    by_id: dict[int, dict] = {}
    for item in schedule:
        try:
            gid = _first(item, "fixture.id", "game.id", "id")
            raw_dt = _first(item, "fixture.timestamp", "timestamp", "fixture.date", "date")
            when = _parse_dt(raw_dt)
            home = str(_first(item, "teams.home.name", "home.name", default="")).strip()
            away = str(_first(item, "teams.away.name", "away.name", default="")).strip()
            if gid is None or when is None or not home or not away:
                continue
            if when < now or when > until:
                continue
            by_id[int(gid)] = {"home": home, "away": away, "when": when,
                               "league": str(_first(item, "league.name", default="Unknown"))}
        except (TypeError, ValueError) as e:
            log.debug("api-sports[%s] parse calendrier: %s", sport, e)

    if not by_id:
        _usage_add(sport, used)
        log.info("api-sports[%s]: 0 match dans les %dh (%d bruts) | quota restant=%s",
                 sport, hours_ahead, len(schedule), remaining)
        return []

    # ── 2. Cotes PAR DATE, paginées ─────────────────────────────────────
    matches: list[dict] = []
    seen: set[int] = set()
    n_sharp = 0
    stopped = ""
    for day in sorted({v["when"].strftime("%Y-%m-%d") for v in by_id.values()}):
        for page in range(1, MAX_ODDS_PAGES + 1):
            if remaining is not None and remaining < QUOTA_GUARD:
                stopped = f"garde quota ({remaining} restantes)"
                break
            try:
                r = requests.get(f"{base}/odds", headers=headers, timeout=15,
                                 params={"date": day, "page": page, "timezone": "UTC"})
            except Exception as e:
                log.warning("api-sports[%s] odds %s p%d: %s", sport, day, page, e)
                stopped = "erreur réseau"
                break
            used += 1
            rem = _remaining(r)
            if rem is not None:
                remaining = rem
            if r.status_code != 200:
                log.warning("api-sports[%s] odds %s p%d: HTTP %d (restant=%s)",
                            sport, day, page, r.status_code, remaining)
                stopped = f"HTTP {r.status_code}"
                break
            body = r.json() or {}
            for item in body.get("response", []) or []:
                gid = _first(item, "fixture.id", "game.id", "id")
                try:
                    gid = int(gid)
                except (TypeError, ValueError):
                    continue
                meta = by_id.get(gid)
                if meta is None or gid in seen:
                    continue
                soft, sharp = extract_prices(item.get("bookmakers", []) or [], prov["draw"])
                if not soft and not sharp:
                    continue
                m = {
                    "id":            f"as_{sport}_{gid}",
                    "match":         f"{meta['home']} vs {meta['away']}",
                    "home":          meta["home"],
                    "away":          meta["away"],
                    "league":        meta["league"],
                    "sport":         sport,
                    "sport_id":      prov["sport_id"],
                    "commence_time": meta["when"].strftime("%Y-%m-%dT%H:%M:%SZ"),
                    # Sans book soft, le prix sharp sert de soft : edge nul,
                    # donc aucun signal, mais le match reste prixé pour le
                    # merge multi-sources en aval.
                    "odds_1xbet":    soft or sharp,
                    "_soft_source":  f"api-sports/{sport}",
                }
                if sharp:
                    m["odds_pinnacle"] = sharp
                    n_sharp += 1
                matches.append(m)
                seen.add(gid)
            paging = body.get("paging") or {}
            try:
                if int(paging.get("current", page)) >= int(paging.get("total", page)):
                    break
            except (TypeError, ValueError):
                break
        if stopped:
            break

    _usage_add(sport, used)
    log.info("api-sports[%s]: %d matchs (%d avec prix sharp) / %d au calendrier | "
             "%d req%s | quota restant=%s",
             sport, len(matches), n_sharp, len(by_id), used,
             f" | arrêt: {stopped}" if stopped else "", remaining)
    return matches


def fetch_all(hours_ahead: int = 24, sports: list[str] | None = None) -> list[dict]:
    """Tous les sports configurés. Un sport sans clé, hors quota ou en panne
    ne pénalise pas les autres (quotas séparés, erreurs isolées)."""
    out: list[dict] = []
    for sport in (sports or list(PROVIDERS)):
        try:
            out.extend(fetch_sport(sport, hours_ahead=hours_ahead))
        except Exception as e:      # ceinture et bretelles : jamais vers l'appelant
            log.error("api-sports[%s]: %s", sport, e)
    return out


def available_sports() -> list[str]:
    """Sports pour lesquels une clé est résolue — pour les logs/diagnostics."""
    return [s for s in PROVIDERS if _key_for(s)]
