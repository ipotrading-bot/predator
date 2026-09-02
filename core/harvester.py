"""
core/harvester.py — PAIM v7.5 — Guerrilla Mode
Soft source : 1XBet direct feed (JSON) + api-sports + odds-api.io + titan007
Sharp source: Pinnacle via api-sports, exchange (Matchbook/Betfair)
Sports: 1=Soccer, 3=Tennis, 4=Basketball
All timestamps : UTC/GMT.

2026-09-02 : la recherche web (Groq compound-mini + Tavily) est SUPPRIMÉE du
harvest — décision opérateur, avec le retrait de Groq/Tavily de tout le
pipeline. Trois fonctions sont parties avec elle : `_fetch_from_gemini`
(demandait à un LLM d'« estimer des cotes 1XBet réalistes » sur des matchs
qu'il pouvait halluciner), `fetch_pinnacle_prices` (recherche groupée d'une
« cote Pinnacle » par LLM — le chemin dominant du prix sharp GÉNÉRÉ, celui
que l'en-tête de l'ancien core/oracle.py désignait comme hors de portée de
MAX_ORACLE) et `fetch_estimated_prices` (cotes de mémoire d'entraînement).
Toutes trois fabriquaient des prix qu'aucun book n'a affichés : une cote
sous-estimée fabrique un edge, et ces signaux-là sont précisément ceux que
le moteur émet (cf. A6). Un match sans prix sharp RÉEL est écarté, point.
"""
import hashlib
import logging
import os
import time
import random
import requests
from datetime import datetime, timedelta, timezone

from core.api_sports import PROVIDERS as _AS_PROVIDERS, fetch_sport as _as_fetch_sport
from core.odds_api_io import SPORTS as _OAI_SPORTS, fetch_sport as _oai_fetch_sport
from core.titan007 import SPORT_ID as _T7_SPORT_ID, fetch_matches as _t7_fetch
from core.paim_engine import strict_team_match

# ── UTC sub-logger (inherits handler from PREDATOR root) ─────────────
log = logging.getLogger("PREDATOR.harvester")

SPORT_IDS = {1: "soccer", 3: "tennis", 4: "basketball", 5: "mma"}

XBET_FEED_TPLS = [
    "https://1xbet.com/LineFeed/Get1x2?sport={sport_id}&count=50&lng=en&mode=4&partner=157",
    "https://1xbet.com/LineFeed/Get1x2?sport={sport_id}&count=50&lng=en&mode=4",
    "https://1xbet.com/LineFeed/Get1x2?sport={sport_id}&count=50&lng=en&mode=4&country=255",
    "https://1xbet.com/LineFeed/Get1x2?sport={sport_id}&count=50&lng=en&mode=1&partner=157",
    "https://1xbet.com/LineFeed/Get1x2?sport={sport_id}&count=50&lng=en&mode=1",
    "https://1xbet.cm/LineFeed/Get1x2?sport={sport_id}&count=50&lng=en&mode=4",
]

# Task 6 — additional soft books for line shopping. Melbet/22bet are widely
# documented as running the same LineFeed backend as 1xbet (same platform
# family, near-identical site/app), so the endpoint SHAPE below mirrors
# XBET_FEED_TPLS exactly — but the exact URL/partner id for each was NOT
# live-verified from this sandbox: outbound requests to 1xbet.com itself
# get Cloudflare-redirected to /en/block from this environment's IP (bot/geo
# gate), so even the already-working 1xbet integration can't be exercised
# here, let alone a brand-new one. _fetch_from_book() below degrades to []
# on any failure (same as the pre-existing 1xbet behavior), so a wrong URL
# here just means that book contributes nothing — confirm these actually
# return data (check the Predator Engine GitHub Actions logs for
# "Melbet <sport> OK" / "22bet <sport> OK" lines) before relying on them.
MELBET_FEED_TPLS = [
    "https://melbet.com/LineFeed/Get1x2?sport={sport_id}&count=50&lng=en&mode=4&partner=169",
    "https://melbet.com/LineFeed/Get1x2?sport={sport_id}&count=50&lng=en&mode=4",
]
BET22_FEED_TPLS = [
    "https://22bet.com/LineFeed/Get1x2?sport={sport_id}&count=50&lng=en&mode=4",
]

