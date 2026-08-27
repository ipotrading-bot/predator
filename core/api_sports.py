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

from core import daily_quota
from core.math_engine import synthetic_dnb
from core.secret_store import get_secret

log = logging.getLogger("PREDATOR.api_sports")

# sport interne -> (hôte, endpoint calendrier, id harvester, a un nul ?, clés d'env)
PROVIDERS: dict[str, dict] = {
    "soccer": {
        "host": "v3.football.api-sports.io", "schedule": "fixtures",
        "sport_id": 1, "draw": True, "odds_by_date": True,
        "keys": ("API_FOOTBALL_KEY", "API_SPORTS_KEY"),
    },
    "basketball": {
        "host": "v1.basketball.api-sports.io", "schedule": "games", "odds_by_date": False,
        "sport_id": 4, "draw": False,
        "keys": ("API_BASKETBALL_KEY", "API_SPORTS_KEY"),
    },
    "baseball": {
        "host": "v1.baseball.api-sports.io", "schedule": "games", "odds_by_date": False,
        "sport_id": 6, "draw": False,
        "keys": ("API_BASEBALL_KEY", "API_SPORTS_KEY"),
    },
    "hockey": {
        "host": "v1.hockey.api-sports.io", "schedule": "games", "odds_by_date": False,
        "sport_id": 7, "draw": False,
        "keys": ("API_HOCKEY_KEY", "API_SPORTS_KEY"),
    },
}

MAX_ODDS_PAGES = 3
# Hors foot, les cotes ne s'obtiennent QUE match par match (`/odds?game=<id>`) :
# `date` n'existe pas sur ces hôtes, et `league`+`season` est fermé au plan
# gratuit au-delà de 2022 — constaté le 2026-08-20. Une requête par match, donc
# un plafond serré. Taux de réussite mesuré : ~2 matchs sur 3 ont des cotes.
MAX_GAME_ODDS = int(os.environ.get("API_SPORTS_MAX_GAME_ODDS", "8"))
QUOTA_GUARD    = 8      # requêtes restantes en dessous desquelles on rend la main

# BUDGET JOURNALIER — le garde-fou qui manquait.
#
# Le plan gratuit donne 100 requêtes/jour PAR SPORT. Quand le Tier 1 est mort,
# les ~40 scans quotidiens atteignent tous le Tier 2 : à 7 requêtes le cycle,
# cela ferait ~280 requêtes/jour, soit près du triple du plan — puis des 429 en
# rafale. Le compte api-sports de ce projet a d'ailleurs été trouvé SUSPENDU le
# 2026-08-20, alors que l'ancienne implémentation dépensait 1 requête PAR MATCH.
# Le compteur partagé de core/daily_quota.py (table Supabase `meta`) fait
# respecter ce plafond entre les runs ; sans Supabase il est inerte et la
# source reste utilisable.
DAILY_BUDGET = int(os.environ.get("API_SPORTS_DAILY_BUDGET", "80"))

# ── Réserve du SETTLEMENT, tenue EN NÉGATIF (2026-08-26) ──────────────
# Mesuré le jour même : le premier audit à utiliser api-sports pour les scores
# s'est heurté à « budget journalier atteint (80/80) » — les scans avaient tout
# consommé avant lui. Zéro settlement, 55 signaux condamnés.
#
# Même panne que le cloisonnement Groq du 2026-08-02, sur une autre ressource,
# et même remède : les SCANS sont amputés, la réserve n'est jamais partagée.
# Un scan de plus vaut moins qu'un résultat de moins — un signal sans score
# sort du ledger en `expired` et n'apprend rien à personne.
#
# Le total reste sous DAILY_BUDGET : le plan fait 100/jour, mais le compte de
# ce projet a déjà été SUSPENDU pour dépassement (2026-08-20). On ne mange pas
# la marge de sûreté, on la partage autrement.
RESULTS_RESERVE = int(os.environ.get("API_SPORTS_RESULTS_RESERVE", "16"))
SCAN_BUDGET = max(1, DAILY_BUDGET - RESULTS_RESERVE)

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


def _usage_get(sport: str) -> int:
    return daily_quota.spent(f"api_sports_{sport}")


def _usage_add(sport: str, n: int) -> None:
    daily_quota.add(f"api_sports_{sport}", n)


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


