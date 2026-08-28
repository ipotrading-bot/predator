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
import hashlib
import logging
import os
import re
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

# ── RYTHME DE DÉPENSE (2026-08-27) ────────────────────────────────────
# Troisième source du stack à tomber sur la même panne, et c'est celle qui
# fait le plus mal : odds-api.io porte le côté SOFT, et le moteur ne peut
# pas calculer d'edge sur un match dont il n'a qu'un côté.
# Relevé le 2026-08-27 à 20:05 : « budget journalier atteint (400/400) » sur
# mma, baseball et hockey, puis 408/400 en fin de soirée. Le budget partait
# premier arrivé, premier servi — les crons du matin le raflaient et les
# scans du soir, quand le slate européen entre dans la zone jouable 2-24 h,
# repartaient sans prix soft.
# Le budget n'est pas augmenté : il est étalé (core/daily_quota).
CYCLE_COST = int(os.environ.get("ODDS_API_IO_CYCLE_COST", "12"))
MULTI_BATCH  = 10          # maximum accepté par /v3/odds/multi
MAX_EVENTS   = int(os.environ.get("ODDS_API_IO_MAX_EVENTS", "60"))
TIMEOUT      = int(os.environ.get("ODDS_API_IO_TIMEOUT", "25"))

SHARP_NAMES = ("pinnacle", "betfair exchange", "smarkets", "matchbook")

# ── POOL DE COMPTES (2026-08-28) ──────────────────────────────────────
# Le plan gratuit fait 500 req/jour et 2 bookmakers PAR COMPTE. Avec un seul
# compte, tennis/basketball/mma/hockey sortaient en « rythme de dépense » à
# chaque tick (mesuré le 2026-08-28 : 221/400 à 13:00, tout pour le foot).
# `ODDS_API_IO_KEYS` (CSV — app_secrets d'abord, puis l'environnement) ajoute
# des comptes ; `ODDS_API_IO_KEY` reste lue et forme le premier.
# Même contrat que core/odds_api.candidate_keys : ordonné, dédupliqué ; un
# compte refusé (401/403/429) est écarté pour le PROCESSUS et la MÊME
# requête est rejouée sur le suivant. Le budget est tenu PAR COMPTE (chaque
# compte a son propre plafond) ; le rythme de dépense porte sur le TOTAL et
# reste celui de core/daily_quota — aucune copie.
# ⚠️ Un compte suspendu pour abus l'est pour de bon (api-sports, 2026-08-20) :
# DAILY_BUDGET reste 400 sur 500 PAR compte, la marge ne se mange pas. Les
# bookmakers se choisissent compte par compte (scripts/odds_api_io_books.py
# --compte N) : une réponse est toujours servie par UN compte, donc les
# deux slots de chaque compte doivent porter des books utiles à eux seuls.
POOL_SECRET = "ODDS_API_IO_KEYS"
_REFUS = (401, 403, 429)          # le compte, pas la requête
_selected_cache: dict[str, list[str]] = {}
_dead_keys: dict[str, str] = {}


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
    add(get_secret("ODDS_API_IO_KEY"))
    # L'environnement rejoint TOUJOURS le pool, même quand app_secrets a une
    # valeur (même leçon qu'OddsAPI : une clé neuve en env restait invisible
    # derrière une clé périmée en table).
    add(os.environ.get(POOL_SECRET))
    add(os.environ.get("ODDS_API_IO_KEY"))
    for i in range(2, 10):
        add(os.environ.get(f"ODDS_API_IO_KEY_{i}"))
    return out


def _bucket(key: str) -> str:
    """Compteur journalier PAR COMPTE — une empreinte, jamais la clé : le
    nom du bucket finit dans la table meta."""
    return f"{QUOTA_BUCKET}_{hashlib.sha1(key.encode()).hexdigest()[:8]}"


def live_keys(keys: list[str]) -> list[str]:
    """Comptes ni refusés ce processus, ni à leur plafond du jour."""
    return [k for k in keys
            if k not in _dead_keys and daily_quota.spent(_bucket(k)) < DAILY_BUDGET]