# name -> (url templates, referer) — extend this dict to add more books;
# _fetch_multi_book() below iterates it generically.
SOFT_BOOKS = {
    "1xbet":  (XBET_FEED_TPLS,  "https://1xbet.com/en/line/"),
    "melbet": (MELBET_FEED_TPLS, "https://melbet.com/en/line/"),
    "22bet":  (BET22_FEED_TPLS,  "https://22bet.com/en/line/"),
}
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://1xbet.com/en/line/",
}
def _odd(val):
    try:
        f = float(val)
        return f if f > 1.01 else 0.0
    except (TypeError, ValueError):
        return 0.0


def _stable_id(prefix: str, home: str, away: str, when: str = "") -> str:
    """ID déterministe : même match réel → même id à chaque scan.

    L'ancien schéma `f"gemini_{sport}_{i}"` utilisait l'index de position dans
    le JSON renvoyé par l'IA. L'ordre variant d'un scan à l'autre, le même
    match changeait d'id à chaque tick — run_engine._save() dédoublonne par
    (match_id, market_key), son delete ne trouvait donc jamais la version
    précédente et les copies s'empilaient en base. Pire : deux matchs
    différents pouvaient se partager un id d'un scan à l'autre, et le delete
    frappait alors le signal d'un autre match.
    """
    raw = f"{prefix}|{home.lower().strip()}|{away.lower().strip()}|{(when or '')[:10]}"
    return f"ai_{prefix}_" + hashlib.sha1(raw.encode()).hexdigest()[:12]


def _parse_xbet_json(data, sport_id):
    sport_name = SPORT_IDS.get(sport_id, "unknown")
    matches = []
    for event in data.get("Value", []):
        try:
            home = str(event.get("O1", "")).strip()
            away = str(event.get("O2", "")).strip()
            if not home or not away:
                continue
            o1 = _odd(event.get("C1"))
            ox = _odd(event.get("C2"))
            o2 = _odd(event.get("C3"))
            if o1 == 0.0 and o2 == 0.0:
                continue
            matches.append({
                "id":         str(event.get("CI", f"{home}_{away}")),
                "match":      f"{home} vs {away}",
                "home":       home,
                "away":       away,
                "league":     str(event.get("L", "Unknown")),
                "sport":      sport_name,
                "sport_id":   sport_id,
                "odds_1xbet": {"1": o1, "X": ox, "2": o2},
            })
        except Exception:
            continue
    return matches


def _fetch_from_book(book: str, url_templates: list, referer: str, sport_id: int) -> list:
    """Try each URL variant for one soft book with a small random delay.
    Returns list of matches (odds keyed "odds_1xbet" regardless of book —
    see _fetch_multi_book for why) or [] on total failure. Generalized
    from the original 1xbet-only fetch (Task 6) so the same retry/parse
    logic covers every book in SOFT_BOOKS without duplication."""
    sport_name = SPORT_IDS.get(sport_id, str(sport_id))
    headers = {**HEADERS, "Referer": referer}
    for tpl in url_templates:
        url = tpl.format(sport_id=sport_id)
        try:
            time.sleep(random.uniform(2, 5))
            r = requests.get(url, headers=headers, timeout=15)
            if r.status_code == 200:
                data = r.json()
                matches = _parse_xbet_json(data, sport_id)
                if matches:
                    log.info("%s %s OK: %d matches via %s", book, sport_name, len(matches), url.split("?")[0])
                    return matches
                log.info("%s %s: HTTP 200 mais 0 match exploitable via %s", book, sport_name, url.split("?")[0])
            else:
                # Un 403/429/5xx silencieux a caché pendant dix jours (août
                # 2026) que le LineFeed bloquait les runners GitHub — on le dit.
                log.warning("%s %s: HTTP %d via %s", book, sport_name, r.status_code, url.split("?")[0])
        except Exception as e:
            log.warning("%s %s fail (%s): %s", book, sport_name, url.split("?")[0], e)
    return []


