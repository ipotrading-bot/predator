"""
core/harvester.py — Tier 2 : sources soft gratuites, line shopping
Soft source : api-sports + odds-api.io + titan007 (+ odds500 en mode ombre)
Sharp source: Pinnacle via api-sports, exchange (Matchbook/Betfair)
All timestamps : UTC/GMT.

2026-09-03 : le LineFeed 1xbet/Melbet/22bet est RETIRÉ (décision opérateur :
« si une source est inutilisable, il faut la dégager »). Bloqué par IP depuis
les runners GitHub depuis août (HTTP 203 / 404 sur chaque URL, mesuré encore
le 2026-09-03 19:44), il coûtait pourtant à chaque scan 9 requêtes × 2-5 s de
sommeil PAR SPORT — ~36 s de budget moteur pour rien, quatre fois par scan.
Les books soft 1xbet/Bet365 arrivent par odds-api.io (authentifié, non
filtré par IP) ; la clé `odds_1xbet` garde son nom historique.

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
import requests
from datetime import datetime, timedelta, timezone

from core.odds_api_io import SPORTS as _OAI_SPORTS, fetch_sport as _oai_fetch_sport
from core.titan007 import SPORT_ID as _T7_SPORT_ID, fetch_matches as _t7_fetch
from core.paim_engine import strict_team_match

# ── UTC sub-logger (inherits handler from PREDATOR root) ─────────────
log = logging.getLogger("PREDATOR.harvester")

SPORT_IDS = {1: "soccer", 3: "tennis", 4: "basketball", 5: "mma"}

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


def _fuzzy_match_event(candidate: dict, pool: list[dict]) -> dict | None:
    """Find `candidate`'s counterpart in `pool` by team-name fuzzy match
    (core.paim_engine.strict_team_match) — used to line up the same
    real-world match across different soft books before comparing prices."""
    for other in pool:
        if strict_team_match(candidate["home"], other["home"]) and \
           strict_team_match(candidate["away"], other["away"]):
            return other
    return None


# sport_id harvester -> nom de sport odds-api.io (core/odds_api_io.py).
_ODDS_API_IO_BY_ID = {cfg[1]: name for name, cfg in _OAI_SPORTS.items()}


def _fetch_from_odds_api_io(sport_id: int) -> list:
    """odds-api.io — accès AUTHENTIFIÉ aux books soft (1xbet & co.), donc
    non filtré par IP — c'est par ici que 1xbet/Bet365 arrivent depuis le
    retrait du LineFeed direct. Fournit h2h + spreads + totals, et un prix
    sharp si un exchange fait partie des books sélectionnés."""
    sport = _ODDS_API_IO_BY_ID.get(sport_id)
    if not sport:
        return []
    return _oai_fetch_sport(sport)


def _fetch_multi_book(sport_id: int) -> list:
    """
    Task 6 — line shopping: fetch every configured soft source (odds-api.io,
    titan007, odds500) for this sport and, for each real-world
    match found on 2+ of them, keep
    the BEST (highest) price per outcome across all of them — not just
    whichever book happened to respond first. `_soft_source` on the
    returned match records which book contributed each surviving price
    (or a "+" joined list when outcomes came from different books), for
    display/debugging attribution.

    Falls back gracefully: if only one book responds, its prices are used
    as-is (identical behavior to the old single-book fetch).
    """
    per_book: dict[str, list] = {}
    # (LineFeed 1xbet/Melbet/22bet et api-sports retirés le 2026-09-03 —
    # voir l'en-tête ; api-sports : deux comptes gratuits suspendus.)
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
            # Pinnacle qu'une autre source apportait, c'est-à-dire le signal.
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
    price per outcome across every soft source (Task 6). Returns combined list.

    Un sport dont aucun book ne rend rien reste VIDE : le repli « demande à
    un LLM d'inventer le slate et ses cotes » a été supprimé le 2026-09-02
    avec Groq/Tavily. Rien trouvé veut dire rien, pas « demande à un modèle »."""
    all_matches = []
    # L'union des sports que les sources soft savent servir — dérivée de
    # leurs propres tables, jamais recopiée ici. Le foot (titan007) est
    # dans les deux premières de toute façon.
    ids = set(_ODDS_API_IO_BY_ID) | {_T7_SPORT_ID}
    for sport_id in sorted(ids):
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
