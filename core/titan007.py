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

CRITÈRE DE RETRAIT (règle 13, posé le 2026-09-07)
-------------------------------------------------
Budget : `DAILY_BUDGET` (500/j), compteur partagé `daily_quota`, lu dans la
ligne de bilan « titan007: N matchs (M avec prix sharp) / K à venir |
E sans cotes (404/vide) sur D demandées | R req aujourd'hui » de chaque run
`Scan standard`. Relevé de référence : samedi 2026-09-06, 750 matchs au
calendrier, 40/40 avec prix sharp et 0 fichier en 404 sur 7 cycles ; lundi
2026-09-07, ~255 au calendrier, 24-33 matchs (20-30 sharp) et 3-10 fichiers
en 404 sur 40 demandés — le cap de 40 atteint alors des matchs dont le
fichier `1x2d` n'existe pas encore. Un 404 sur un calendrier mince est
normal ; un 404 sur un calendrier plein ne l'est pas.

La source SORT du registre (`core/source_adapter.CALL_ORDER`) le jour où,
sur 7 jours consécutifs de runs `Scan standard`, l'une des deux mesures
tient :
  - moins de 10 matchs avec prix sharp par cycle en médiane (elle ne
    porte plus le Tier 2 sud-américain, sa seule raison d'être) ;
  - plus de 50 % des fichiers de cotes demandés sans cotes (404/vide) — le
    feed `1x2d` se ferme ou change de forme, et la tolérance devient une
    intrusion.
Le retrait se fait dans le même commit que sa ligne d'`INCIDENTS.md`, avec
les sept bilans cités ; le gardien
`tests/test_titan007.py::test_le_critere_de_retrait_est_ecrit_et_la_source_est_au_registre`
tombe si la source ou ce paragraphe disparaît seul.
"""
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from core import daily_quota
from core.execution_books import book_canonique, ordre
from core.source_adapter import league_rank

log = logging.getLogger("PREDATOR.titan007")

FIXTURES_URL = "https://bf.titan007.com/vbsxml/bfdata_ut.js"
ODDS_URL     = "http://1x2d.titan007.com/{sid}.js"

# Décalage du calendrier par rapport à UTC — voir le docstring.
SITE_UTC_OFFSET_H = int(os.environ.get("TITAN007_UTC_OFFSET", "8"))

QUOTA_BUCKET  = "titan007"
DAILY_BUDGET  = int(os.environ.get("TITAN007_DAILY_BUDGET", "500"))

# Rythme de dépense (2026-08-27) — titan007 est la SECONDE source qui porte
# réellement un prix sharp (~24-33 matchs par cycle). Elle n'était pas encore
# à sec le soir du relevé (242/500 à 20:03), mais rien ne la protégeait : le
# budget partait premier arrivé, premier servi, comme api-sports et
# odds-api.io qui, eux, tombaient. On la verrouille avant la panne, pas après.
CYCLE_COST    = int(os.environ.get("TITAN007_CYCLE_COST", "35"))
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

# Books SOFT qui forment la MÉDIANE de marché (plafond anti-book figé de
# `_soft_price`). Liste FERMÉE volontairement : le feed en contient 157, dont
# des books obscurs ou figés dont la cote aberrante fausserait la médiane.
# Le PRIX retenu, lui, est celui du seul book d'exécution (2026-09-07).
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
    except urllib.error.HTTPError as e:
        # Un 404 sur un fichier de cotes = pas encore publié : attendu sur un
        # calendrier plein (mesuré 18/40 par scan le 2026-09-10), donc INFO ;
        # la mémoire `_sans_cotes_*` évite de le redemander. Tout autre code
        # reste un avertissement. L'URL entière (sans query string, par
        # construction) : le critère de retrait demande de savoir si les 404
        # frappent toujours les mêmes sid.
        (log.info if e.code == 404 else log.warning)("titan007: %s — %s", url, e)
        return None
    except Exception as e:
        log.warning("titan007: %s — %s", url, e)
        return None


# ── Mémoire des fichiers de cotes absents (2026-09-10) ────────────────
# Mesuré à l'audit : 18 des 40 fichiers `1x2d` demandés répondaient 404 à
# CHAQUE scan standard (122 avertissements en 3 jours), et le cap de 40
# s'épuisait sur des matchs sans cotes pendant que d'autres, plus loin dans
# le calendrier, en avaient. Un 404 = fichier pas encore publié ; il peut
# apparaître plus tard dans la journée, d'où une mémoire à durée limitée
# (SANS_COTES_TTL_H) partagée entre runs via `meta`, et non « pour la
# journée ». Sans base (tests, panne) : mémoire vide, comportement d'avant.
SANS_COTES_KEY = "titan007_sans_cotes"
SANS_COTES_TTL_H = float(os.environ.get("TITAN007_SANS_COTES_TTL_H", "6"))


def _sans_cotes_lire() -> dict:
    sb = daily_quota._db()
    if sb is None:
        return {}
    try:
        row = sb.table("meta").select("value").eq("key", SANS_COTES_KEY).maybe_single().execute()
        data = json.loads((row.data or {}).get("value") or "{}") if row and row.data else {}
        return data if isinstance(data, dict) else {}
    except Exception as e:                                       # noqa: BLE001
        log.debug("titan007: mémoire sans-cotes illisible (%s)", e)
        return {}


def _sans_cotes_ecrire(memo: dict) -> None:
    sb = daily_quota._db()
    if sb is None:
        return
    try:
        now = datetime.now(timezone.utc).isoformat()
        sb.table("meta").upsert({"key": SANS_COTES_KEY, "value": json.dumps(memo),
                                 "updated_at": now}, on_conflict="key").execute()
    except Exception as e:                                       # noqa: BLE001
        log.debug("titan007: mémoire sans-cotes non écrite (%s)", e)


def _sans_cotes_frais(memo: dict, sid: str, now: datetime) -> bool:
    """Ce sid a-t-il répondu « sans cotes » il y a moins de SANS_COTES_TTL_H ?"""
    ts = memo.get(str(sid))
    if not ts:
        return False
    try:
        return (now - datetime.fromisoformat(ts)).total_seconds() < SANS_COTES_TTL_H * 3600
    except (TypeError, ValueError):
        return False


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
    """Le prix du book d'EXÉCUTION, borné par la médiane des books soft.

    Jusqu'au 2026-09-07 c'était un line shopping sur `SOFT_BOOKS` : le
    meilleur prix crédible, chez n'importe lequel d'entre eux. L'opérateur
    ne mise que chez `EXECUTION_BOOKS` (décision opérateur, règle 11) : un
    prix Bet365 ou Marathonbet n'est pas exécutable, donc pas un edge. Le
    plafond `MAX_SOFT_OUTLIER` reste : il protège du book FIGÉ, et le book
    d'exécution peut l'être dans ce feed comme un autre. Renvoie None sans
    prix du book d'exécution, ou si moins de trois books soft cotent — sans
    médiane fiable, mieux vaut pas de prix qu'un prix douteux."""
    par_book = _soft_prices(books)
    if not par_book:
        return None
    return par_book[min(par_book, key=ordre)]


def _soft_prices(books: dict) -> dict[str, dict]:
    """Un bloc 1X2 par book d'EXÉCUTION présent dans le feed (2026-09-08 :
    plusieurs books, `core.constants.EXECUTION_BOOKS`), chacun borné par la
    médiane des books soft — le plafond anti-book-figé s'applique book par
    book, et un book figé est simplement absent du résultat. Jamais un
    maximum par issue entre books : le moteur départage bloc contre bloc,
    sur le prix final (core/execution_books.choisir_bloc_h2h)."""
    quotes = [odds for book, odds in books.items()
              if any(n in book.lower() for n in SOFT_BOOKS)]
    if len(quotes) < 3:
        return {}
    medianes = {}
    for k in ("1", "X", "2"):
        vals = [q[k] for q in quotes if q.get(k, 0) > 1.01]
        medianes[k] = _median(vals) if vals else None
    par_book: dict[str, dict] = {}
    for book, execution in books.items():
        canon = book_canonique(book)
        if canon is None or canon in par_book:
            continue
        out: dict = {}
        for k in ("1", "X", "2"):
            mine = float(execution.get(k, 0) or 0)
            med = medianes[k]
            if med is None or mine <= 1.01 or mine > med * MAX_SOFT_OUTLIER:
                out[k] = 0.0             # absent, ou figé au-dessus du marché
                continue
            out[k] = mine
        if out.get("1") and out.get("2"):
            par_book[canon] = out
    return par_book


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
    ouverture = daily_quota.paced_allowance(DAILY_BUDGET, CYCLE_COST)
    if spent >= ouverture:
        log.info("titan007: rythme de dépense — %d/%d dépensées, l'ouverture "
                 "de cette heure est %d. Le reste est gardé pour les scans du "
                 "soir.", spent, DAILY_BUDGET, ouverture)
        return []

    now   = datetime.now(timezone.utc)
    until = now + timedelta(hours=hours_ahead)
    cap   = max_matches or MAX_MATCHES

    upcoming = [f for f in fetch_fixtures() if now < f["kickoff"] <= until]
    # Priorité de ligue AVANT l'heure. Mesuré le 2026-08-28 (vendredi, 24 h) :
    # 238 matchs au calendrier, positions 0-57 toutes à 17:45-18:00 (Eerste
    # Divisie, U21, Welsh PR, Pologne D3…) — Bayern–Stuttgart (18:30) en
    # position 62, hors du cap de 40. Le scan ne voyait que des divisions
    # mineures et sortait à EV −3 à −9 % sur toutes. Le tri par heure
    # servait la masse ; celui-ci sert d'abord ce qui a un prix sharp liquide,
    # puis le reste dans l'ordre d'avant. Zéro requête de plus.
    upcoming.sort(key=lambda f: (league_rank(f["league"]), f["kickoff"]))
    if not upcoming:
        log.info("titan007: 0 match dans les %dh", hours_ahead)
        return []

    matches: list[dict] = []
    n_sharp = 0
    n_asked = 0          # fichiers de cotes demandés
    n_unreachable = 0    # … dont sans cotes (404, timeout, fichier vide) — CRITÈRE DE RETRAIT
    n_sautes = 0         # sid connus sans cotes depuis < SANS_COTES_TTL_H, pas redemandés
    now = datetime.now(timezone.utc)
    memo = _sans_cotes_lire()
    for fx in upcoming:
        if n_asked >= cap:
            break
        if _sans_cotes_frais(memo, fx["sid"], now):
            n_sautes += 1
            continue
        if daily_quota.spent(QUOTA_BUCKET) >= DAILY_BUDGET:
            log.warning("titan007: budget épuisé en cours de cycle — %d matchs conservés",
                        len(matches))
            break
        if n_asked:
            time.sleep(REQUEST_DELAY)      # cadence volontairement basse
        n_asked += 1
        books = fetch_odds(fx["sid"])
        if not books:
            n_unreachable += 1
            memo[str(fx["sid"])] = now.isoformat()
            continue
        memo.pop(str(fx["sid"]), None)
        par_book = _soft_prices(books)
        soft  = par_book[min(par_book, key=ordre)] if par_book else None
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
        if par_book:
            m["h2h_par_book"] = par_book
        if sharp:
            m["odds_pinnacle"] = sharp
            n_sharp += 1
        matches.append(m)

    # Les deux compteurs du critère de retrait sont sur CETTE ligne, pour que
    # la mesure se fasse par grep sur les logs des runs « Scan standard ».
    log.info("titan007: %d matchs (%d avec prix sharp) / %d à venir | "
             "%d sans cotes (404/vide) sur %d demandées | %d req aujourd'hui | "
             "%d sautés (sans cotes il y a < %.0fh)",
             len(matches), n_sharp, len(upcoming), n_unreachable, n_asked,
             daily_quota.spent(QUOTA_BUCKET), n_sautes, SANS_COTES_TTL_H)
    _sans_cotes_ecrire({s: t for s, t in memo.items() if _sans_cotes_frais(memo, s, now)})
    return matches


def probe() -> tuple[bool, str]:
    """(joignable ?, détail) — pour scripts/ops.py sources."""
    body = _get(FIXTURES_URL)
    if not body:
        return False, "injoignable"
    n = len(re.findall(r'A\[\d+\]\s*=\s*"', body))
    return (n > 0), f"{n} matchs au calendrier | {daily_quota.spent(QUOTA_BUCKET)}/{DAILY_BUDGET} req aujourd'hui"