def _fetch_from_1xbet(sport_id):
    """Back-compat wrapper — 1xbet alone, no line shopping. Prefer
    _fetch_multi_book() for the best-price-across-books behavior."""
    tpls, referer = SOFT_BOOKS["1xbet"]
    return _fetch_from_book("1xbet", tpls, referer, sport_id)


def _fuzzy_match_event(candidate: dict, pool: list[dict]) -> dict | None:
    """Find `candidate`'s counterpart in `pool` by team-name fuzzy match
    (core.paim_engine.strict_team_match) — used to line up the same
    real-world match across different soft books before comparing prices."""
    for other in pool:
        if strict_team_match(candidate["home"], other["home"]) and \
           strict_team_match(candidate["away"], other["away"]):
            return other
    return None


# sport_id harvester -> nom de sport api-sports (core/api_sports.py).
# Le foot (1) et le basket (4) recoupent le LineFeed ; le baseball (6) et le
# hockey (7) n'existent QUE par cette voie côté Tier 2, alors qu'ils sont
# scannés en Tier 1 et appris par la couche d'apprentissage — c'est
# précisément le trou que l'incident d'août 2026 a révélé.
_API_SPORTS_BY_ID = {p["sport_id"]: name for name, p in _AS_PROVIDERS.items()}


def _fetch_from_api_sports(sport_id: int) -> list:
    """Famille api-sports.io — fournisseur indépendant d'OddsAPI ET du
    LineFeed : authentifié par CLÉ, donc non filtré par IP (les runners
    GitHub sont bloqués par 1xbet/ESPN/SofaScore, pas par api-sports —
    vérifié le 2026-08-20). Peut ramener un prix sharp (`odds_pinnacle`)
    dans la même réponse, auquel cas aucune recherche web n'est nécessaire."""
    sport = _API_SPORTS_BY_ID.get(sport_id)
    if not sport:
        return []
    return _as_fetch_sport(sport)


# sport_id harvester -> nom de sport odds-api.io (core/odds_api_io.py).
_ODDS_API_IO_BY_ID = {cfg[1]: name for name, cfg in _OAI_SPORTS.items()}


def _fetch_from_odds_api_io(sport_id: int) -> list:
    """odds-api.io — accès AUTHENTIFIÉ aux books soft (1xbet & co.), donc
    non filtré par IP, là où le LineFeed de ces mêmes books est bloqué
    depuis les runners GitHub. Fournit h2h + spreads + totals, et un prix
    sharp si un exchange fait partie des books sélectionnés."""
    sport = _ODDS_API_IO_BY_ID.get(sport_id)
    if not sport:
        return []
    return _oai_fetch_sport(sport)


