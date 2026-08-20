"""
core/odds_api_io.py — odds-api.io : cotes SOFT (1xbet & co.), 500 req/jour.

CE QUE ÇA APPORTE
-----------------
Le modèle d'edge de PREDATOR compare une référence sharp au prix d'un book
SOFT — historiquement 1xbet, via son endpoint LineFeed. Or ce LineFeed est
injoignable depuis les runners GitHub (timeout/203, vérifié le 2026-08-20) :
le côté soft ne tenait plus que sur OddsAPI, et mourait avec lui.

odds-api.io redonne accès aux mêmes books par une voie AUTHENTIFIÉE (donc
non filtrée par IP) et licenciée — ce n'est pas un contournement du blocage
LineFeed mais un accès légitime au même prix.

Vérifié en direct le 2026-08-20 avec la clé du projet :
  GET /v3/events?sport=football&from=…&to=…   → 40 matchs à venir
  GET /v3/odds/multi?eventIds=<10>&bookmakers=1xbet
    → ML  {"home","draw","away"} | Spread {"hdp","home","away"}
      Totals {"hdp","over","under"}
soit exactement les trois marchés du moteur (h2h / spreads / totals).

BUDGET — le point de conception
-------------------------------
Les cotes se demandent PAR ÉVÉNEMENT, mais `/v3/odds/multi` en accepte DIX
par appel. Un scan coûte donc 1 requête de calendrier + ceil(N/10) requêtes
de cotes : ~5 pour 40 matchs. À ~40 scans/jour cela fait ~200 requêtes,
sous le plafond de 500. Le compteur partagé de core/daily_quota.py fait
respecter ce plafond entre les runs — sans lui, une journée où Tier 1 est
mort suffirait à le dépasser (voir la suspension du compte api-sports).

LES BOOKS SÉLECTIONNÉS
----------------------
Le plan restreint le nombre de bookmakers actifs simultanément ; le compte
est interrogé à l'exécution (`/v3/bookmakers/selected`) plutôt que codé en
dur, pour qu'un changement côté odds-api.io soit pris en compte sans
toucher au code. `ODDS_API_IO_BOOKMAKERS` (CSV) permet de forcer la liste
et d'économiser cette requête.

Si l'un des books sélectionnés est un exchange ou un book sharp (voir
SHARP_NAMES), son prix ressort en `odds_pinnacle` et le match devient
exploitable sans aucune autre source.

CGU : les cotes servent au CALCUL interne. Leur redistribution telle quelle
est interdite — ne pas les republier brutes sur le dashboard public.
"""
import logging
import os
from datetime import datetime, timedelta, timezone

import requests

from core import daily_quota
from core.secret_store import get_secret

log = logging.getLogger("PREDATOR.odds_api_io")

BASE_URL = "https://api.odds-api.io/v3"
QUOTA_BUCKET = "odds_api_io"

# sport interne PREDATOR -> (slug odds-api.io, sport_id harvester, a un nul ?)
SPORTS: dict[str, tuple[str, int, bool]] = {
    "soccer":     ("football",           1, True),
    "tennis":     ("tennis",             3, False),
    "basketball": ("basketball",         4, False),
    "mma":        ("mixed-martial-arts", 5, False),
    "baseball":   ("baseball",           6, False),
    "hockey":     ("ice-hockey",         7, False),
}

# Plan gratuit : 500 requêtes/jour. On garde une marge : le settlement et
# d'éventuels appels manuels passent par le même compte.
DAILY_BUDGET = int(os.environ.get("ODDS_API_IO_DAILY_BUDGET", "400"))
MULTI_BATCH  = 10          # maximum accepté par /v3/odds/multi
MAX_EVENTS   = int(os.environ.get("ODDS_API_IO_MAX_EVENTS", "60"))
TIMEOUT      = int(os.environ.get("ODDS_API_IO_TIMEOUT", "25"))

SHARP_NAMES = ("pinnacle", "betfair exchange", "smarkets", "matchbook")

_selected_cache: list[str] | None = None


def _key(api_key: str | None = None) -> str | None:
    return api_key or get_secret("ODDS_API_IO_KEY")


def _get(path: str, key: str, params: dict) -> tuple[int, object]:
    """(status, corps) — ne lève jamais. Compte la requête au budget du jour."""
    try:
        r = requests.get(f"{BASE_URL}/{path}", timeout=TIMEOUT,
                         params={**params, "apiKey": key})
    except Exception as e:
        log.warning("odds-api.io %s: %s", path, e)
        return 0, None
    daily_quota.add(QUOTA_BUCKET, 1)
    if r.status_code != 200:
        body = (r.text or "")[:160]
        log.warning("odds-api.io %s: HTTP %d %s", path, r.status_code, body)
        return r.status_code, None
    try:
        return 200, r.json()
    except Exception as e:
        log.warning("odds-api.io %s: réponse illisible (%s)", path, e)
        return 200, None


