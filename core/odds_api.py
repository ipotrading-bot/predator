"""
core/odds_api.py — PAIM v8.3 — The Odds API (Hunter Multi-Sport Mode)
Markets: h2h | spreads | totals selon le sport

QUOTA — ne rien recopier ici, ce chiffre a déjà divergé trois fois. La seule
source de vérité est l'en-tête `x-requests-remaining` des réponses, loggé à
chaque appel servi. Constat du 2026-08-01 : le plan réel était à **500
requêtes/mois** (et non 20 000 comme l'annonçait ce docstring), pour une
consommation mesurée de ~16 crédits/heure — un mois de quota brûlé en 30
heures, deux mois de suite.

DÉCISION OPÉRATEUR (2026-08-01) : on laisse couler. Pas de rationnement, pas
de garde local — la clé se vide, on en change. Un gouverneur mensuel a été
écrit puis retiré : rendre des scans stériles pour étaler un budget que
l'opérateur préfère dépenser à fond n'avait pas de sens de son point de vue.

Reste le PRÉ-VOL GRATUIT (`_events_in_window`), qui n'est PAS du
rationnement : il ne refuse jamais un scan utile, il évite seulement de
payer une ligue qui n'a aucun match dans la fenêtre — la réponse aurait été
vide de toute façon. Il fait donc durer la même couverture plus longtemps,
sans jamais la réduire.
"""
import logging
import os
import re
import requests
from datetime import datetime, timedelta, timezone

from core.secret_store import get_secret

log = logging.getLogger("PREDATOR.odds_api")

BASE_URL     = "https://api.the-odds-api.com/v4"
PINNACLE_KEY = "pinnacle"
XBET_KEY     = "onexbet"
CIRCA_KEY    = "circa"        # Circa Sports — sharp US book
CRIS_KEY     = "bookmaker"    # Bookmaker.eu — CRIS network

