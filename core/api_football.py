"""
core/api_football.py — PAIM — API-Football (api-sports.io), source foot
additionnelle et INDÉPENDANTE de The Odds API — fournisseur différent,
donc pas exposée à la même panne de compte/quota (voir l'incident du
2026-07-12 où ODDS_API_KEY est resté hors service plusieurs jours).

S'insère dans core/harvester.py::_fetch_multi_book() comme un book de plus,
au même titre que 1xbet/Melbet/22bet — même forme de sortie (match/home/
away/league/sport/sport_id/odds_1xbet). "odds_1xbet" est le nom générique
du champ "prix soft" utilisé dans tout le pipeline, pas spécifique au
bookmaker 1xbet.

Plan gratuit : 100 req/jour. Chaque cycle coûte 1 requête /fixtures + 1
requête /odds par fixture retenue (LIMIT_FIXTURES plafonne ce coût). Sans
clé, retourne [] silencieusement (même contrat que core/odds_api.fetch_odds).

⚠️ Non vérifié en conditions réelles : ni clé ni documentation officielle
n'étaient accessibles depuis ce sandbox au moment d'écrire ce module
(api-football.com bloque les fetchs automatisés en 403/Cloudflare). La
structure de réponse ci-dessous (fixtures[].fixture/teams/league,
odds[].bookmakers[].bets[].values[]) vient de la doc publique connue et de
recherches web, PAS d'un appel réel testé — à confirmer via les logs du
premier run une fois API_FOOTBALL_KEY configurée (cherche
"API-Football:" dans les logs Actions).
"""
import logging
import os
from datetime import datetime, timedelta, timezone

import requests

log = logging.getLogger("PREDATOR.api_football")

BASE_URL = "https://v3.football.api-sports.io"

# Budget plan gratuit (100 req/jour). Golden Hour tourne 48x/jour, mais
# _fetch_multi_book() n'est atteint côté run_engine.py que quand Tier 1
# (OddsAPI) est vide — voir run_engine.py Tier 2. LIMIT_FIXTURES garde
# chaque cycle à 1 + LIMIT_FIXTURES requêtes pour rester sous le plafond
# même si plusieurs cycles Tier 2 se déclenchent le même jour.
LIMIT_FIXTURES = 5

# Guard de sécurité : sous ce seuil de requêtes restantes, on abandonne le
# cycle plutôt que de risquer un HTTP 429 en plein milieu (même logique que
# le quota_remaining < 50 de core/odds_api.py, adapté à un plafond 25x plus petit).
_QUOTA_GUARD_THRESHOLD = 10

_MATCH_WINNER_NAMES = {"match winner", "home/away", "1x2", "full time result"}


def _headers(api_key: str) -> dict:
    return {"x-apisports-key": api_key}


def _odd(val) -> float:
    try:
        f = float(val)
        return f if f > 1.01 else 0.0
    except (TypeError, ValueError):
        return 0.0


def _extract_1x2(bookmakers: list) -> dict | None:
    """Meilleure cote 1X2 tous bookmakers du plan confondus (line shopping
    simple — cohérent avec le merge multi-book de harvester.py)."""
    best: dict = {}
    for bk in bookmakers or []:
        for bet in bk.get("bets", []) or []:
            name = str(bet.get("name", "")).strip().lower()
            if name not in _MATCH_WINNER_NAMES:
                continue
            values = {}
            for v in bet.get("values", []) or []:
                label = str(v.get("value", "")).strip().lower()
                values[label] = _odd(v.get("odd"))
            o1 = values.get("home")
            ox = values.get("draw", 0.0)
            o2 = values.get("away")
            if o1 and o2 and o1 > best.get("1", 0):
                best = {"1": o1, "X": ox or 0.0, "2": o2}
    return best or None


def fetch_football_matches(api_key: str | None = None, hours_ahead: int = 24) -> list[dict]:
    """
    Fixtures foot + cotes 1X2 des prochaines `hours_ahead` heures. Sortie
    dans la même forme que core/harvester.py::_parse_xbet_json, pour
    s'insérer directement dans _fetch_multi_book(). Returns [] si clé
    absente, quota bas, ou erreur — jamais d'exception vers l'appelant.
    """
    if not api_key:
        api_key = os.environ.get("API_FOOTBALL_KEY")
    if not api_key:
        return []

    now   = datetime.now(timezone.utc)
    until = now + timedelta(hours=hours_ahead)

    try:
        fx = requests.get(
            f"{BASE_URL}/fixtures",
            headers=_headers(api_key),
            params={
                "from":     now.strftime("%Y-%m-%d"),
                "to":       until.strftime("%Y-%m-%d"),
                "timezone": "UTC",
            },
            timeout=15,
        )
        remaining = fx.headers.get("x-ratelimit-requests-remaining", "?")
        if fx.status_code in (401, 403):
            log.error("API-Football: auth error — vérifier API_FOOTBALL_KEY")
            return []
        if fx.status_code != 200:
            log.warning("API-Football fixtures: HTTP %d", fx.status_code)
            return []
        try:
            remaining_int = int(remaining)
        except (TypeError, ValueError):
            remaining_int = None
        if remaining_int is not None and remaining_int < _QUOTA_GUARD_THRESHOLD:
            log.warning("API-Football quota guard — %d restantes, cycle ignoré", remaining_int)
            return []
        fixtures = fx.json().get("response", []) or []
    except Exception as e:
        log.error("API-Football fixtures: %s", e)
        return []

    matches = []
    for item in fixtures[:LIMIT_FIXTURES]:
        try:
            fixture    = item.get("fixture", {}) or {}
            teams      = item.get("teams", {}) or {}
            ts         = fixture.get("timestamp")
            fixture_id = fixture.get("id")
            if ts is None or fixture_id is None:
                continue
            commence = datetime.fromtimestamp(ts, tz=timezone.utc)
            if commence < now or commence > until:
                continue
            home = str((teams.get("home") or {}).get("name", "")).strip()
            away = str((teams.get("away") or {}).get("name", "")).strip()
            if not home or not away:
                continue

            odds_r = requests.get(
                f"{BASE_URL}/odds",
                headers=_headers(api_key),
                params={"fixture": fixture_id},
                timeout=15,
            )
            if odds_r.status_code != 200:
                continue
            odds_resp = odds_r.json().get("response", []) or []
            if not odds_resp:
                continue
            h2h = _extract_1x2(odds_resp[0].get("bookmakers", []))
            if not h2h:
                continue

            matches.append({
                "id":         f"af_{fixture_id}",
                "match":      f"{home} vs {away}",
                "home":       home,
                "away":       away,
                "league":     str((item.get("league") or {}).get("name", "Unknown")),
                "sport":      "soccer",
                "sport_id":   1,
                "odds_1xbet": h2h,
            })
        except Exception as e:
            log.warning("API-Football fixture parse: %s", e)
            continue

    if matches:
        log.info("API-Football: %d matchs | quota restant=%s", len(matches), remaining)
    return matches