def _favourite_side(books: list[dict]) -> str:
    """'1' ou '2' — le côté favori au CONSENSUS des books soft.

    Décidé sur la moyenne des probabilités implicites et non sur un seul book :
    deux books peuvent se contredire sur un match serré, et laisser le choix du
    favori dépendre de l'ordre de la réponse HTTP rendrait le prix retenu
    instable d'un scan à l'autre.
    """
    tot1 = tot2 = 0.0
    n = 0
    for o in books:
        o1, o2 = float(o.get("1") or 0), float(o.get("2") or 0)
        if o1 <= 1.01 or o2 <= 1.01:
            continue
        tot1 += 1.0 / o1
        tot2 += 1.0 / o2
        n += 1
    if not n:
        return "1"
    return "1" if tot1 >= tot2 else "2"


def _executable_price(odds: dict, fav_key: str, draw: bool) -> float:
    """Prix que ce book-ci permet RÉELLEMENT de jouer sur le côté `fav_key`.

    Football : DNB synthétique, donc les DEUX jambes chez CE book.
    Hors football : la cote brute du côté, un seul pari.
    """
    fav = float(odds.get(fav_key) or 0)
    if not draw:
        return fav
    return synthetic_dnb(fav, float(odds.get("X") or 0))


def extract_prices(bookmakers: list, draw: bool) -> tuple[dict | None, dict | None]:
    """
    (soft, sharp) — `soft` est le bloc de cotes D'UN SEUL BOOK, celui dont le
    prix FINAL EXÉCUTABLE est le meilleur sur le côté qui sera joué ; `sharp`
    est le premier book sharp trouvé (aucun line shopping côté sharp : on veut
    LE prix de référence, pas le plus généreux).

    ⚠️ POURQUOI PLUS JAMAIS UN MAX PAR ISSUE (2026-08-27)
    -----------------------------------------------------
    Cette fonction retenait le meilleur prix issue par issue à travers tous les
    books soft. Le 1X2 qui en sortait n'était offert par PERSONNE : son « 1 »
    venait de Bwin, son « X » d'Unibet. Or le prix d'entrée du football est un
    DNB synthétique, qui engage deux jambes — et deux jambes ne se placent chez
    deux books qu'en admettant que la cote calculée n'est pas celle qu'on
    obtiendra. Mesuré le 2026-08-27 sur 30 matchs api-sports (3 à 12 books
    soft) : le 1X2 line-shoppé surestime le prix par-book de 0,73 point médian
    et de 2,46 au p90. C'est de l'edge fabriqué, qui s'ajoute à celui que la
    dévigorisation fabriquait déjà.

    Le line shopping n'est pas abandonné — il est déplacé au bon endroit : on
    compare les books sur le prix FINAL, puis on prend le bloc du gagnant
    INTACT. Un seul book, donc un prix réellement affiché.

    Hors football le pari final n'a qu'une jambe, mais on rend malgré tout le
    bloc d'un seul book : le côté opposé n'est jamais misé (le h2h joue le
    favori), et un bloc cohérent supprime à la racine toute recomposition
    accidentelle en aval.
    """
    soft_books: list[dict] = []
    sharp: dict | None = None
    for bk in bookmakers or []:
        odds = _bookmaker_odds(bk, draw)
        if not odds:
            continue
        if _is_sharp(bk):
            if sharp is None:
                sharp = odds
            continue
        soft_books.append(odds)

    if not soft_books:
        return None, sharp

    fav_key = _favourite_side(soft_books)
    # `max` conserve le premier ex aequo : à prix égal, l'ordre de la réponse
    # tranche, et il est stable pour un même payload. Quand aucun book ne
    # produit de prix exploitable (pas de nul en football), le premier est
    # rendu tel quel — le match reste prixé et c'est `to_binary` qui refusera,
    # au point unique où ce refus est déjà documenté.
    best = max(soft_books, key=lambda o: _executable_price(o, fav_key, draw))
    return best, sharp


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
    if spent >= SCAN_BUDGET:
        log.warning("api-sports[%s]: budget de SCAN atteint (%d/%d) — cycle ignoré. "
                    "Les %d requêtes restantes sont la réserve du settlement : un scan "
                    "de plus vaut moins qu'un résultat de moins.",
                    sport, spent, SCAN_BUDGET, RESULTS_RESERVE)
        return []

    base    = f"https://{prov['host']}"
    headers = {"x-apisports-key": key}
    now     = datetime.now(timezone.utc)
    until   = now + timedelta(hours=hours_ahead)
    used    = 0
    remaining = None

    # ── 1. Calendrier (1 requête, par plage de dates) ───────────────────
    try:
        # `date=` est le SEUL filtre calendrier commun aux quatre hôtes :
        # `from`/`to` exigent un `league`+`season` côté foot et n'existent pas
        # du tout ailleurs (vérifié en direct le 2026-08-20 — l'ancien appel
        # renvoyait un refus applicatif sur les quatre sports).
        r = requests.get(f"{base}/{prov['schedule']}", headers=headers, timeout=15,
                         params={"date": now.strftime("%Y-%m-%d"), "timezone": "UTC"})
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
            # On cale au budget de SCAN, pas au plafond total : un 429 pendant
            # un scan ne doit pas emporter la réserve du settlement avec lui.
            _usage_add(sport, max(used, SCAN_BUDGET))
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

    # La fenêtre de 24h chevauche presque toujours deux dates UTC : on va
    # chercher le lendemain aussi, sinon la moitié tardive du slate manque.
    if until.strftime("%Y-%m-%d") != now.strftime("%Y-%m-%d"):
        try:
            r2 = requests.get(f"{base}/{prov['schedule']}", headers=headers, timeout=15,
                              params={"date": until.strftime("%Y-%m-%d"), "timezone": "UTC"})
            used += 1
            if r2.status_code == 200:
                body2 = r2.json() or {}
                if not (body2.get("errors") or []):
                    schedule = schedule + (body2.get("response") or [])
        except Exception as e:
            log.debug("api-sports[%s] calendrier J+1: %s", sport, e)

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

    # ── 2. Cotes ────────────────────────────────────────────────────────
    matches: list[dict] = []
    seen: set[int] = set()
    n_sharp = 0
    stopped = ""

    def _absorb(items) -> None:
        nonlocal n_sharp
        for item in items or []:
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
                # Sans book soft, le prix sharp sert aussi de soft : edge nul,
                # donc jamais de signal, mais le match reste prixé.
                "odds_1xbet":    soft or sharp,
                "_soft_source":  f"api-sports/{sport}",
            }
            if sharp:
                m["odds_pinnacle"] = sharp
                n_sharp += 1
            matches.append(m)
            seen.add(gid)

    def _budget_left() -> bool:
        return remaining is None or remaining >= QUOTA_GUARD

    if prov["odds_by_date"]:
        # Foot : `/odds?date=&page=` — plusieurs matchs par réponse, c'est ce
        # qui rend la source tenable dans 100 requêtes/jour.
        for day in sorted({v["when"].strftime("%Y-%m-%d") for v in by_id.values()}):
            for page in range(1, MAX_ODDS_PAGES + 1):
                if not _budget_left():
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
                _absorb(body.get("response"))
                paging = body.get("paging") or {}
                try:
                    if int(paging.get("current", page)) >= int(paging.get("total", page)):
                        break
                except (TypeError, ValueError):
                    break
            if stopped:
                break
    else:
        # Basket/baseball/hockey : une requête PAR match, donc plafonnée.
        for gid in sorted(by_id, key=lambda g: by_id[g]["when"])[:MAX_GAME_ODDS]:
            if not _budget_left():
                stopped = f"garde quota ({remaining} restantes)"
                break
            try:
                r = requests.get(f"{base}/odds", headers=headers, timeout=15,
                                 params={"game": gid})
            except Exception as e:
                log.warning("api-sports[%s] odds game=%s: %s", sport, gid, e)
                stopped = "erreur réseau"
                break
            used += 1
            rem = _remaining(r)
            if rem is not None:
                remaining = rem
            if r.status_code != 200:
                log.warning("api-sports[%s] odds game=%s: HTTP %d", sport, gid, r.status_code)
                stopped = f"HTTP {r.status_code}"
                break
            _absorb((r.json() or {}).get("response"))

    _usage_add(sport, used)
    log.info("api-sports[%s]: %d matchs (%d avec prix sharp) / %d au calendrier | "
             "%d req%s | quota restant=%s",
             sport, len(matches), n_sharp, len(by_id), used,
             f" | arrêt: {stopped}" if stopped else "", remaining)
    return matches