# ── Sport keys actifs — sélection RENTABILITÉ MAXIMALE ───────────────
# Critères de sélection :
#   1. Lag Pinnacle→Melbet documenté (source d'edge réel)
#   2. Kelly fraction élevée (≥ 0.18 — confiance marché)
#   3. Volume quotidien suffisant (≥ 3 matchs/jour en moyenne)
#   4. Données Pinnacle + Melbet confirmées disponibles
#
# EXCLUS (signal/bruit trop faible) :
#   - Cricket / Darts / Boxing (Kelly 0.10–0.15, marchés peu efficients)
#   - Ligues Scandi / Irlande / Chine / Japon / Corée soccer (lag faible, volumes bas)
#   - Copa Sudamericana / Brazil B / Chile / Colombia / Argentina (Pinnacle peu liquide)
#   - Tennis (saison de transition gazon — incertitude surface/forme)
#
# Budget : Engine 6×/j × 11 = 66/j | Deep 2×/j × 11 = 22/j | Total : ~2 640/mois.
SPORT_KEYS = {
    # (Retiré 2026-08-06 — la Coupe du Monde 2026 est terminée, instruction
    # opérateur. Elle occupait la priorité 1 ; la ligue ne rend plus que des
    # 404 hors saison, mais la garder gardait aussi vivant le calendrier 168h
    # de run_engine.py, supprimé dans le même commit.)

    # ── PRIORITÉ 2 — Playoffs Amérique du Nord (sharps = Kelly 0.25–0.30) ──
    "basketball_nba":                        "basketball",  # NBA Finals — marché le + sharp au monde
    "basketball_wnba":                       "basketball",  # WNBA — juin-sept, comble le vide NBA/NHL off-season
    "icehockey_nhl":                         "hockey",      # NHL Stanley Cup Finals — mouvement max
    "baseball_mlb":                          "baseball",    # MLB — 10+ matchs/jour, lag US→EU ✓

    # ── PRIORITÉ 3 — Baseball Asie (lag timezone = fenêtre AM UTC) ────
    "baseball_kbo":                          "baseball",    # KBO Corée — lag Asie 06:00–13:00 UTC ✓
    "baseball_npb":                          "baseball",    # NPB Japon — lag Asie 06:00–13:00 UTC ✓

    # ── PRIORITÉ 5 — Copa Libertadores (lag SA soirée = fenêtre 21:00 UTC) ─
    "soccer_conmebol_copa_libertadores":     "soccer",      # R16/QF — lag SA maximal documenté

    # ── PRIORITÉ 6 — Brasileirão (quotidien, lag BR→EU cohérent) ─────
    "soccer_brazil_campeonato":              "soccer",      # Série A Brésil — marché sharp actif

    # ── PRIORITÉ 7 — MLS (très actif juin–août, lag NA→EU) ───────────
    "soccer_usa_mls":                        "soccer",      # MLS — volumes élevés, 1XBet actif

    # ── PRIORITÉ 8 — Autres ligues actives en juin ───────────────────
    "soccer_argentina_primera_division":     "soccer",      # Liga Argentina — marché SA sharp
    "soccer_mexico_ligamx":                  "soccer",      # Liga MX — actif été

    # ── PRIORITÉ 9 — Big 5 européen (dormant été, reprise mi/fin-août 2026) ──
    "soccer_epl":                            "soccer",      # EPL — reprise 21/08, Pinnacle+1xBet ✓
    "soccer_spain_la_liga":                  "soccer",      # La Liga — reprise 16/08, Pinnacle+1xBet ✓
    "soccer_germany_bundesliga":             "soccer",      # Bundesliga — reprise 28/08, Pinnacle+1xBet ✓
    "soccer_italy_serie_a":                  "soccer",      # Serie A — reprise 22/08, Pinnacle+1xBet ✓
    "soccer_france_ligue_one":               "soccer",      # Ligue 1 — reprise 22/08, Pinnacle+1xBet ✓

    # ── PRIORITÉ 7 — Australie (marchés Pinnacle très sharps) ────────
    "aussierules_afl":                       "aussierules", # AFL — ~9 matchs/semaine, Pinnacle ✓
    "rugbyleague_nrl":                       "rugbyleague", # NRL — ~8 matchs/semaine, Pinnacle ✓

    # ── Sports de combat — flux OddsAPI réel depuis le 2026-08-22 (Phase 1) ──
    # Le MMA était pricé par recherche web (fetch_mma_events, supprimé) : son
    # +37,5% de ROI sur 8 paris n'était validable par aucun CLV réel. h2h
    # seulement (_MARKETS_BY_SPORT). Les semaines sans carte ne coûtent rien :
    # le pré-vol _events_in_window (0 crédit) rend 0 et la ligue est sautée.
    "mma_mixed_martial_arts":                "mma",         # UFC/PFL/Bellator — cartes ven-dim
    "boxing_boxing":                         "boxing",      # boxe — marché mince, h2h

    # ── Phase 2 (2026-08-22) — auto-activation par le pré-vol gratuit ──────
    # Ajoutées AVANT leur saison : tant qu'aucun match n'est dans la fenêtre,
    # _events_in_window rend 0 et rien n'est payé. NFL : gardée en plus par
    # SEASON_OPENS (pas de présaison — lignes molles, rotations imprévisibles).
    "americanfootball_nfl":                  "americanfootball",       # NFL — saison régulière uniquement
    "soccer_uefa_champs_league":             "soccer",                 # LdC — phase de ligue mi-sept.
    "soccer_uefa_europa_league":             "soccer",                 # UEL — idem
    "basketball_euroleague":                 "euroleague_basketball",  # mécaniques basketball, Kelly dédiée
}

# ── Ouverture de saison : une ligue n'est pas scannée avant cette date ───
# Le pré-vol ne distingue pas présaison et saison régulière : un match NFL
# d'août est un match. Or la présaison est exactement ce qu'on ne veut pas
# (lignes molles, rotations imprévisibles — instruction opérateur). Surcharge
# par env (NFL_SEASON_START=YYYY-MM-DD) ; aucune date = pas de garde.
SEASON_OPENS: dict[str, str] = {
    "americanfootball_nfl": os.environ.get("NFL_SEASON_START", "2026-09-10"),
}


