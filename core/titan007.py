"""
core/titan007.py — Titan007 / 球探网 : soft ET sharp, sur les ligues que
personne d'autre ne couvre gratuitement.

CE QUE ÇA RÉSOUT
----------------
Après avoir rebranché Matchbook (sharp) et odds-api.io (soft), il restait un
trou mesurable : le 2026-08-20, 23 matchs soft contre 56 marchés sharp ne
donnaient que **8 matchs des deux côtés**. Les deux sources ne regardent pas
les mêmes ligues. Matchbook est riche en Amérique du Sud (Libertadores,
Sudamericana, réserves argentines, Brésil D2, Équateur, Colombie, Mexique) ;
odds-api.io l'est beaucoup moins.

Titan007 couvre exactement ce gisement : 286 matchs par jour sur 85 ligues,
massivement hors Europe, avec jusqu'à **157 bookmakers par match** — dont
Pinnacle, Bet365, 1xBet, Sbobet, Marathonbet. Un seul appel de calendrier,
puis un appel par match.

POURQUOI ELLE PASSE LÀ OÙ LES AUTRES ÉCHOUENT
---------------------------------------------
Toutes les sources sans clé testées jusqu'ici sont filtrées par IP depuis les
runners GitHub (1xbet LineFeed : 203, ESPN : 403 Akamai, SofaScore : 403).
Celle-ci répond HTTP 200 depuis deux points de sortie datacenter distincts
(Azure GB et US), vérifié le 2026-08-20. C'est la propriété qui compte.

STATUT JURIDIQUE — À CONNAÎTRE
------------------------------
C'est une API interne non documentée. Aucune interdiction explicite d'usage
automatisé (pas de page de CGU, `1x2d` sans robots.txt), mais aucune
autorisation explicite non plus : **une tolérance, pas un contrat**. Deux
conséquences tenues dans le code :
  - les deux endpoints utilisés sont SANS query string et hors des chemins
    interdits par le robots.txt de `bf.titan007.com` (qui bannit `/*?*`).
    Ne jamais y ajouter de paramètre : cela les ferait basculer sous un
    `Disallow`. Le feed de handicap asiatique en porte une — il est
    volontairement absent de ce module ;
  - cadence délibérément basse (`REQUEST_DELAY`, budget journalier partagé),
    et traitement en source best-effort : toute panne rend [] avec un log,
    jamais une exception.

FUSEAU HORAIRE — LE PIÈGE
-------------------------
Le calendrier donne la date et l'heure dans le fuseau du site, sans le dire.
Calibré le 2026-08-20 contre les heures UTC de Matchbook sur 14 matchs
communs : **UTC+8** (12 concordances exactes ; les 2 écarts étaient de faux
appariements entre équipes réserves argentines). Se tromper ici décalerait
tous les `match_time` de huit heures — les signaux seraient refusés par le
garde « match déjà commencé », ou pire, réglés sur le mauvais match.
"""
import logging
import os
import re
import time
import urllib.request
from datetime import datetime, timedelta, timezone

from core import daily_quota

log = logging.getLogger("PREDATOR.titan007")

FIXTURES_URL = "https://bf.titan007.com/vbsxml/bfdata_ut.js"
ODDS_URL     = "http://1x2d.titan007.com/{sid}.js"

# Décalage du calendrier par rapport à UTC — voir le docstring.
SITE_UTC_OFFSET_H = int(os.environ.get("TITAN007_UTC_OFFSET", "8"))

QUOTA_BUCKET  = "titan007"
DAILY_BUDGET  = int(os.environ.get("TITAN007_DAILY_BUDGET", "500"))
MAX_MATCHES   = int(os.environ.get("TITAN007_MAX_MATCHES", "40"))
REQUEST_DELAY = float(os.environ.get("TITAN007_DELAY", "0.4"))
TIMEOUT       = int(os.environ.get("TITAN007_TIMEOUT", "25"))

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/127.0 Safari/537.36")
_HEADERS = {"User-Agent": _UA, "Referer": "https://www.titan007.com/"}

# Books SHARP, par ordre de préférence : on veut LA référence, pas la plus
# généreuse. Pinnacle d'abord (c'est celle que le ledger historise depuis
# toujours), les exchanges ensuite.
SHARP_BOOKS = ("pinnacle", "betfair exchange", "matchbook", "smarkets")