def mark_dead(key: str, reason: str) -> None:
    _dead_keys[key] = reason


def _get(path: str, key: str, params: dict) -> tuple[int, object]:
    """(status, corps) — ne lève jamais. Compte la requête au budget du jour,
    au total ET au compte."""
    try:
        r = requests.get(f"{BASE_URL}/{path}", timeout=TIMEOUT,
                         params={**params, "apiKey": key})
    except Exception as e:
        log.warning("odds-api.io %s: %s", path, e)
        return 0, None
    daily_quota.add(QUOTA_BUCKET, 1)
    daily_quota.add(_bucket(key), 1)
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
    """Books actifs sur CE compte. `ODDS_API_IO_BOOKMAKERS` court-circuite
    l'appel réseau (pour tous les comptes) ; sinon le résultat est mémorisé
    par compte pour le processus."""
    forced = os.environ.get("ODDS_API_IO_BOOKMAKERS", "").strip()
    if forced:
        return [b.strip() for b in forced.split(",") if b.strip()]
    if key in _selected_cache and not force:
        return _selected_cache[key]
    _, body = _get("bookmakers/selected", key, {})
    books: list[str] = []
    if isinstance(body, dict):
        books = [str(b) for b in (body.get("bookmakers") or [])]
    elif isinstance(body, list):
        books = [str(b) for b in body]
    _selected_cache[key] = books
    return books


def reset_cache() -> None:
    """Oublie les books mémorisés et les comptes écartés (tests, rotation)."""
    _selected_cache.clear()
    _dead_keys.clear()


def _request(path: str, params_for, keys: list[str], sport: str) -> tuple[int, object, str | None]:
    """Une requête servie par le premier compte vivant ; un compte refusé est
    écarté et la MÊME requête rejouée sur le suivant. `params_for(key)` rend
    les paramètres (les books sont par compte), ou None si ce compte n'est
    pas exploitable. Rend (status, corps, compte utilisé)."""
    while True:
        vivants = live_keys(keys)
        if not vivants:
            return 0, None, None
        key = vivants[0]
        num = keys.index(key) + 1
        params = params_for(key)
        if params is None:
            mark_dead(key, "aucun bookmaker sélectionné")
            log.warning("odds-api.io[%s]: compte #%d : aucun bookmaker sélectionné — écarté "
                        "(scripts/odds_api_io_books.py --compte %d)", sport, num, num)
            continue
        status, body = _get(path, key, params)
        if status in _REFUS:
            mark_dead(key, f"HTTP {status}")
            log.warning("odds-api.io[%s]: compte #%d écarté (HTTP %d) — requête rejouée "
                        "sur le suivant (%d compte(s) vivant(s))",
                        sport, num, status, len(live_keys(keys)))
            continue
        return status, body, key


def _odd(val) -> float:
    try:
        f = float(val)
        return f if f > 1.01 else 0.0
    except (TypeError, ValueError):
        return 0.0


def _is_sharp(book: str) -> bool:
    low = book.lower()
    return any(s in low for s in SHARP_NAMES)