def _season_open(sport_key: str, now: datetime) -> bool:
    """False si la ligue a une date d'ouverture et qu'on est avant (0 crédit)."""
    raw = SEASON_OPENS.get(sport_key)
    if not raw:
        return True
    try:
        opens = datetime.fromisoformat(raw).replace(tzinfo=timezone.utc)
    except ValueError:
        log.warning("SEASON_OPENS[%s]=%r illisible — garde ignoré", sport_key, raw)
        return True
    return now >= opens

# Markets fetched per sport (API supports h2h,spreads,totals in one call)
_MARKETS_BY_SPORT = {
    "basketball":       "h2h,spreads,totals",
    "hockey":           "h2h,spreads,totals",  # NHL ML + puck line + O/U
    "americanfootball": "h2h,spreads,totals",  # NFL ML + point spread + O/U
    "baseball":         "h2h,totals",          # MLB ML + O/U (no spreads)
    "rugby":            "h2h,spreads,totals",
    "rugbyleague":      "h2h,spreads,totals",  # NRL — même structure que rugby union
    "aussierules":      "h2h,spreads,totals",  # AFL — ligne = 6.5+ pts typique
    "tennis":           "h2h,totals",
    "darts":            "h2h",
    "cricket":          "h2h",
    "boxing":           "h2h",
    "mma":              "h2h",                 # ML seulement — pas de spreads/totals sur un combat
    "soccer":           "h2h,spreads,totals",
    "euroleague_basketball": "h2h,spreads,totals",  # mêmes marchés que la NBA
}


# ── Extraction helpers ────────────────────────────────────────────────

def _odd(val) -> float:
    try:
        f = float(val)
        return f if f > 1.01 else 0.0
    except (TypeError, ValueError):
        return 0.0


def _extract_h2h(bookmakers: list, bookie_key: str, home: str, away: str) -> dict | None:
    """{"1": float, "X": float, "2": float} — X=0 for binary sports."""
    for bk in bookmakers:
        if bk.get("key") != bookie_key:
            continue
        for mkt in bk.get("markets", []):
            if mkt.get("key") != "h2h":
                continue
            prices = {o["name"]: _odd(o.get("price")) for o in mkt.get("outcomes", [])}
            return {
                "1": prices.get(home, 0.0),
                "X": prices.get("Draw", 0.0),
                "2": prices.get(away, 0.0),
            }
    return None


def _extract_spreads(bookmakers: list, bookie_key: str, home: str, away: str) -> dict | None:
    """{"home": float, "away": float, "point": float} — point is home team's line."""
    for bk in bookmakers:
        if bk.get("key") != bookie_key:
            continue
        for mkt in bk.get("markets", []):
            if mkt.get("key") != "spreads":
                continue
            result: dict = {}
            for o in mkt.get("outcomes", []):
                price = _odd(o.get("price"))
                point = float(o.get("point", 0))
                if o["name"] == home:
                    result["home"]  = price
                    result["point"] = point
                elif o["name"] == away:
                    result["away"]       = price
                    result["away_point"] = point
            if "home" in result and "away" in result and result["home"] > 1.01:
                return result
    return None


def _extract_totals(bookmakers: list, bookie_key: str) -> dict | None:
    """{"over": float, "under": float, "point": float}."""
    for bk in bookmakers:
        if bk.get("key") != bookie_key:
            continue
        for mkt in bk.get("markets", []):
            if mkt.get("key") != "totals":
                continue
            result: dict = {}
            for o in mkt.get("outcomes", []):
                price = _odd(o.get("price"))
                side  = o.get("name", "").lower()
                if side == "over":
                    result["over"]  = price
                    result["point"] = float(o.get("point", 0))
                elif side == "under":
                    result["under"] = price
            if "over" in result and "under" in result:
                return result
    return None


