"""
core/api_football.py — PAIM — API-Football (api-sports.io), source foot
additionnelle et INDÉPENDANTE de The Odds API — fournisseur différent,
donc pas exposée à la même panne de compte/quota (voir l'incident du
2026-07-12 où ODDS_API_KEY est resté hors service plusieurs jours, et celui
du 2026-08-10 → 20 où rien n'a été émis pendant dix jours).

S'insère dans core/harvester.py::_fetch_multi_book() comme un book de plus,
au même titre que 1xbet/Melbet/22bet — même forme de sortie (match/home/
away/league/sport/sport_id/odds_1xbet). "odds_1xbet" est le nom générique
du champ « prix soft » utilisé dans tout le pipeline, pas spécifique au
bookmaker 1xbet.

v10.3 (2026-08-20) — deux changements qui font de ce module une VRAIE source
de repli et non un figurant :

1. BUDGET. L'ancienne version payait 1 requête /odds PAR FIXTURE (jusqu'à
   LIMIT_FIXTURES) à chaque cycle ; avec ~40 runs/jour et un plan gratuit
   de 100 req/jour, le quota était mort avant midi et la fonction rendait
   [] sans un seul log (un /odds ≠ 200 faisait `continue` en silence). On
   interroge désormais /odds PAR DATE, paginé : 1 requête /fixtures +
   au plus MAX_ODDS_PAGES requêtes par jour de la fenêtre (2 jours en 24h),
   soit ≤ 7 requêtes par cycle. Et chaque réponse ≠ 200 est loggée.

2. PINNACLE DANS LA RÉPONSE. /odds renvoie TOUS les bookmakers du plan pour
   chaque fixture ; s'il y a Pinnacle, on le sort comme `odds_pinnacle`
   (prix sharp) en plus du meilleur prix soft des autres books. Un match
   ainsi prixé des deux côtés donne un signal SANS recherche web — c'est
   exactement ce qui manquait quand Groq/Tavily étaient à sec.

⚠️ Structure de réponse (fixtures[].fixture/teams/league, odds[].fixture.id/
bookmakers[].name/bets[].values[], paging.current/total) : doc publique
d'API-Football v3, non vérifiée live depuis ce sandbox (403/Cloudflare sur
les fetchs automatisés). Tout écart se verra dans les logs Actions : chercher
"API-Football" — il y a maintenant une ligne par cycle, succès ou échec.
"""
import logging
from datetime import datetime, timedelta, timezone

import requests

from core.secret_store import get_secret

log = logging.getLogger("PREDATOR.api_football")

BASE_URL = "https://v3.football.api-sports.io"

# Pages /odds lues par jour de la fenêtre (l'API pagine par ~10 fixtures).
# Plafonne le coût d'un cycle à 1 + 2*MAX_ODDS_PAGES requêtes en fenêtre 24h.
MAX_ODDS_PAGES = 3

# Garde : sous ce seuil de requêtes restantes (en-tête x-ratelimit-
# requests-remaining, plan gratuit = 100/jour), on rend ce qu'on a plutôt
# que de risquer un 429 au milieu du cycle.
_QUOTA_GUARD_THRESHOLD = 8

_MATCH_WINNER_NAMES = {"match winner", "home/away", "1x2", "full time result"}

# Bookmakers considérés « sharp » dans la réponse /odds — reconnus par NOM
# (insensible à la casse), pas par id numérique : les ids de la doc n'ont
# pas pu être vérifiés et un id faux rendrait silencieusement 0 Pinnacle.
SHARP_BOOKMAKER_NAMES = ("pinnacle",)


def _headers(api_key: str) -> dict:
    return {"x-apisports-key": api_key}


def _odd(val) -> float:
    try:
        f = float(val)
        return f if f > 1.01 else 0.0
    except (TypeError, ValueError):
        return 0.0


def _is_sharp(bookmaker: dict) -> bool:
    name = str(bookmaker.get("name", "")).strip().lower()
    return any(s in name for s in SHARP_BOOKMAKER_NAMES)


def _bookmaker_1x2(bookmaker: dict) -> dict | None:
    """{"1","X","2"} du marché vainqueur d'UN bookmaker, ou None."""
    for bet in bookmaker.get("bets", []) or []:
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
        if o1 and o2:
            return {"1": o1, "X": ox or 0.0, "2": o2}
    return None


def _extract_1x2(bookmakers: list, *, sharp: bool = False) -> dict | None:
    """Meilleure cote 1X2 par issue parmi les bookmakers SOFT (line shopping,
    cohérent avec le merge multi-book de harvester.py) — ou, avec
    sharp=True, la cote du premier bookmaker sharp trouvé (pas de line
    shopping côté sharp : on veut LE prix Pinnacle, pas le plus généreux)."""
    best: dict = {}
    for bk in bookmakers or []:
        if _is_sharp(bk) != sharp:
            continue
        odds = _bookmaker_1x2(bk)
        if not odds:
            continue
        if sharp:
            return odds
        if not best:
            best = dict(odds)
        else:
            for k in ("1", "X", "2"):
                if odds.get(k, 0.0) > best.get(k, 0.0):
                    best[k] = odds[k]
    return best or None


def _remaining(resp) -> int | None:
    try:
        return int(resp.headers.get("x-ratelimit-requests-remaining", ""))
    except (TypeError, ValueError):
        return None