def selected_bookmakers(key: str, *, force: bool = False) -> list[str]:
    """Books actifs sur le compte. `ODDS_API_IO_BOOKMAKERS` court-circuite
    l'appel réseau ; sinon le résultat est mémorisé pour le processus."""
    global _selected_cache
    forced = os.environ.get("ODDS_API_IO_BOOKMAKERS", "").strip()
    if forced:
        return [b.strip() for b in forced.split(",") if b.strip()]
    if _selected_cache is not None and not force:
        return _selected_cache
    _, body = _get("bookmakers/selected", key, {})
    books: list[str] = []
    if isinstance(body, dict):
        books = [str(b) for b in (body.get("bookmakers") or [])]
    elif isinstance(body, list):
        books = [str(b) for b in body]
    _selected_cache = books
    return books


def reset_cache() -> None:
    global _selected_cache
    _selected_cache = None


def _odd(val) -> float:
    try:
        f = float(val)
        return f if f > 1.01 else 0.0
    except (TypeError, ValueError):
        return 0.0


def _is_sharp(book: str) -> bool:
    low = book.lower()
    return any(s in low for s in SHARP_NAMES)


def _pick_main_line(lines: list, a_key: str, b_key: str) -> dict | None:
    """La ligne PRINCIPALE parmi plusieurs handicaps/totaux : celle dont les
    deux prix sont les plus proches.

    Le book en publie une douzaine (hdp -3.75 à +3.75) ; prendre la première
    reviendrait à retenir une ligne extrême cotée 1.01/8.60, hors marché et
    sans rapport avec la ligne que le moteur compare au sharp.
    """
    best, best_gap = None, None
    for row in lines or []:
        a, b = _odd(row.get(a_key)), _odd(row.get(b_key))
        if not a or not b:
            continue
        gap = abs(a - b)
        if best_gap is None or gap < best_gap:
            best, best_gap = row, gap
    return best


def _markets(entries: list, draw: bool) -> dict:
    """Convertit les marchés d'un book vers la forme attendue par le moteur
    (voir core/odds_api.py : `odds_*`, `spreads_*`, `totals_*`)."""
    out: dict = {}
    for m in entries or []:
        name = str(m.get("name", "")).strip().lower()
        rows = m.get("odds") or []
        if name in ("ml", "moneyline", "1x2", "match winner") and rows:
            r = rows[0]
            o1, o2 = _odd(r.get("home")), _odd(r.get("away"))
            if o1 and o2:
                out["h2h"] = {"1": o1, "X": _odd(r.get("draw")) if draw else 0.0, "2": o2}
        elif name == "spread":
            r = _pick_main_line(rows, "home", "away")
            if r is not None:
                try:
                    point = float(r.get("hdp"))
                except (TypeError, ValueError):
                    continue
                out["spreads"] = {"home": _odd(r.get("home")), "away": _odd(r.get("away")),
                                  "point": point, "away_point": -point}
        elif name == "totals":
            r = _pick_main_line(rows, "over", "under")
            if r is not None:
                try:
                    point = float(r.get("hdp"))
                except (TypeError, ValueError):
                    continue
                out["totals"] = {"over": _odd(r.get("over")), "under": _odd(r.get("under")),
                                 "point": point}
    return out


def _to_match(ev: dict, sport: str, sport_id: int, draw: bool) -> dict | None:
    home = str(ev.get("home", "")).strip()
    away = str(ev.get("away", "")).strip()
    if not home or not away:
        return None
    soft: dict = {}
    sharp: dict = {}
    for book, entries in (ev.get("bookmakers") or {}).items():
        parsed = _markets(entries, draw)
        if not parsed:
            continue
        if _is_sharp(str(book)):
            if not sharp:
                sharp = parsed
        elif not soft:
            soft = parsed
        else:
            # Line shopping : meilleur prix par issue entre books soft.
            for k in ("1", "X", "2"):
                if parsed.get("h2h", {}).get(k, 0) > soft.get("h2h", {}).get(k, 0):
                    soft.setdefault("h2h", {})[k] = parsed["h2h"][k]
    base = soft or sharp
    if not base.get("h2h"):
        return None

    out = {
        "id":            f"oai_{ev.get('id')}",
        "match":         f"{home} vs {away}",
        "home":          home,
        "away":          away,
        "league":        str((ev.get("league") or {}).get("name", "Unknown")),
        "sport":         sport,
        "sport_id":      sport_id,
        "commence_time": str(ev.get("date", "")),
        "odds_1xbet":    base["h2h"],
        "_soft_source":  "odds-api.io",
    }
    if base.get("spreads"):
        out["spreads_1xbet"] = base["spreads"]
    if base.get("totals"):
        out["totals_1xbet"] = base["totals"]
    if sharp.get("h2h"):
        out["odds_pinnacle"] = sharp["h2h"]
        if sharp.get("spreads"):
            out["spreads_pinnacle"] = sharp["spreads"]
        if sharp.get("totals"):
            out["totals_pinnacle"] = sharp["totals"]
    return out