# ── Event parser ──────────────────────────────────────────────────────

def _parse_event(ev: dict, sport_type: str) -> dict | None:
    home = str(ev.get("home_team", "")).strip()
    away = str(ev.get("away_team", "")).strip()
    if not home or not away:
        return None

    bookmakers = ev.get("bookmakers", [])

    xbet_h2h  = _extract_h2h(bookmakers, XBET_KEY,     home, away)
    pin_h2h   = _extract_h2h(bookmakers, PINNACLE_KEY, home, away)
    if not xbet_h2h or not pin_h2h:
        return None  # Both books must have h2h for the event to be useful

    circa_h2h = _extract_h2h(bookmakers, CIRCA_KEY, home, away)
    cris_h2h  = _extract_h2h(bookmakers, CRIS_KEY,  home, away)

    event = {
        "id":            ev.get("id", f"{home}_{away}"),
        "match":         f"{home} vs {away}",
        "home":          home,
        "away":          away,
        "league":        ev.get("sport_title", ""),
        "sport":         sport_type,
        "sport_id":      {"soccer": 1, "tennis": 3, "basketball": 4, "boxing": 5, "darts": 6, "cricket": 7, "hockey": 8, "americanfootball": 10, "baseball": 11, "rugby": 12, "volleyball": 13, "tabletennis": 14, "handball": 15, "aussierules": 16, "rugbyleague": 17, "euroleague_basketball": 4}.get(sport_type, 1),
        "commence_time": ev.get("commence_time", ""),
        "odds_1xbet":    xbet_h2h,
        "odds_pinnacle": pin_h2h,
    }
    if circa_h2h:
        event["odds_circa"] = circa_h2h
    if cris_h2h:
        event["odds_cris"] = cris_h2h

    # ── Spreads (binary sports only — tennis/boxing/darts/cricket/baseball have no spreads) ──
    if sport_type not in ("tennis", "boxing", "mma", "darts", "cricket", "baseball", "rugbyleague"):
        xs = _extract_spreads(bookmakers, XBET_KEY,     home, away)
        ps = _extract_spreads(bookmakers, PINNACLE_KEY, home, away)
        if xs and ps:
            event["spreads_1xbet"]    = xs
            event["spreads_pinnacle"] = ps
            cs = _extract_spreads(bookmakers, CIRCA_KEY, home, away)
            rs = _extract_spreads(bookmakers, CRIS_KEY,  home, away)
            if cs:
                event["spreads_circa"] = cs
            if rs:
                event["spreads_cris"] = rs

    # ── Totals (all sports) ───────────────────────────────────────────
    xt = _extract_totals(bookmakers, XBET_KEY)
    pt = _extract_totals(bookmakers, PINNACLE_KEY)
    if xt and pt:
        event["totals_1xbet"]    = xt
        event["totals_pinnacle"] = pt
        ct = _extract_totals(bookmakers, CIRCA_KEY)
        rt = _extract_totals(bookmakers, CRIS_KEY)
        if ct:
            event["totals_circa"] = ct
        if rt:
            event["totals_cris"] = rt

    return event


# ── Pré-vol GRATUIT ───────────────────────────────────────────────────
# Deux endpoints de l'API v4 ne comptent PAS dans le quota (doc éditeur,
# vérifiée 2026-08-01) : `/v4/sports` et `/v4/sports/{key}/events`. Seul
# `/odds` est facturé, au tarif [marchés] × [régions] — soit 3 crédits par
# ligue pour un `h2h,spreads,totals` sur `eu`.
#
# Jusqu'ici on payait ces 3 crédits pour CHAQUE ligue de SPORT_KEYS à chaque
# scan, y compris celles qui n'avaient aucun match dans la fenêtre : en Golden
# Hour (fenêtre 2h) c'est le cas de presque toutes. Demander d'abord la liste
# des matchs — gratuitement — et ne payer que les ligues qui en ont au moins
# un est une économie sans aucune contrepartie : la réponse `/odds` aurait été
# vide de toute façon. Ce n'est pas du rationnement — aucun scan utile n'est
# refusé, jamais.

