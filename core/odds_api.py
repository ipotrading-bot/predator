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
    # ── PRIORITÉ 1 — FIFA World Cup 2026 (lag maximal garanti) ────────
    "soccer_fifa_world_cup":                 "soccer",      # 48 matchs phase groupes — lag +2h documenté

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
}

# Markets fetched per sport (API supports h2h,spreads,totals in one call)
_MARKETS_BY_SPORT = {
    "basketball":       "h2h,spreads,totals",
    "hockey":           "h2h,spreads,totals",  # NHL ML + puck line + O/U
    "americanfootball": "h2h,spreads,totals",  # NFL ML + point spread + O/U
    "baseball":         "h2h,totals",          # MLB ML + O/U (no spreads)
    "rugby":            "h2h,spreads,totals",
    "rugbyleague":      "h2h,spreads,totals",  # NRL — même structure que rugby union
    "aussierules":      "h2h,spreads,totals",  # AFL — ligne = 6.5+ pts typique
    "volleyball":       "h2h,totals",
    "tennis":           "h2h,totals",
    "darts":            "h2h",
    "cricket":          "h2h",
    "boxing":           "h2h",
    "soccer":           "h2h,spreads,totals",
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
        "sport_id":      {"soccer": 1, "tennis": 3, "basketball": 4, "boxing": 5, "darts": 6, "cricket": 7, "hockey": 8, "americanfootball": 10, "baseball": 11, "rugby": 12, "volleyball": 13, "tabletennis": 14, "handball": 15, "aussierules": 16, "rugbyleague": 17}.get(sport_type, 1),
        "commence_time": ev.get("commence_time", ""),
        "odds_1xbet":    xbet_h2h,
        "odds_pinnacle": pin_h2h,
    }
    if circa_h2h:
        event["odds_circa"] = circa_h2h
    if cris_h2h:
        event["odds_cris"] = cris_h2h

    # ── Spreads (binary sports only — tennis/boxing/darts/cricket/baseball have no spreads) ──
    if sport_type not in ("tennis", "boxing", "darts", "cricket", "baseball", "volleyball", "rugbyleague"):
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


# ── Public API ────────────────────────────────────────────────────────

def fetch_odds(api_key: str | None = None, hours_ahead: int = 24,
               sport_keys: dict | None = None) -> list[dict]:
    """
    Fetch events in the next `hours_ahead` hours with h2h + spreads + totals.
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
    if not api_key:
        # Supabase (app_secrets) d'abord, os.environ en filet — la clé tourne
        # tous les 1-2 jours et le secret GitHub se périme entre deux
        # rotations. Voir core/secret_store.py.
        api_key = get_secret("ODDS_API_KEY")
    if not api_key:
        log.error("No ODDS_API_KEY — ni dans app_secrets (Supabase) ni dans "
                  "l'environnement (.env / GitHub Secrets / Vercel)")
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
    for sport_key, sport_type in keys_to_scan.items():
        n_events = _events_in_window(api_key, sport_key, time_from, time_to)
        if n_events == 0:
            skipped_empty += 1
            continue
        scan_plan.append((sport_key, sport_type, n_events if n_events is not None else 0))
    scan_plan.sort(key=lambda x: x[2], reverse=True)
    if skipped_empty:
        log.info("Pré-vol gratuit : %d/%d ligues sans match dans la fenêtre — "
                 "%d crédits économisés", skipped_empty, len(keys_to_scan), skipped_empty * 3)

    all_events = []
    quota_remaining = 9999  # updated after first successful response
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
            r = requests.get(url, params=params, timeout=15)
            remaining = r.headers.get("x-requests-remaining", "?")
            used      = r.headers.get("x-requests-used", "?")
            try:
                quota_remaining = int(remaining)
            except (ValueError, TypeError):
                pass

            if r.status_code == 404:
                continue  # Not in season
            if r.status_code in (401, 403):
                log.error("Auth error — check ODDS_API_KEY")
                return []
            if r.status_code == 422:
                # Vrai épuisement côté API : la clé est à sec, il faut en
                # changer. C'est le SEUL arrêt possible — plus aucun garde
                # local ne coupe le scan avant (décision opérateur).
                log.warning("Quota exhausted after %d sport keys — keeping %d events fetched so far",
                            list(keys_to_scan).index(sport_key), len(all_events))
                break
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