def _fetch_multi_book(sport_id: int) -> list:
    """
    Task 6 — line shopping: fetch every configured soft book (SOFT_BOOKS)
    for this sport and, for each real-world match found on 2+ books, keep
    the BEST (highest) price per outcome across all of them — not just
    whichever book happened to respond first. `_soft_source` on the
    returned match records which book contributed each surviving price
    (or a "+" joined list when outcomes came from different books), for
    display/debugging attribution.

    Falls back gracefully: if only one book responds, its prices are used
    as-is (identical behavior to the old single-book fetch).
    """
    per_book: dict[str, list] = {}
    # Le LineFeed ne connaît que les ids de SPORT_IDS ; l'interroger pour un
    # sport qu'il ne couvre pas coûte 3 books x 6 URLs de sleep pour rien.
    if sport_id in SPORT_IDS:
        for book, (tpls, referer) in SOFT_BOOKS.items():
            found = _fetch_from_book(book, tpls, referer, sport_id)
            if found:
                per_book[book] = found

    as_matches = _fetch_from_api_sports(sport_id)
    if as_matches:
        per_book["api_sports"] = as_matches

    oai_matches = _fetch_from_odds_api_io(sport_id)
    if oai_matches:
        per_book["odds_api_io"] = oai_matches

    # Titan007 : foot uniquement, mais c'est la seule source gratuite qui
    # couvre les ligues sud-américaines et secondaires où l'exchange est
    # riche — et elle apporte le prix sharp ET le prix soft d'un coup.
    if sport_id == _T7_SPORT_ID:
        t7_matches = _t7_fetch()
        if t7_matches:
            per_book["titan007"] = t7_matches

    # odds.500.com (mission 3) — EN DERNIER, et ce n'est pas un détail : elle
    # se mesure CONTRE les sources déjà collectées (divergence → scorecard →
    # promotion). Tant qu'elle est en mode ombre, `_fetch_from_odds500` rend
    # [] : ses prix sont enregistrés et comparés, ils ne créent aucun signal.
    # Ses libellés d'équipes sont chinois ; c'est core/free_sources.py qui les
    # résout en noms canoniques et ÉCARTE les matchs qu'il ne sait pas nommer.
    trusted_so_far = [m for ms in per_book.values() for m in ms]
    o500_matches = _fetch_from_odds500(sport_id, trusted_so_far)
    if o500_matches:
        per_book["odds500"] = o500_matches

    if not per_book:
        return []

    books_in_order = list(per_book.keys())
    merged: list[dict] = list(per_book[books_in_order[0]])
    for m in merged:
        m["_soft_source"] = books_in_order[0]

    for book in books_in_order[1:]:
        for cand in per_book[book]:
            existing = _fuzzy_match_event(cand, merged)
            if existing is None:
                cand["_soft_source"] = book
                merged.append(cand)
                continue
            # Same real-world match found on another book — keep the
            # better price per outcome (line shopping), track provenance.
            sources = set(existing["_soft_source"].split("+"))
            # Champs que le line shopping ne doit PAS perdre : un prix sharp
            # ou une heure de coup d'envoi n'existent que chez certaines
            # sources, et l'ancienne fusion ne recopiait que les prix soft —
            # un match trouvé d'abord sur le LineFeed perdait donc le prix
            # Pinnacle qu'api-sports apportait, c'est-à-dire le signal.
            for extra in ("odds_pinnacle", "commence_time", "league",
                          "spreads_1xbet", "totals_1xbet",
                          "spreads_pinnacle", "totals_pinnacle"):
                if cand.get(extra) and not existing.get(extra):
                    existing[extra] = cand[extra]
            improved = False
            for key in ("1", "X", "2"):
                new_odd = cand["odds_1xbet"].get(key, 0.0)
                cur_odd = existing["odds_1xbet"].get(key, 0.0)
                if new_odd > cur_odd:
                    existing["odds_1xbet"][key] = new_odd
                    improved = True
            if improved:
                sources.add(book)
                existing["_soft_source"] = "+".join(sorted(sources))

    # Marchés de prédiction (Kalshi/Polymarket) — rôle CONSENSUS, jamais sharp.
    # Ils ne modifient ni un prix ni un signal : ils confrontent le slate à un
    # avis qui ne recopie aucun bookmaker, et crient quand un « edge » ressemble
    # à un prix périmé. Best-effort, jamais bloquant.
    _measure_consensus(sport_id, merged)

    return merged


def _measure_consensus(sport_id: int, merged: list) -> None:
    """Confronte le slate aux marchés de prédiction — mesure seule.

    Import PARESSEUX et jamais bloquant, comme `_fetch_from_odds500`.
    """
    try:
        from core.free_sources import measure_slate_consensus
        measure_slate_consensus(sport_id, merged)
    except Exception as e:
        log.warning("consensus: %s — ignoré ce cycle", e)


def _fetch_from_odds500(sport_id: int, trusted: list) -> list[dict]:
    """Sources gratuites Asie (mission 3) — best-effort, jamais bloquant.

    Import PARESSEUX : `core/free_sources` tire odds500 + sevenm +
    team_aliases, et un harvester qui ne sert pas le football n'a aucune
    raison de payer ces imports.
    """
    try:
        from core.free_sources import fetch_odds500
        return fetch_odds500(sport_id, trusted)
    except Exception as e:
        log.warning("odds500: %s — ignorée ce cycle", e)
        return []