def _events_in_window(api_key: str, sport_key: str,
                      time_from: str, time_to: str) -> int | None:
    """Nombre de matchs de cette ligue dans la fenêtre — 0 crédit.

    Renvoie None si l'appel échoue : on ne sait pas, donc on laisse le scan
    payant décider. Ne jamais transformer une panne du pré-vol en « pas de
    match » — ce serait une panne silencieuse du pipeline entier.
    """
    try:
        r = requests.get(
            f"{BASE_URL}/sports/{sport_key}/events/",
            params={"apiKey": api_key,
                    "commenceTimeFrom": time_from,
                    "commenceTimeTo": time_to},
            timeout=10,
        )
        if r.status_code == 404:
            return 0          # hors saison
        if r.status_code != 200:
            return None
        return len(r.json() or [])
    except Exception as e:
        log.debug("preflight %s: %s", sport_key, e)
        return None


# ── Pool de clés OddsAPI (v10.3) ──────────────────────────────────────
#
# POURQUOI. Le 10 août 2026 la clé unique ODDS_API_KEY est tombée à 0 crédit
# et n'a pas été tournée pendant dix jours : 40 runs/jour à « 0 matchs,
# 0 signaux », et rien dans le système ne pouvait faire mieux qu'attendre un
# humain. Une clé épuisée est un événement NORMAL (500 req/mois, soit une clé
# tous les ~2 jours au rythme actuel) — il doit se traiter tout seul.
#
# COMMENT. Le moteur lit désormais un POOL ordonné :
#   1. l'argument explicite `api_key` (tests, scripts)
#   2. `ODDS_API_KEYS` — plusieurs clés séparées par virgule/espace/retour
#      à la ligne, dans app_secrets (Supabase) ou l'environnement
#   3. `ODDS_API_KEY` — la clé « historique », toujours honorée
#   4. `ODDS_API_KEY_2` … `ODDS_API_KEY_9` dans l'environnement
# Chaque clé est sondée via GET /v4/sports (0 crédit) avant d'être utilisée ;
# un 401/403/422 en cours de scan marque la clé morte et le scan REPREND sur
# la suivante, même ligue, sans rien perdre. Le marquage est process-local :
# chaque run re-sonde (gratuit), donc une clé rechargée le 1er du mois
# revient d'elle-même dans la rotation.
#
# Ajouter une clé : `python scripts/rotate_odds_key.py --add <clé>` — elle
# est validée avant d'être écrite dans app_secrets.ODDS_API_KEYS.

POOL_SECRET = "ODDS_API_KEYS"

# Crédits minimum pour qu'une clé soit jugée utilisable. À 0 il reste de quoi
# faire échouer un scan au milieu ; le pré-vol gratuit (`_events_in_window`)
# ne coûte rien mais l'appel /odds qui suit, si. Ce n'est PAS un rationnement
# (décision opérateur 2026-08-01) : la clé est écartée quand elle ne peut
# plus rien payer, pas avant.
MIN_CREDITS = int(os.environ.get("ODDS_MIN_CREDITS", "1"))
_dead_keys: dict[str, str] = {}      # clé -> raison (process-local)
_last_failure: str = ""


def _split_keys(raw: str | None) -> list[str]:
    return [k.strip() for k in re.split(r"[,;\s]+", raw or "") if k.strip()]