# Books SOFT retenus pour le line shopping. Liste FERMÉE volontairement : le
# feed en contient 157, dont des books obscurs ou figés dont la cote aberrante
# créerait un edge qui n'existe pas. On ne garde que des books réellement
# jouables, cohérents avec l'historique du projet (1xBet en tête).
SOFT_BOOKS = (
    "1xbet", "bet 365", "bet365", "melbet", "william hill", "bwin", "betway",
    "unibet", "marathonbet", "marathon", "sbobet", "interwetten", "ladbrokes",
    "bet-at-home", "188bet", "betcris", "12bet", "megapari", "mostbet",
    "betsson", "betano", "tipico", "netbet", "vbet", "parimatch",
)

# Sport : le feed 1x2d est FOOTBALL uniquement (vérifié — un id de basket y
# renvoie un vieux match de foot). Ne pas y chercher d'autres sports.
SPORT      = "soccer"
SPORT_ID   = 1


def _get(url: str) -> str | None:
    try:
        req = urllib.request.Request(url, headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.read().decode("utf-8", "replace")
    except Exception as e:
        log.warning("titan007: %s — %s", url.split("/")[2], e)
        return None


def _odd(val) -> float:
    try:
        f = float(val)
        return f if 1.01 < f < 1000 else 0.0
    except (TypeError, ValueError):
        return 0.0


def _kickoff_utc(row: list) -> datetime | None:
    """Champs [43] année, [36] « M-D », [11] « HH:MM », fuseau du site."""
    try:
        month, day = row[36].split("-")
        hour, minute = row[11].split(":")
        local = datetime(int(row[43]), int(month), int(day), int(hour), int(minute),
                         tzinfo=timezone.utc)
    except (ValueError, IndexError):
        return None
    return local - timedelta(hours=SITE_UTC_OFFSET_H)


def fetch_fixtures() -> list[dict]:
    """Calendrier du jour : un seul appel, aucun paramètre d'URL."""
    body = _get(FIXTURES_URL)
    if not body:
        return []
    daily_quota.add(QUOTA_BUCKET, 1)
    out = []
    for raw in re.findall(r'A\[\d+\]\s*=\s*"([^"]*)"', body):
        row = raw.split("^")
        if len(row) < 44:
            continue
        home, away = row[7].strip(), row[10].strip()
        when = _kickoff_utc(row)
        if not home or not away or when is None:
            continue
        out.append({"sid": row[0], "league": row[4].strip() or "Unknown",
                    "home": home, "away": away, "kickoff": when})
    log.info("titan007: %d matchs au calendrier", len(out))
    return out


def fetch_odds(sid: str) -> dict:
    """{book: {"1","X","2"}} — cotes ACTUELLES, décimales.

    Champs par enregistrement : [2] book, [3:6] ouverture, [10:13] actuelle,
    [16] payout %. On prend l'actuelle : l'ouverture ne sert qu'au CLV.
    """
    body = _get(ODDS_URL.format(sid=sid))
    if not body:
        return {}
    daily_quota.add(QUOTA_BUCKET, 1)
    block = re.search(r'var\s+game\s*=\s*Array\((.*?)\);\s*\n', body, re.S)
    if not block:
        return {}
    out: dict = {}
    for rec in re.findall(r'"([^"]*)"', block.group(1)):
        parts = rec.split("|")
        if len(parts) < 17:
            continue
        o1, ox, o2 = (_odd(parts[10]), _odd(parts[11]), _odd(parts[12]))
        if o1 and o2:
            out[parts[2].strip()] = {"1": o1, "X": ox, "2": o2}
    return out


# Une cote soft ne peut pas dépasser la MÉDIANE des books soft de plus que
# ce facteur. Sans ce plafond, le line shopping sur 157 books ramasse
# systématiquement le book figé : mesuré le 2026-08-20, un match colombien
# ressortait à 4,59 côté soft contre 3,58 sharp — 28 % d'écart, soit un edge
# énorme et entièrement faux. La médiane est insensible à ces valeurs
# aberrantes, là où le maximum les cherche.
MAX_SOFT_OUTLIER = float(os.environ.get("TITAN007_MAX_OUTLIER", "1.10"))


def _median(values: list[float]) -> float:
    vals = sorted(values)
    n = len(vals)
    if not n:
        return 0.0
    mid = n // 2
    return vals[mid] if n % 2 else (vals[mid - 1] + vals[mid]) / 2


def _sharp_price(books: dict) -> dict | None:
    """Le premier book sharp trouvé, dans l'ordre de préférence : on veut CE
    prix de référence, pas le plus généreux."""
    for name in SHARP_BOOKS:
        for book, odds in books.items():
            if name in book.lower():
                return dict(odds)
    return None


def _soft_price(books: dict) -> dict | None:
    """Line shopping BORNÉ : le meilleur prix qui reste crédible face à la
    médiane des books soft. Renvoie None si moins de trois books cotent —
    sans médiane fiable, mieux vaut pas de prix qu'un prix douteux."""
    quotes = [odds for book, odds in books.items()
              if any(n in book.lower() for n in SOFT_BOOKS)]
    if len(quotes) < 3:
        return None
    out: dict = {}
    for k in ("1", "X", "2"):
        vals = [q[k] for q in quotes if q.get(k, 0) > 1.01]
        if not vals:
            out[k] = 0.0
            continue
        ceiling = _median(vals) * MAX_SOFT_OUTLIER
        keep = [v for v in vals if v <= ceiling]
        out[k] = max(keep) if keep else _median(vals)
    return out if out.get("1") and out.get("2") else None


def fetch_matches(hours_ahead: int = 24, max_matches: int | None = None) -> list[dict]:
    """
    Matchs à venir avec prix soft ET sharp, dans la forme du harvester.

    Coût : 1 requête de calendrier + 1 par match retenu, plafonné par
    `max_matches` et par le budget journalier partagé. Rend [] sur toute
    panne — source best-effort, jamais une dépendance dure.
    """
    spent = daily_quota.spent(QUOTA_BUCKET)
    if spent >= DAILY_BUDGET:
        log.warning("titan007: budget journalier atteint (%d/%d) — cycle ignoré",
                    spent, DAILY_BUDGET)
        return []

    now   = datetime.now(timezone.utc)
    until = now + timedelta(hours=hours_ahead)
    cap   = max_matches or MAX_MATCHES

    upcoming = [f for f in fetch_fixtures() if now < f["kickoff"] <= until]
    upcoming.sort(key=lambda f: f["kickoff"])
    if not upcoming:
        log.info("titan007: 0 match dans les %dh", hours_ahead)
        return []

    matches: list[dict] = []
    n_sharp = 0
    for i, fx in enumerate(upcoming[:cap]):
        if daily_quota.spent(QUOTA_BUCKET) >= DAILY_BUDGET:
            log.warning("titan007: budget épuisé en cours de cycle — %d matchs conservés",
                        len(matches))
            break
        if i:
            time.sleep(REQUEST_DELAY)      # cadence volontairement basse
        books = fetch_odds(fx["sid"])
        if not books:
            continue
        soft  = _soft_price(books)
        sharp = _sharp_price(books)
        if not soft and not sharp:
            continue
        m = {
            "id":            f"t7_{fx['sid']}",
            "match":         f"{fx['home']} vs {fx['away']}",
            "home":          fx["home"],
            "away":          fx["away"],
            "league":        fx["league"],
            "sport":         SPORT,
            "sport_id":      SPORT_ID,
            "commence_time": fx["kickoff"].strftime("%Y-%m-%dT%H:%M:%SZ"),
            # Sans book soft, le sharp sert de soft : edge nul par
            # construction, donc jamais de faux signal.
            "odds_1xbet":    soft or sharp,
            "_soft_source":  "titan007",
        }
        if sharp:
            m["odds_pinnacle"] = sharp
            n_sharp += 1
        matches.append(m)

    log.info("titan007: %d matchs (%d avec prix sharp) / %d à venir | %d req aujourd'hui",
             len(matches), n_sharp, len(upcoming), daily_quota.spent(QUOTA_BUCKET))
    return matches


def probe() -> tuple[bool, str]:
    """(joignable ?, détail) — pour scripts/ops.py sources."""
    body = _get(FIXTURES_URL)
    if not body:
        return False, "injoignable"
    n = len(re.findall(r'A\[\d+\]\s*=\s*"', body))
    return (n > 0), f"{n} matchs au calendrier | {daily_quota.spent(QUOTA_BUCKET)}/{DAILY_BUDGET} req aujourd'hui"