def echelle(lines: list, a_key: str, b_key: str) -> list[dict]:
    """TOUTES les lignes cotées des deux côtés, la plus équilibrée en tête.

    POURQUOI L'ÉCHELLE ENTIÈRE, ET PLUS SEULEMENT LA LIGNE PRINCIPALE
    ----------------------------------------------------------------
    Le book publie une douzaine de handicaps (hdp -3.75 à +3.75) et autant de
    totaux. Ce module n'en gardait qu'UN : celui dont les deux prix sont les
    plus proches — bonne heuristique pour désigner la ligne de référence du
    marché, mais elle s'applique ICI, sans rien savoir de ce que cote le book
    sharp en face. `core/matchbook.py` faisait exactement la même chose de son
    côté. Les deux sources choisissaient donc leur ligne principale
    SÉPARÉMENT, puis `run_engine._meme_ligne` refusait la paire dès que les
    deux choix différaient — ce qu'il DOIT faire (deux handicaps différents
    sont deux paris différents, cf. A6), mais sur une divergence que personne
    n'avait besoin de subir : le book cote aussi la ligne du sharp, on venait
    juste de la jeter.

    Mesuré le 2026-08-27 sur les matchs communs aux deux sources : 1 total sur
    2 et 0 spread sur 2 survivaient à cette comparaison.

    La ligne principale reste `echelle(...)[0]` — même heuristique, même
    résultat qu'avant quand aucun alignement n'est nécessaire. Ce qui change,
    c'est que le reste de l'échelle n'est plus perdu, et que
    `run_engine._aligner_sur_meme_ligne` peut y retrouver la ligne que le
    sharp cote vraiment.
    """
    out: list[dict] = []
    for row in lines or []:
        a, b = _odd(row.get(a_key)), _odd(row.get(b_key))
        if not a or not b:
            continue
        try:
            point = float(row.get("hdp"))
        except (TypeError, ValueError):
            continue
        out.append({a_key: a, b_key: b, "point": point})
    out.sort(key=lambda r: abs(r[a_key] - r[b_key]))
    return out


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
            ech = echelle(rows, "home", "away")
            if ech:
                out["spreads"] = {**ech[0], "away_point": -ech[0]["point"],
                                  "ladder": [{**r, "away_point": -r["point"]} for r in ech]}
        elif name == "totals":
            ech = echelle(rows, "over", "under")
            if ech:
                out["totals"] = {**ech[0], "ladder": ech}
    return out