def fetch_matches():
    """Fetch matches for all configured sports, line-shopping the best
    price per outcome across every book in SOFT_BOOKS (Task 6). Returns
    combined list.

    Un sport dont aucun book ne rend rien reste VIDE : le repli « demande à
    un LLM d'inventer le slate et ses cotes » a été supprimé le 2026-09-02
    avec Groq/Tavily. Rien trouvé veut dire rien, pas « demande à un modèle »."""
    all_matches = []
    for sport_id in SPORT_IDS:
        all_matches.extend(_fetch_multi_book(sport_id))

    # Sports couverts par api-sports mais absents du LineFeed (baseball,
    # hockey).
    extra_ids = (set(_API_SPORTS_BY_ID) | set(_ODDS_API_IO_BY_ID)) - set(SPORT_IDS)
    for sport_id in sorted(extra_ids):
        all_matches.extend(_fetch_multi_book(sport_id))
    return all_matches


# ── Betfair Exchange (Tier 1.5 — sharp prices peer-to-peer) ──────────

_BETFAIR_LOGIN_URL      = "https://identitysso.betfair.com/api/login"
_BETFAIR_CERTLOGIN_URL  = "https://identitysso-cert.betfair.com/api/certlogin"
_BETFAIR_API_URL        = "https://api.betfair.com/exchange/betting/rest/v1.0"
_BETFAIR_COMMISSION     = 0.05   # Standard 5% commission on net winnings

_BETFAIR_EVENT_TYPES: dict[str, str] = {
    "soccer":           "1",
    "tennis":           "2",
    "basketball":       "7522",
    "cricket":          "4",
    "rugby":            "451485",
    "boxing":           "6",
    "mma":              "26420387",
    "hockey":           "7524",
    "americanfootball": "6423",
    "darts":            "3503",
    "baseball":         "7511",
}

_betfair_session: dict = {}


def _betfair_login() -> bool:
    """
    Non-Interactive (bot) login via client-cert mutual TLS — the ONLY login
    method Betfair supports for unattended/automated callers. The old
    identitysso.betfair.com/api/login endpoint is the Interactive method
    (meant for a human completing a browser session) and returns an empty/
    non-JSON body when called headlessly — confirmed live 2026-07-09
    ("Betfair login: Expecting value: line 1 column 1 (char 0)" on every
    scan, 0 markets ever fetched). BETFAIR_CERT/BETFAIR_CERT_KEY hold the
    PEM cert/key content as GitHub secrets; written to temp files here
    because requests' `cert=` param requires filesystem paths, not PEM
    strings. See https://identitysso-cert.betfair.com/api/certlogin docs:
    response uses `sessionToken`/`loginStatus`, NOT `token`/`status` like
    the interactive endpoint.
    """
    username  = os.environ.get("BETFAIR_USERNAME", "")
    password  = os.environ.get("BETFAIR_PASSWORD", "")
    app_key   = os.environ.get("BETFAIR_APP_KEY",  "")
    cert_pem  = os.environ.get("BETFAIR_CERT", "")
    key_pem   = os.environ.get("BETFAIR_CERT_KEY", "")
    if not all([username, password, app_key, cert_pem, key_pem]):
        return False
    import tempfile
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".crt", delete=False) as cf, \
             tempfile.NamedTemporaryFile(mode="w", suffix=".key", delete=False) as kf:
            cf.write(cert_pem)
            kf.write(key_pem)
            cert_path, key_path = cf.name, kf.name
        try:
            r = requests.post(
                _BETFAIR_CERTLOGIN_URL,
                data={"username": username, "password": password},
                headers={
                    "Content-Type":  "application/x-www-form-urlencoded",
                    "Accept":        "application/json",
                    "X-Application": app_key,
                },
                cert=(cert_path, key_path),
                timeout=15,
            )
        finally:
            os.unlink(cert_path)
            os.unlink(key_path)
        data = r.json()
        if data.get("loginStatus") == "SUCCESS":
            _betfair_session["token"]   = data["sessionToken"]
            _betfair_session["app_key"] = app_key
            log.info("Betfair: session ouverte (cert login)")
            return True
        log.warning("Betfair login: %s", data.get("loginStatus", "FAILED"))
        return False
    except Exception as e:
        log.error("Betfair login: %s", e)
        return False