# ── Résultats terminés (settlement déterministe, 2026-08-26) ──────────
# Le score final est un CHAMP de la réponse `/fixtures?date=`, pas quelque
# chose à faire chercher sur le web par un LLM. fetch_sport() télécharge déjà
# cette réponse à chaque scan et JETTE les matchs commencés (`if when < now:
# continue`) : la donnée du settlement était déjà payée, puis mise à la
# poubelle.
#
# Mesuré le 2026-08-26 : le settlement ne trouvait un résultat réel que pour
# 11 % des signaux depuis le 24 août (65 % trois jours plus tôt), parce que ses
# DEUX chemins de recherche web étaient morts en même temps — Tavily au plafond
# de plan (HTTP 432) et le `compound-mini` de Groq en limite par minute. Un
# audit a rendu « 0 settled | 52 skipped », en vert et sans alerte. Ici, aucun
# quota d'IA n'intervient : une requête par journée de calendrier règle tous
# les matchs de cette journée.
_STATUTS_TERMINES = frozenset({"FT", "AET", "PEN"})


def _score(item: dict, cote: str):
    """Buts d'un côté, quel que soit le sport (foot: goals.*, autres: scores.*)."""
    for chemin in (f"goals.{cote}", f"scores.{cote}.total", f"scores.{cote}.points",
                   f"scores.{cote}", f"{cote}.total"):
        v = _first(item, chemin)
        if isinstance(v, dict):
            v = v.get("total", v.get("points"))
        if v is not None:
            try:
                return int(v)
            except (TypeError, ValueError):
                continue
    return None