def _line_shopping(soft: dict, autre: dict) -> None:
    """Meilleur prix par ISSUE entre deux books soft — 1X2, mais aussi
    handicaps et totaux, LIGNE PAR LIGNE.

    Le second book ne servait qu'au 1X2 : ses handicaps et ses totaux étaient
    jetés, et si le PREMIER book n'en cotait aucun, le match repartait sans
    spread ni total du tout. Or le plan gratuit d'odds-api.io autorise DEUX
    books simultanés (message d'erreur de l'API, relevé le 2026-08-27) : la
    moitié de la couverture soft disponible se perdait ici.

    Le prix se compare toujours À LIGNE ÉGALE. Retenir le meilleur prix
    toutes lignes confondues reviendrait à choisir un AUTRE pari parce qu'il
    est mieux payé — l'artefact exact qu'A6 a supprimé (voir
    `run_engine._meme_ligne`).
    """
    for k in ("1", "X", "2"):
        if autre.get("h2h", {}).get(k, 0) > soft.get("h2h", {}).get(k, 0):
            soft.setdefault("h2h", {})[k] = autre["h2h"][k]

    for marche, cotes in (("spreads", ("home", "away")), ("totals", ("over", "under"))):
        if not autre.get(marche):
            continue
        if not soft.get(marche):
            soft[marche] = autre[marche]
            continue
        par_ligne = {r["point"]: dict(r) for r in soft[marche].get("ladder", [])}
        for r in autre[marche].get("ladder", []):
            cible = par_ligne.get(r["point"])
            if cible is None:
                par_ligne[r["point"]] = dict(r)
                continue
            for c in cotes:
                if r.get(c, 0) > cible.get(c, 0):
                    cible[c] = r[c]
        ladder = sorted(par_ligne.values(),
                        key=lambda r: abs(r[cotes[0]] - r[cotes[1]]))
        soft[marche] = {**ladder[0], "ladder": ladder}


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
            _line_shopping(soft, parsed)
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
    keys = candidate_keys(api_key)
    if not keys:
        log.debug("odds-api.io: pas de clé (ODDS_API_IO_KEY / ODDS_API_IO_KEYS) — source ignorée")
        return []

    budget_total = DAILY_BUDGET * len(keys)
    used_before = daily_quota.spent(QUOTA_BUCKET)
    if used_before >= budget_total or not live_keys(keys):
        log.warning("odds-api.io[%s]: budget journalier atteint (%d/%d, %d compte(s)) "
                    "— cycle ignoré", sport, used_before, budget_total, len(keys))
        return []
    ouverture = daily_quota.paced_allowance(budget_total, CYCLE_COST)
    if used_before >= ouverture:
        log.info("odds-api.io[%s]: rythme de dépense — %d/%d dépensées, "
                 "l'ouverture de cette heure est %d. Le reste est gardé pour "
                 "les scans du soir : sans prix soft, un match sharp ne "
                 "produit aucun edge.", sport, used_before, budget_total, ouverture)
        return []

    now   = datetime.now(timezone.utc)
    until = now + timedelta(hours=hours_ahead)
    cap   = max_events or MAX_EVENTS
    def _params_events(k: str):
        # Vérifié AVANT le calendrier : un compte sans bookmaker ne peut rien
        # demander, autant ne pas lui payer la requête /events.
        if not selected_bookmakers(k):
            return None
        return {
            "sport": slug,
            "from": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "to":   until.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "limit": str(cap),
        }

    status, body, _ = _request("events", _params_events, keys, sport)
    if status != 200 or not isinstance(body, list):
        return []

    # `pending` = à venir. Les statuts live/settled/cancelled n'ont rien à
    # faire dans un scan pré-match.
    events = [e for e in body if str(e.get("status", "")).lower() == "pending"][:cap]
    if not events:
        log.info("odds-api.io[%s]: 0 match à venir dans les %dh", sport, hours_ahead)
        return []

    matches: list[dict] = []
    book_param = ""
    for i in range(0, len(events), MULTI_BATCH):
        if daily_quota.spent(QUOTA_BUCKET) >= budget_total or not live_keys(keys):
            log.warning("odds-api.io[%s]: budget épuisé en cours de cycle — "
                        "%d matchs conservés", sport, len(matches))
            break
        batch = events[i:i + MULTI_BATCH]
        ids = ",".join(str(e.get("id")) for e in batch)

        def _params(k: str, ids: str = ids):
            books = selected_bookmakers(k)
            return {"eventIds": ids, "bookmakers": ",".join(books)} if books else None

        status, body, key = _request("odds/multi", _params, keys, sport)
        if status != 200 or not isinstance(body, list):
            break
        book_param = ",".join(selected_bookmakers(key))
        for ev in body:
            m = _to_match(ev, sport, sport_id, draw)
            if m:
                matches.append(m)

    n_sharp = sum(1 for m in matches if m.get("odds_pinnacle"))
    log.info("odds-api.io[%s]: %d matchs (%d avec prix sharp) / %d à venir | "
             "books=%s | comptes=%d/%d | %d/%d req au total aujourd'hui",
             sport, len(matches), n_sharp, len(events), book_param,
             len(live_keys(keys)), len(keys),
             daily_quota.spent(QUOTA_BUCKET), budget_total)
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
    """(utilisable ?, détail) — pour scripts/ops.py sources. Un compte par
    ligne : un compte sans bookmaker ou refusé se voit ici, pas au scan."""
    keys = candidate_keys(api_key)
    if not keys:
        return False, "pas de clé (ODDS_API_IO_KEY / ODDS_API_IO_KEYS)"
    parts, ok_any = [], False
    for i, key in enumerate(keys, 1):
        status, body = _get("bookmakers/selected", key, {})
        if status != 200:
            parts.append(f"#{i}: HTTP {status}")
            continue
        ok_any = True
        books = (body or {}).get("bookmakers") if isinstance(body, dict) else body
        parts.append(f"#{i}: books={books} | {daily_quota.spent(_bucket(key))}/{DAILY_BUDGET}")
    total = daily_quota.spent(QUOTA_BUCKET)
    return ok_any, (f"{'OK' if ok_any else 'KO'} — {len(keys)} compte(s) · "
                    + " · ".join(parts)
                    + f" | total {total}/{DAILY_BUDGET * len(keys)} req aujourd'hui")