def _bf_request(endpoint: str, body: dict):
    token   = _betfair_session.get("token",   "")
    app_key = _betfair_session.get("app_key", "")
    if not token:
        return None
    try:
        r = requests.post(
            f"{_BETFAIR_API_URL}/{endpoint}/",
            json=body,
            headers={
                "X-Authentication": token,
                "X-Application":    app_key,
                "Content-Type":     "application/json",
                "Accept":           "application/json",
            },
            timeout=20,
        )
        return r.json() if r.status_code == 200 else None
    except Exception as e:
        log.error("Betfair %s: %s", endpoint, e)
        return None


def fetch_betfair_prices(sports: list = None, hours_ahead: int = 48) -> dict:
    """
    Betfair Exchange Tier 1.5 — back prices for Win/MATCH_ODDS markets.
    Returns {norm_key: {"match": str, "home": str, "away": str,
                        "1": float, "X": float, "2": float}}
    norm_key = "home_lower_away_lower" for fuzzy lookup.
    Prices are commission-adjusted (×0.95 on profit) — comparable to Pinnacle closing lines.
    Returns {} when BETFAIR_APP_KEY is not set or login fails.
    """
    if not os.environ.get("BETFAIR_APP_KEY"):
        return {}
    if not _betfair_session.get("token"):
        if not _betfair_login():
            return {}

    if sports is None:
        sports = ["soccer", "tennis", "basketball", "hockey", "mma", "cricket"]

    event_type_ids = [_BETFAIR_EVENT_TYPES[s] for s in sports if s in _BETFAIR_EVENT_TYPES]
    if not event_type_ids:
        return {}

    now   = datetime.now(timezone.utc)
    until = now + timedelta(hours=hours_ahead)

    catalogue = _bf_request("listMarketCatalogue", {
        "filter": {
            "eventTypeIds":    event_type_ids,
            "marketTypeCodes": ["MATCH_ODDS"],
            "marketStartTime": {
                "from": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "to":   until.strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
            "inPlayOnly": False,
        },
        "maxResults":       "50",
        "marketProjection": ["RUNNER_DESCRIPTION", "EVENT"],
        "sort":             "FIRST_TO_START",
    })

    if not catalogue:
        log.info("Betfair: 0 marchés retournés")
        return {}

    market_ids = [m["marketId"] for m in catalogue][:25]
    books = _bf_request("listMarketBook", {
        "marketIds":       market_ids,
        "priceProjection": {
            "priceData":             ["EX_BEST_OFFERS"],
            "exBestOffersOverrides": {"bestPricesDepth": 1},
        },
    })

    if not books:
        return {}

    cat_map = {m["marketId"]: m for m in catalogue}
    result: dict = {}

    for book in books:
        try:
            mid      = book["marketId"]
            cat_desc = cat_map.get(mid, {}).get("runners", [])
            names    = {r["selectionId"]: r.get("runnerName", "?") for r in cat_desc}

            prices: dict[str, float] = {}
            for runner in book.get("runners", []):
                sid   = runner["selectionId"]
                rname = names.get(sid, "?")
                backs = runner.get("ex", {}).get("availableToBack", [])
                if backs and float(backs[0].get("price", 0)) > 1.01:
                    raw = float(backs[0]["price"])
                    # Commission-adjust so price is net of 5% Betfair fee
                    prices[rname] = round(1 + (raw - 1) * (1 - _BETFAIR_COMMISSION), 4)

            if len(prices) < 2:
                continue

            draw_price = next((p for n, p in prices.items() if "draw" in n.lower()), 0.0)
            teams      = [(n, p) for n, p in prices.items() if "draw" not in n.lower()]
            if len(teams) < 2:
                continue

            home_name, home_p = teams[0]
            away_name, away_p = teams[1]
            norm_key = f"{home_name.lower().strip()}_{away_name.lower().strip()}"

            result[norm_key] = {
                "match": f"{home_name} vs {away_name}",
                "home":  home_name,
                "away":  away_name,
                "1":     home_p,
                "X":     draw_price,
                "2":     away_p,
            }
        except Exception:
            continue

    log.info("Betfair: %d marchés avec prix (commission -5%%)", len(result))
    return result