def candidate_keys(explicit: str | None = None) -> list[str]:
    """Pool ordonné et dédupliqué — voir le bloc de commentaire ci-dessus."""
    out: list[str] = []

    def add(raw: str | None) -> None:
        for k in _split_keys(raw):
            if k not in out:
                out.append(k)

    add(explicit)
    add(get_secret(POOL_SECRET))
    add(get_secret("ODDS_API_KEY"))
    # L'ENVIRONNEMENT rejoint TOUJOURS le pool, même quand app_secrets a une
    # valeur : get_secret() ne regarde l'env que si la table est VIDE, donc
    # une clé neuve posée dans les secrets GitHub restait invisible tant
    # qu'une clé périmée traînait dans la table (constaté le 2026-08-22 :
    # app_secrets figé au 06/08 sur une clé à 499/500, rotation opérateur
    # sans effet). La clé morte est écartée par la sonde gratuite, la
    # neuve prend le relais — sans toucher à la priorité de la table.
    add(os.environ.get(POOL_SECRET))
    add(os.environ.get("ODDS_API_KEY"))
    for i in range(2, 10):
        add(os.environ.get(f"ODDS_API_KEY_{i}"))
    return out


def mark_dead(key: str, reason: str) -> None:
    global _last_failure
    _dead_keys[key] = reason
    _last_failure = reason


def reset_pool() -> None:
    """Oublie les clés marquées mortes (tests, ou après une rotation)."""
    global _last_failure
    _dead_keys.clear()
    _last_failure = ""


def pool_status(explicit: str | None = None) -> dict:
    """{'total': n, 'dead': m, 'live': n-m, 'reason': dernière panne} —
    pour les logs et l'alerte Telegram de run_engine.py."""
    keys = candidate_keys(explicit)
    dead = [k for k in keys if k in _dead_keys]
    return {"total": len(keys), "dead": len(dead), "live": len(keys) - len(dead),
            "reason": _last_failure}


def pool_exhausted() -> bool:
    """True quand il EXISTE des clés et qu'elles sont TOUTES mortes —
    le cas qui réclame une rotation humaine, et seulement celui-là."""
    st = pool_status()
    return st["total"] > 0 and st["live"] == 0


# Dernier `x-requests-remaining` vu (sonde gratuite ou réponse payante) —
# c'est ce que la politique de dépense (core/scan_windows.SpendPolicy) lit
# pour sa garde de réserve. None = jamais observé.
_last_remaining: int | None = None
_last_used: int | None = None


def pool_remaining() -> int | None:
    return _last_remaining


def pool_counters() -> dict:
    """{'remaining', 'used', 'total', 'pct'} de la clé ACTIVE (celle que le
    prochain scan utilisera) — None partout si jamais observé. Sert à la
    ligne de log et à l'alerte par paliers de run_engine (Mission 2)."""
    r, u = _last_remaining, _last_used
    if r is None or u is None or (r + u) <= 0:
        return {"remaining": r, "used": u, "total": None, "pct": None}
    return {"remaining": r, "used": u, "total": r + u, "pct": 100.0 * r / (r + u)}


def _note_remaining(raw, used=None) -> None:
    global _last_remaining, _last_used
    try:
        _last_remaining = int(raw)
    except (TypeError, ValueError):
        pass
    if used is not None:
        try:
            _last_used = int(used)
        except (TypeError, ValueError):
            pass


def probe_key(key: str) -> tuple[bool, str]:
    """(vivante?, détail) via GET /v4/sports — 0 crédit consommé."""
    try:
        r = requests.get(f"{BASE_URL}/sports/", params={"apiKey": key}, timeout=10)
    except Exception as e:
        return False, f"appel impossible : {e}"
    if r.status_code in (401, 403):
        return False, f"HTTP {r.status_code} — clé invalide, révoquée ou quota épuisé"
    if r.status_code == 422:
        return False, "HTTP 422 — quota épuisé"
    if r.status_code != 200:
        return False, f"HTTP {r.status_code}"
    # Le compteur, pas seulement le code HTTP : mesuré en direct le
    # 2026-08-20, une clé à 499/500 crédits répond encore 200 sur /sports
    # (endpoint gratuit) alors qu'elle n'a plus de quoi payer un scan. La
    # laisser « vivante » ferait perdre la première ligue du scan sur un 401.
    remaining = r.headers.get("x-requests-remaining")
    used = r.headers.get("x-requests-used", "?")
    try:
        left = int(remaining)
    except (TypeError, ValueError):
        left = None
    if left is not None:
        _note_remaining(left, used)
    if left is not None and left < MIN_CREDITS:
        return False, f"quota épuisé — restantes={left} utilisées={used}"
    return True, f"HTTP 200 — restantes={remaining or '?'} utilisées={used}"