def fetch_sport(sport: str, api_key: str | None = None, hours_ahead: int = 24,
                max_events: int | None = None) -> list[dict]:
    """Matchs à venir + cotes pour UN sport. Rend [] sans clé, hors budget,
    ou sur panne — jamais d'exception, toujours une ligne de log."""
    cfg = SPORTS.get(sport)
    if cfg is None:
        return []
    slug, sport_id, draw = cfg
    key = _key(api_key)
    if not key:
        log.debug("odds-api.io: pas de clé (ODDS_API_IO_KEY) — source ignorée")
        return []

    used_before = daily_quota.spent(QUOTA_BUCKET)
    if used_before >= DAILY_BUDGET:
        log.warning("odds-api.io[%s]: budget journalier atteint (%d/%d) — cycle ignoré",
                    sport, used_before, DAILY_BUDGET)
        return []

    books = selected_bookmakers(key)
    if not books:
        log.warning("odds-api.io[%s]: aucun bookmaker sélectionné sur le compte "
                    "— rien à demander", sport)
        return []

    now   = datetime.now(timezone.utc)
    until = now + timedelta(hours=hours_ahead)
    cap   = max_events or MAX_EVENTS
    status, body = _get("events", key, {
        "sport": slug,
        "from": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "to":   until.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "limit": str(cap),
    })
    if status != 200 or not isinstance(body, list):
        return []

    # `pending` = à venir. Les statuts live/settled/cancelled n'ont rien à
    # faire dans un scan pré-match.
    events = [e for e in body if str(e.get("status", "")).lower() == "pending"][:cap]
    if not events:
        log.info("odds-api.io[%s]: 0 match à venir dans les %dh", sport, hours_ahead)
        return []

    matches: list[dict] = []
    book_param = ",".join(books)
    for i in range(0, len(events), MULTI_BATCH):
        if daily_quota.spent(QUOTA_BUCKET) >= DAILY_BUDGET:
            log.warning("odds-api.io[%s]: budget épuisé en cours de cycle — "
                        "%d matchs conservés", sport, len(matches))
            break
        batch = events[i:i + MULTI_BATCH]
        status, body = _get("odds/multi", key, {
            "eventIds": ",".join(str(e.get("id")) for e in batch),
            "bookmakers": book_param,
        })
        if status != 200 or not isinstance(body, list):
            break
        for ev in body:
            m = _to_match(ev, sport, sport_id, draw)
            if m:
                matches.append(m)

    n_sharp = sum(1 for m in matches if m.get("odds_pinnacle"))
    log.info("odds-api.io[%s]: %d matchs (%d avec prix sharp) / %d à venir | "
             "books=%s | %d req au total aujourd'hui",
             sport, len(matches), n_sharp, len(events), book_param,
             daily_quota.spent(QUOTA_BUCKET))
    return matches


def fetch_all(hours_ahead: int = 24, sports: list[str] | None = None) -> list[dict]:
    """Tous les sports demandés ; l'échec de l'un n'emporte pas les autres."""
    out: list[dict] = []
    for sport in (sports or ["soccer", "basketball", "baseball", "hockey"]):
        try:
            out.extend(fetch_sport(sport, hours_ahead=hours_ahead))
        except Exception as e:
            log.error("odds-api.io[%s]: %s", sport, e)
    return out


def probe(api_key: str | None = None) -> tuple[bool, str]:
    """(utilisable ?, détail) — pour scripts/ops.py sources."""
    key = _key(api_key)
    if not key:
        return False, "pas de clé (ODDS_API_IO_KEY)"
    status, body = _get("bookmakers/selected", key, {})
    if status != 200:
        return False, f"HTTP {status}"
    books = (body or {}).get("bookmakers") if isinstance(body, dict) else body
    used = daily_quota.spent(QUOTA_BUCKET)
    return True, f"OK — books={books} | {used}/{DAILY_BUDGET} req aujourd'hui"