def fetch_football_matches(api_key: str | None = None, hours_ahead: int = 24) -> list[dict]:
    """
    Fixtures foot + cotes 1X2 des prochaines `hours_ahead` heures. Sortie
    dans la même forme que core/harvester.py::_parse_xbet_json (plus
    `commence_time`, et `odds_pinnacle` quand le plan expose Pinnacle),
    pour s'insérer directement dans _fetch_multi_book(). Returns [] si clé
    absente, quota bas, ou erreur — jamais d'exception vers l'appelant, et
    TOUJOURS une ligne de log disant pourquoi.
    """
    if not api_key:
        api_key = get_secret("API_FOOTBALL_KEY")
    if not api_key:
        log.debug("API-Football: pas de clé — source ignorée")
        return []

    now   = datetime.now(timezone.utc)
    until = now + timedelta(hours=hours_ahead)
    requests_used = 0

    # ── 1. Fixtures de la fenêtre (1 requête) ───────────────────────────
    try:
        fx = requests.get(
            f"{BASE_URL}/fixtures",
            headers=_headers(api_key),
            params={"from": now.strftime("%Y-%m-%d"),
                    "to":   until.strftime("%Y-%m-%d"),
                    "timezone": "UTC"},
            timeout=15,
        )
        requests_used += 1
        remaining = _remaining(fx)
        if fx.status_code in (401, 403):
            log.error("API-Football: auth error (HTTP %d) — vérifier API_FOOTBALL_KEY", fx.status_code)
            return []
        if fx.status_code == 429:
            log.warning("API-Football: HTTP 429 — quota journalier épuisé (restant=%s)", remaining)
            return []
        if fx.status_code != 200:
            log.warning("API-Football fixtures: HTTP %d", fx.status_code)
            return []
        fixtures = fx.json().get("response", []) or []
    except Exception as e:
        log.error("API-Football fixtures: %s", e)
        return []

    by_id: dict[int, dict] = {}
    for item in fixtures:
        try:
            fixture = item.get("fixture", {}) or {}
            teams   = item.get("teams", {}) or {}
            ts, fid = fixture.get("timestamp"), fixture.get("id")
            if ts is None or fid is None:
                continue
            commence = datetime.fromtimestamp(ts, tz=timezone.utc)
            if commence < now or commence > until:
                continue
            home = str((teams.get("home") or {}).get("name", "")).strip()
            away = str((teams.get("away") or {}).get("name", "")).strip()
            if not home or not away:
                continue
            by_id[int(fid)] = {
                "home": home, "away": away, "commence": commence,
                "league": str((item.get("league") or {}).get("name", "Unknown")),
            }
        except Exception as e:
            log.debug("API-Football fixture parse: %s", e)

    if not by_id:
        log.info("API-Football: 0 fixture dans les %dh (réponse %d) | quota restant=%s",
                 hours_ahead, len(fixtures), remaining)
        return []

    # ── 2. Cotes PAR DATE, paginées (≤ MAX_ODDS_PAGES requêtes/jour) ────
    dates = sorted({v["commence"].strftime("%Y-%m-%d") for v in by_id.values()})
    matches: list[dict] = []
    seen: set[int] = set()
    n_sharp = 0
    stopped = ""
    for day in dates:
        for page in range(1, MAX_ODDS_PAGES + 1):
            if remaining is not None and remaining < _QUOTA_GUARD_THRESHOLD:
                stopped = f"garde quota ({remaining} restantes)"
                break
            try:
                r = requests.get(
                    f"{BASE_URL}/odds",
                    headers=_headers(api_key),
                    params={"date": day, "page": page, "timezone": "UTC"},
                    timeout=15,
                )
            except Exception as e:
                log.warning("API-Football odds %s p%d: %s", day, page, e)
                stopped = "erreur réseau"
                break
            requests_used += 1
            remaining = _remaining(r) if _remaining(r) is not None else remaining
            if r.status_code != 200:
                log.warning("API-Football odds %s p%d: HTTP %d (restant=%s)",
                            day, page, r.status_code, remaining)
                stopped = f"HTTP {r.status_code}"
                break
            body = r.json() or {}
            for item in body.get("response", []) or []:
                try:
                    fid = int((item.get("fixture") or {}).get("id"))
                except (TypeError, ValueError):
                    continue
                meta = by_id.get(fid)
                if meta is None or fid in seen:
                    continue
                bks  = item.get("bookmakers", []) or []
                soft = _extract_1x2(bks, sharp=False)
                sharp = _extract_1x2(bks, sharp=True)
                if not soft and not sharp:
                    continue
                m = {
                    "id":            f"af_{fid}",
                    "match":         f"{meta['home']} vs {meta['away']}",
                    "home":          meta["home"],
                    "away":          meta["away"],
                    "league":        meta["league"],
                    "sport":         "soccer",
                    "sport_id":      1,
                    "commence_time": meta["commence"].strftime("%Y-%m-%dT%H:%M:%SZ"),
                    # Sans book soft, le prix Pinnacle sert aussi de soft :
                    # edge nul, donc jamais de signal, mais le match reste
                    # visible et prixé pour les autres books du merge.
                    "odds_1xbet":    soft or sharp,
                }
                if sharp:
                    m["odds_pinnacle"] = sharp
                    n_sharp += 1
                matches.append(m)
                seen.add(fid)
            paging = body.get("paging") or {}
            try:
                if int(paging.get("current", page)) >= int(paging.get("total", page)):
                    break
            except (TypeError, ValueError):
                break
        if stopped:
            break

    log.info("API-Football: %d matchs (%d avec Pinnacle) sur %d fixtures | %d req%s | quota restant=%s",
             len(matches), n_sharp, len(by_id), requests_used,
             f" | arrêt: {stopped}" if stopped else "", remaining)
    return matches