def _next_live_key(keys: list[str], start: int) -> tuple[str | None, int]:
    """Première clé vivante à partir de l'index `start` (sondage gratuit).
    Les clés qui ne répondent pas sont marquées mortes au passage."""
    for i in range(start, len(keys)):
        k = keys[i]
        if k in _dead_keys:
            continue
        ok, detail = probe_key(k)
        if ok:
            log.info("OddsAPI clé #%d/%d active (…%s) — %s", i + 1, len(keys), k[-4:], detail)
            return k, i
        mark_dead(k, detail)
        log.warning("OddsAPI clé #%d/%d (…%s) écartée — %s", i + 1, len(keys), k[-4:], detail)
    return None, len(keys)


# ── Public API ────────────────────────────────────────────────────────

def fetch_odds(api_key: str | None = None, hours_ahead: int = 24,
               sport_keys: dict | None = None, spend_policy=None) -> list[dict]:
    """
    Fetch events in the next `hours_ahead` hours with h2h + spreads + totals.

    `spend_policy` (core/scan_windows.SpendPolicy, optionnel) : consultée
    ligue par ligue APRÈS le pré-vol gratuit — une ligue peuplée mais hors
    fenêtre favorable, payée il y a moins de 180 min, ou sous la réserve
    de crédits, est sautée ET loggée. Sans politique, on paie comme avant.
    Priority: NBA → Tennis Masters → Soccer.
    sport_keys: override the default SPORT_KEYS dict (used by Golden Hour mode).
    Returns [] if API key missing or quota exhausted (engine falls back to Gemini).

    AUCUN RATIONNEMENT, décision opérateur du 2026-08-01 : « laisse OddsAPI
    couler, si c'est fini on aura d'autres [clés] ». Un gouverneur de quota
    mensuel a été écrit puis retiré — il rendait des scans stériles pour
    étaler un budget que l'opérateur préfère dépenser à fond, quitte à
    changer de clé. L'ancien garde `remaining < 50` est parti avec : il
    immobilisait 10% du plan sans jamais le dépenser. Le scan s'arrête
    désormais sur un vrai 422 (quota épuisé côté API), pas avant.
    """
    # Pool de clés : app_secrets (Supabase) d'abord, os.environ en filet —
    # voir le bloc « Pool de clés OddsAPI » plus haut et core/secret_store.py.
    keys = candidate_keys(api_key)
    if not keys:
        log.error("No ODDS_API_KEY — ni dans app_secrets (Supabase) ni dans "
                  "l'environnement (.env / GitHub Secrets / Vercel)")
        return []
    api_key, key_idx = _next_live_key(keys, 0)
    if api_key is None:
        log.critical("OddsAPI : les %d clés du pool sont épuisées/invalides (%s) — "
                     "rotation requise : python scripts/rotate_odds_key.py --add <clé>",
                     len(keys), _last_failure)
        return []
    assert api_key is not None  # narrow type after early return

    keys_to_scan = sport_keys if sport_keys is not None else SPORT_KEYS

    now       = datetime.now(timezone.utc)
    until     = now + timedelta(hours=hours_ahead)
    time_from = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    time_to   = until.strftime("%Y-%m-%dT%H:%M:%SZ")

    # Pré-vol gratuit : on ne garde que les ligues qui ont réellement des
    # matchs dans la fenêtre, triées par volume décroissant — si un 422
    # interrompt le scan, il l'aura interrompu sur les ligues les moins
    # fournies, pas au hasard de l'ordre du dictionnaire.
    scan_plan: list = []
    skipped_empty = 0
    skipped_season = 0
    skipped_policy = 0
    for sport_key, sport_type in keys_to_scan.items():
        if not _season_open(sport_key, now):
            skipped_season += 1
            continue
        n_events = _events_in_window(api_key, sport_key, time_from, time_to)
        if n_events == 0:
            skipped_empty += 1
            continue
        if spend_policy is not None:
            allowed, _why = spend_policy.allow(sport_key, sport_type, now, _last_remaining)
            if not allowed:
                skipped_policy += 1
                continue
        scan_plan.append((sport_key, sport_type, n_events if n_events is not None else 0))
    scan_plan.sort(key=lambda x: x[2], reverse=True)
    if skipped_empty:
        log.info("Pré-vol gratuit : %d/%d ligues sans match dans la fenêtre — "
                 "%d crédits économisés", skipped_empty, len(keys_to_scan), skipped_empty * 3)
    if skipped_season:
        log.info("Hors saison : %d ligue(s) avant leur date d'ouverture (SEASON_OPENS) — "
                 "0 crédit, 0 appel", skipped_season)
    if skipped_policy:
        log.info("Politique de dépense : %d ligue(s) peuplée(s) sautée(s) ce scan "
                 "(fond espacé / réserve) — détail ligne par ligne ci-dessus", skipped_policy)

    all_events = []
    for sport_key, sport_type, _n in scan_plan:
        markets = _MARKETS_BY_SPORT.get(sport_type, "h2h")
        url = f"{BASE_URL}/sports/{sport_key}/odds/"
        params = {
            "apiKey":           api_key,
            "regions":          "eu",
            "markets":          markets,
            "bookmakers":       f"{PINNACLE_KEY},{XBET_KEY},{CIRCA_KEY},{CRIS_KEY}",
            "oddsFormat":       "decimal",
            "commenceTimeFrom": time_from,
            "commenceTimeTo":   time_to,
        }
        try:
            r = None
            while True:
                params["apiKey"] = api_key
                r = requests.get(url, params=params, timeout=15)
                if r.status_code not in (401, 403, 422):
                    break
                # Clé à sec (422) ou refusée (401/403 — OddsAPI renvoie aussi
                # un 401 OUT_OF_USAGE_CREDITS à 0 crédit) : on la marque
                # morte et on REPREND LA MÊME LIGUE sur la clé suivante du
                # pool. Le scan ne s'arrête que si le pool entier est mort.
                mark_dead(api_key, f"HTTP {r.status_code} sur {sport_key}")
                log.warning("OddsAPI clé #%d (…%s) morte (HTTP %d) — bascule",
                            key_idx + 1, api_key[-4:], r.status_code)
                api_key, key_idx = _next_live_key(keys, key_idx + 1)
                if api_key is None:
                    log.critical("OddsAPI : pool épuisé (%d clés) après %d ligues — "
                                 "%d events conservés. Rotation requise : "
                                 "python scripts/rotate_odds_key.py --add <clé>",
                                 len(keys), list(keys_to_scan).index(sport_key), len(all_events))
                    return all_events
            remaining = r.headers.get("x-requests-remaining", "?")
            used      = r.headers.get("x-requests-used", "?")
            _note_remaining(remaining, used)
            if spend_policy is not None and r.status_code == 200:
                spend_policy.note_paid(sport_key)

            if r.status_code == 404:
                continue  # Not in season
            if r.status_code != 200:
                log.warning("%s: HTTP %d", sport_key, r.status_code)
                continue

            events = [_parse_event(e, sport_type) for e in r.json()]
            events = [e for e in events if e]
            all_events.extend(events)
            if events:
                has_totals  = sum(1 for e in events if "totals_1xbet"  in e)
                has_spreads = sum(1 for e in events if "spreads_1xbet" in e)
                log.info("%s: %d events | totals=%d spreads=%d | used=%s remaining=%s",
                         sport_key, len(events), has_totals, has_spreads, used, remaining)

        except Exception as e:
            log.error("%s: %s", sport_key, e)

    return all_events