def fetch_results(jour: str, sport: str = "soccer", api_key: str | None = None) -> list[dict]:
    """Matchs TERMINÉS d'une journée UTC, avec leur score final.

    `jour` au format YYYY-MM-DD. Retourne
    [{"id", "home", "away", "home_score", "away_score", "when"}].
    Rend [] sur toute panne : le settlement retombe alors sur la recherche web.

    Coût : UNE requête par journée et par sport, quel que soit le nombre de
    matchs — à comparer aux 52 recherches LLM d'un seul audit.
    """
    prov = PROVIDERS.get(sport)
    if prov is None:
        return []
    key = api_key or _key_for(sport)
    if not key:
        log.debug("api-sports[%s] résultats : pas de clé — recherche web en repli", sport)
        return []
    spent = _usage_get(sport)
    if spent >= DAILY_BUDGET:
        log.warning("api-sports[%s] résultats : plafond TOTAL atteint (%d/%d) — même la "
                    "réserve du settlement est épuisée ; repli sur la recherche web",
                    sport, spent, DAILY_BUDGET)
        return []

    try:
        r = requests.get(f"https://{prov['host']}/{prov['schedule']}",
                         headers={"x-apisports-key": key}, timeout=15,
                         params={"date": jour, "timezone": "UTC"})
        _usage_add(sport, 1)
        if r.status_code != 200:
            log.warning("api-sports[%s] résultats %s: HTTP %d", sport, jour, r.status_code)
            return []
        body = r.json() or {}
        if body.get("errors"):
            log.warning("api-sports[%s] résultats %s: %s", sport, jour, body["errors"])
            return []
    except Exception as e:                       # jamais bloquant : c'est un bonus
        log.warning("api-sports[%s] résultats %s: %s", sport, jour, e)
        return []

    out: list[dict] = []
    for item in body.get("response") or []:
        try:
            statut = str(_first(item, "fixture.status.short", "status.short",
                                "status.long", default="") or "").upper()
            if statut not in _STATUTS_TERMINES:
                continue
            home = str(_first(item, "teams.home.name", "home.name", default="")).strip()
            away = str(_first(item, "teams.away.name", "away.name", default="")).strip()
            hs, as_ = _score(item, "home"), _score(item, "away")
            if not home or not away or hs is None or as_ is None:
                continue
            out.append({"id": _first(item, "fixture.id", "game.id", "id"),
                        "home": home, "away": away,
                        "home_score": hs, "away_score": as_,
                        "when": _parse_dt(_first(item, "fixture.timestamp", "timestamp",
                                                 "fixture.date", "date"))})
        except (TypeError, ValueError) as e:
            log.debug("api-sports[%s] parse résultat: %s", sport, e)
    log.info("api-sports[%s] résultats %s : %d match(s) terminé(s) — 1 requête",
             sport, jour, len(out))
    return out

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
