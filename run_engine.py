"""
run_engine.py — PREDATOR PAIM v8.8 — Hunter Multi-Sport + Portfolio Balancer
Markets: h2h (NBA/Tennis) | spreads (NBA/Soccer) | totals (all)
Sharp filter: Prob. Sharp (Power devigged, see core/math_engine.py) >= threshold per market type
Pipeline: OddsAPI → Web Search (Groq/Tavily) → AI Estimator → AH0.0/ML/PS/OU → Edge → Balancer → Supabase
All timestamps : UTC/GMT — no local-time contamination.
"""
import hashlib
import json
import logging
import os
import random
import time
import signal
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

from core.db import (get_db, MissingCredentialsError,
                     log_to_ledger as _log_to_ledger,
                     is_unique_violation as _is_unique_violation)
from core.harvester import fetch_matches, fetch_pinnacle_prices, fetch_estimated_prices, fetch_betfair_prices
from core.ai_search import ai_dead as gemini_quota_dead
from core.closing_line import capture_from_exchange, capture_from_scan
from core.matchbook import fetch_matchbook_prices
# Appariement slate ↔ exchange : déplacé dans core/ le 2026-08-26 pour que
# core/closing_line.py puisse s'en servir sans importer la racine.
from core.exchange_match import lookup_exchange as _lookup_exchange
from core.api_sports import fetch_all as _api_sports_all
from core.odds_api_io import fetch_all as _odds_api_io_all
from core.titan007 import fetch_matches as _titan007_fetch
from core.math_engine import (to_binary, devig_bounds, is_round_number_line, devig as _devig,
                              dnb_leg_split as _dnb_leg_split,
                              executable_price as _executable_price)
from core.tax_engine import optimal_stake_fraction as _optimal_stake_fraction
from core.odds_api import fetch_odds, pool_status as _odds_pool_status, pool_counters as _odds_pool_counters
from core.scan_windows import SpendPolicy as _SpendPolicy
from core.constants import CLOSING_LINE_WINDOW_MIN as _CLOSING_LINE_WINDOW_MIN
from core.oracle import get_pinnacle_price, MAX_ORACLE_DEFAULT as _MAX_ORACLE_DEFAULT
from core.run_contract import terminer as _terminer_run, verdict_de_fin
from core.learning_layer import load_thresholds as _load_thresholds
from core.learning_layer import load_segment_thresholds as _load_segment_thresholds
from core.learning_layer import load_sport_ranking as _load_sport_ranking
from core.learning_layer import load_edge_ceilings as _load_edge_ceilings
from core.learning_layer import load_odds_ceilings as _load_odds_ceilings
from core.paim_engine import (
    compute_alpha, MIN_EDGE, strict_team_match,
    market_label, SHARP_PROB_BY_MARKET, calculate_consensus_price,
    correlation_group as _correlation_group, resolve_selection_side,
)
from core.constants import ELITE_EDGE as _ELITE_EDGE, SOCCER_ELITE_EDGE as _SOCCER_ELITE_EDGE, BASKETBALL_ELITE_EDGE as _BASKETBALL_ELITE_EDGE, risk_flag as _risk_flag, SUSPECT_EDGE as _SUSPECT_EDGE, KELLY_FRACTION as _KELLY_FRACTION, AH0_VALUE_THRESHOLD as _AH0_VALUE_THRESHOLD, PURGE_EDGE_FLOOR as _PURGE_EDGE_FLOOR, MLB_LINEUP_WINDOW_H as _MLB_LINEUP_WINDOW_H, PUSH_PROB_ROUND_LINE as _PUSH_PROB_ROUND_LINE, TAX_RATE as _TAX_RATE, BANKROLL_REF as _BANKROLL_REF, EV_EDGE_FLOOR as _EV_EDGE_FLOOR, RETIRED_SPORTS as _RETIRED_SPORTS, EXCHANGE_DIVERGENCE_PTS as _EXCHANGE_DIVERGENCE_DEFAULT
from core.tax_engine import suggest_system as _suggest_system, is_combo_tax_viable as _is_combo_tax_viable
import core.risk_manager as _risk_manager

load_dotenv()

# ── Deep-scan mode (DEEP_SCAN=1) ─────────────────────────────────────
# Triggered by .github/workflows/scan.yml (mode `deep`) or manually.
# Lifts per-sport quotas + scans 48h ahead with up to 100 events.
DEEP_SCAN    = os.environ.get("DEEP_SCAN",    "0") == "1"
GOLDEN_HOUR  = os.environ.get("GOLDEN_HOUR", "0") == "1"
GUERRILLA    = os.environ.get("GUERRILLA",   "0") == "1"  # skip OddsAPI → Tier 2 direct

# ── OddsAPI DÉCLARÉ OBSOLÈTE — décision opérateur du 2026-08-26 ──────
# Predator ne s'appuie plus sur une source de cotes PAYANTE. Le Tier 1 est
# éteint par défaut : chaque scan part directement sur les sources gratuites
# (api-sports, odds-api.io, titan007, Matchbook, harvest soft).
#
# POURQUOI un interrupteur plutôt qu'une suppression : `core/odds_api.py`
# n'est pas qu'une source, c'est aussi le VOCABULAIRE des sports — ses
# `SPORT_KEYS` sont les valeurs écrites dans `signals.sport` et relues par
# `api/index.py`. L'arracher casserait l'invariant des sport-keys (4 fichiers
# synchrones, cf. AUDIT.md §2) pour supprimer du code qui ne coûte plus rien
# une fois qu'on ne l'appelle plus.
#
# CE QUI MEURT AVEC LUI, en clair : les sports qu'AUCUNE source gratuite ne
# price (MMA/boxe, NFL, NCAAF, LdC/UEL, Euroleague, tennis par clés
# dynamiques) n'émettront plus rien, et la capture closing-line « en
# stop » sur le payload payant s'arrête — seul `run_closing_line.py` la
# fait encore. C'est le prix assumé de la sortie du payant.
#
# Réactivation explicite, sans autre changement : ODDS_API=1
#
# RALLUMÉ le 2026-09-01 (décision opérateur, nouvelle clé dans le pool) : le
# flag est posé par scripts/ci_scan_mode.py::TIER1_ENV pour standard et deep
# seulement (golden : 24 ticks/jour, trop cher). Le DÉFAUT reste 0 — verrouillé par tests/test_oddsapi_obsolete.py —
# pour qu'un `python run_engine.py` local ou un futur workflow ne dépense
# jamais un crédit sans l'avoir demandé.
ODDS_API_ENABLED = os.environ.get("ODDS_API", "0") == "1"
REPRICE      = os.environ.get("REPRICE",     "0") == "1"  # Matchbook seul vs slate soft en cache — zéro source payante
DEBUG_MODE   = os.environ.get("PREDATOR_DEBUG", "0") == "1"

# ── Mode FANTÔME — segments mesurés mais plus recommandés ────────────
#
# Un segment fantôme continue d'être scanné, persisté en base, réglé et
# appris ; il ne part simplement plus sur Telegram. C'est exactement ce que
# fait déjà le disjoncteur par sport plus bas (voir le bloc « Per-sport
# circuit breaker ») — même mécanisme, mais décidé par la mesure et non par
# une série noire passagère.
#
# POURQUOI ces deux-là — mesuré le 2026-08-04 sur ai_learning_ledger, 182
# paris réglés, après exclusion des lignes à match_time passé (voir le garde
# « MATCH PASSÉ » dans _emit) :
#
#   golden_hour (T-2h) : 67 paris, 39% de réussite pour 54,5% requis,
#                        ROI -29,2%, p=0,007. Écart avec la tranche 2-24h
#                        significatif (Fisher p=0,0098).
#   baseball           : 48 paris, 42% pour 56,5% requis, ROI -26,8%,
#                        p=0,027 — non établi après correction pour les 4
#                        sports testés (0,11), d'où le fantôme plutôt que le
#                        retrait sec : on cesse de miser sans cesser d'apprendre.
#
# Le fantôme est préféré à la coupure PARCE QU'il préserve l'information :
# couper le cron arrêterait aussi la collecte, et on ne saurait jamais si le
# segment se redresse. Pour lever un fantôme, retirer l'entrée ici et
# refaire l'analyse par tranche — la comparaison reste valide, les lignes
# ayant continué d'être réglées normalement.
#
# ATTENTION pour toute analyse ultérieure du ledger : ces paris sont en base
# comme les autres et RIEN ne les distingue au niveau de la ligne (pas de
# colonne dédiée, cohérent avec le disjoncteur existant). Les isoler = filtrer
# sur le segment ET sur created_at >= 2026-08-04.
#
# PRÉCISION AJOUTÉE LE 2026-08-06 — « sans cesser d'apprendre » ne vaut plus
# pour les SEUILS. core/learning_layer.py ne calcule désormais les planchers
# que sur la zone jouable (2-24h avant le coup d'envoi, voir playable_rows) :
# les paris golden_hour continuent d'être persistés, réglés et archivés au
# ledger, mais ils ne pèsent plus sur threshold_<sport>. C'était le point : ils
# faisaient monter les planchers de sports rentables à cause de pertes que le
# système ne prend plus. La collecte reste donc intacte pour une décision
# manuelle de réouverture — c'est exactement ce qui a permis de mesurer ce
# segment — mais l'ajustement automatique, lui, ne les voit plus.
SHADOW_SPORTS      = {"baseball"}
SHADOW_GOLDEN_HOUR = True

# ── Global Timeout Handler (Safety Net) ──────────────────────────────
# Prevents Engine from hanging GitHub Actions (5+ min on Tier 2/3 fallback)
# Installed inside run() (not here at module level) — signal.signal() only
# works in the main thread of the main interpreter; a module-level call
# raised ValueError the instant anything imported run_engine from a worker
# thread (a test runner, a dashboard route off the request thread, etc),
# before a single line of run()'s actual logic ever executed. See
# tests/test_run_engine_import.py.
from core.constants import GLOBAL_TIMEOUT, SCAN_TIMEOUTS

_budget_arme = GLOBAL_TIMEOUT      # renseigné par _arm_global_timeout, pour le message


class EngineTimeout(BaseException):
    """Le budget de temps du run est épuisé. Dérive de BaseException, PAS
    d'Exception : le moteur est truffé d'`except Exception` « jamais
    bloquants » (sources, IA, alias), et `core.net` retente sur TimeoutError
    comme sur une erreur réseau transitoire. Le 2026-08-28 15:50:56 le filet
    golden (600 s) a bien levé — et le run a continué 7 min de plus, avalé par
    la boucle d'alias IA, sous le verrou d'écriture. Un timeout qu'on peut
    attraper par accident n'est pas un timeout."""


def _timeout_handler(signum, frame):
    log.error("TIMEOUT: Engine exceeded %d seconds — exiting gracefully", _budget_arme)
    raise EngineTimeout(f"Global timeout ({_budget_arme}s) exceeded")


def _mode_courant() -> str:
    """La clé de mode, dans l'ORDRE DE PRIORITÉ de `run()`.

    REPRICE prime : si deux drapeaux sont posés ensemble par un dispatch
    manuel, le mode le plus restrictif l'emporte — même règle qu'en tête de
    `run()`, et c'est pour cela que cette fonction existe plutôt qu'une
    seconde chaîne de `if` recopiée à côté. Une règle en double finit toujours
    par diverger ; c'est la panne la plus fréquente de ce dépôt.
    """
    if REPRICE:
        return "reprice"
    if GUERRILLA:
        return "guerrilla"
    if GOLDEN_HOUR:
        return "golden"
    if DEEP_SCAN:
        return "deep"
    return "standard"


def _arm_global_timeout(mode: str | None = None) -> int:
    """Filet SIGALRM au mieux, dimensionné sur le MODE (D3).

    Une valeur unique de 540 s servait les cinq modes : neuf fois la médiane
    d'un tick golden, et moins que la durée normale d'un deep ou d'un
    guerrilla — 32990495899 est mort dessus en toutes lettres. Voir
    `core.constants.SCAN_TIMEOUTS` pour les mesures.

    Se dégrade en silence (avertissement, moteur sans borne dure) plutôt que
    de planter, sur les deux échecs documentés de `signal` : AttributeError
    (pas de SIGALRM — Windows) ou ValueError (pas le thread principal).

    Rend le budget effectivement armé, pour que l'appelant puisse le
    journaliser — un filet dont personne ne connaît la taille ne s'explique
    pas quand il se déclenche.
    """
    global _budget_arme
    _budget_arme = SCAN_TIMEOUTS.get(mode or _mode_courant(), GLOBAL_TIMEOUT)
    try:
        signal.signal(signal.SIGALRM, _timeout_handler)
        signal.alarm(_budget_arme)
    except (AttributeError, ValueError) as e:
        log.warning("Global timeout not installed (%s) — running without a hard timeout", e)
    return _budget_arme

# ── UTC logger ────────────────────────────────────────────────────────
_fmt = logging.Formatter(
    fmt="%(asctime)s UTC | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
_fmt.converter = time.gmtime          # Force UTC — ignore server local time
_handler = logging.StreamHandler()
_handler.setFormatter(_fmt)
log = logging.getLogger("PREDATOR")
log.setLevel(logging.DEBUG if DEBUG_MODE else logging.INFO)
log.addHandler(_handler)
log.propagate = False

if DEBUG_MODE:
    log.debug("DEBUG MODE ENABLED — verbose logging active")

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT  = os.environ.get("TELEGRAM_CHAT_ID")

ELITE_EDGE  = _ELITE_EDGE   # % — send Telegram alert (from core.constants)
_MAJOR_SPORTS = {"soccer", "basketball", "hockey", "baseball", "rugbyleague", "aussierules",
                 "americanfootball", "euroleague_basketball",   # Phase 2 : cap SUSPECT appliqué
                 "college_football"}                            # Phase 3 : marché liquide, un edge > 10 % = prix mal apparié

# Plafonds d'edge appris, par sport — rempli par run() depuis `meta`, lu par
# _emit(). Vide = aucun plafond appris, les bornes globales de constants.py
# s'appliquent seules.
_EDGE_CEILINGS: dict[str, float] = {}

# Plafonds de COTE appris. Ne s'activent que sur une bande PROUVÉE perdante
# (borne haute de Wilson sous le seuil de rentabilité) — au 2026-08-02 aucune
# ne l'était, donc ce dict reste vide et rien ne change.
_ODDS_CEILINGS: dict[str, float] = {}

# Fast mode (default): 20 events, tight quota — speed over coverage
# Deep mode (DEEP_SCAN=1): 100 events, wide quota — plus de matchs dans la
# MÊME fenêtre de 24h que le mode normal (les deux fenêtres ont été alignées
# le 2026-08-04, voir le bloc hours_ahead plus bas) : deep ne va plus chercher
# plus LOIN, il creuse plus PROFOND sur la même journée.
MAX_MATCHES = 100 if DEEP_SCAN else 50

# ── TTL des caches sports-hors-OddsAPI (heures) ──────────────────────
# Défauts calés sur le partage de TPD Groq avec le tick golden/le settlement.
# Ils sont surdimensionnés pour le table tennis : le slate ITTF tourne toutes
# les 30-60 min, donc à 4h de TTL un scan sur deux ne voyait qu'une carte déjà
# jouée. Le mode `guerrilla`, qui a son propre budget, les raccourcit par
# l'env (scripts/ci_scan_mode.py::MODE_ENV).
# TTL d'un résultat VIDE. eSports et sports alternatifs n'ont pas de feed
# Melbet dans ce flux : si la recherche rend zéro (clé Groq morte, par ex.),
# le vide reste en cache et le sport est muet jusqu'à expiration.
_TTL_EMPTY   = float(os.environ.get("CACHE_EMPTY_TTL_H",   "3"))
# TTL du slate soft photographié par les scans complets pour le mode REPRICE.
# Au-delà, le prix soft est trop vieux pour prétendre être jouable : on
# préfère un tick REPRICE muet à un edge calculé contre une cote fantôme.
_TTL_SOFT_SLATE = float(os.environ.get("CACHE_SOFT_SLATE_TTL_H", "4"))
# Coupe-circuit d'urgence si Matchbook devait mal se comporter en prod
# (géoblocage US non constaté en test, voir core/matchbook.py).
_MATCHBOOK_OFF = os.environ.get("MATCHBOOK_OFF", "") == "1"

# Nombre de repêchages oracle (1 appel IA chacun) quand la recherche groupée
# n'a pas trouvé de ligne Pinnacle pour un match. Le défaut vaut ZÉRO depuis le
# 2026-08-27 : un prix « Pinnacle » produit par un LLM ne peut pas servir de
# référence sharp. La valeur vit dans `core.oracle`, à côté du code qu'elle
# gouverne, plutôt qu'en dur ici — voir sa docstring pour le raisonnement et
# pour les deux chemins que ce réglage NE couvre pas.
_MAX_ORACLE = int(os.environ.get("MAX_ORACLE", str(_MAX_ORACLE_DEFAULT)))

# Divergence tolérée entre DEUX avis sharp indépendants (Pinnacle et
# l'exchange), en POINTS de probabilité. Au-delà, le match entier est refusé :
# voir `core.constants.EXCHANGE_DIVERGENCE_PTS` pour la mesure qui la fonde et
# pour ce qu'elle ne couvre pas encore.
_EXCHANGE_DIVERGENCE_PTS = float(
    os.environ.get("EXCHANGE_DIVERGENCE_PTS", str(_EXCHANGE_DIVERGENCE_DEFAULT)))

# Sports absents du plan OddsAPI : la recherche web est leur SEULE source de
# prix sharp. Le budget oracle doit leur revenir en premier — il se dépensait
# dans l'ordre de la liste, or SPORT_IDS énumère le foot en premier et le MMA
# en dernier, donc les 3 slots partaient toujours au foot. Run 30766186188 :
# les 6 combats UFC récupérés chez Melbet (Blachowicz, Rakic, de Ridder…) sont
# tous tombés en « Échec prix Sharp » sans qu'un seul appel oracle soit tenté.
# Plus AUCUN sport hors OddsAPI depuis le 2026-08-22 : eSports/tabletennis/
# volleyball/handball retirés (Phase 0), MMA passé sur flux OddsAPI réel
# (Phase 1). Conservé vide pour la priorité du Tier 3 (oracle) : tout sport
# ajouté ici passerait devant les autres dans la file de l'oracle.
_NO_ODDSAPI_SPORTS: frozenset = frozenset()

SPORT_EMOJI  = {
    "soccer": "⚽", "tennis": "🎾", "basketball": "🏀", "boxing": "🥊",
    "mma": "🥋", "darts": "🎯", "cricket": "🏏", "hockey": "🏒",
    "americanfootball": "🏈", "baseball": "⚾", "euroleague_basketball": "🏀",
    "rugby": "🏉", "rugbyleague": "🏉", "aussierules": "🦘",
    "college_football": "🏈",
}

# Golden Hour — T-120min — 7 sports à lag maximal et volume élevé (juin 2026)
# Budget : 72 exec/j (*/20) × 7 sports = 504 req/j max | ~15 120 req/mois.
# Sélection : Pinnacle+1XBet confirmés + mouvement de ligne pré-match élevé.
#
# WC 2026 (début 11/06) : 3-4 matchs/jour → fenêtres 2h très actives
# KBO/NPB : 09:00–13:00 UTC → lag Asie sur books EU = prime pour Predator
# Copa Lib : 21:00–23:00 UTC → SA evening, lag 1XBet bien documenté
# NBA/NHL Finals : 22:00–02:00 UTC → tip-off windows, mouvement max
GOLDEN_SPORT_KEYS = {
    # (Retiré 2026-08-06 — Coupe du Monde terminée, instruction opérateur.)
    "soccer_conmebol_copa_libertadores":    "soccer",      # Copa Lib — lag SA maximal
    "soccer_brazil_campeonato":             "soccer",      # Brasileirão — quotidien
    "soccer_usa_mls":                       "soccer",      # MLS — actif juin–août
    "soccer_argentina_primera_division":    "soccer",      # Liga Argentina — marché SA sharp
    "soccer_mexico_ligamx":                 "soccer",      # Liga MX — actif été
    "soccer_epl":                           "soccer",      # EPL — reprise 21/08, Pinnacle+1xBet ✓
    "soccer_spain_la_liga":                 "soccer",      # La Liga — reprise 16/08, Pinnacle+1xBet ✓
    "soccer_germany_bundesliga":            "soccer",      # Bundesliga — reprise 28/08, Pinnacle+1xBet ✓
    "soccer_italy_serie_a":                 "soccer",      # Serie A — reprise 22/08, Pinnacle+1xBet ✓
    "soccer_france_ligue_one":              "soccer",      # Ligue 1 — reprise 22/08, Pinnacle+1xBet ✓
    "basketball_nba":                       "basketball",  # NBA Finals
    "basketball_wnba":                      "basketball",  # WNBA — remplit le créneau NBA off-season
    "icehockey_nhl":                        "hockey",      # NHL Cup Finals
    "baseball_mlb":                         "baseball",    # MLB — 10+ matchs/jour
    "baseball_kbo":                         "baseball",    # KBO Corée — lag Asie ✓
    "baseball_npb":                         "baseball",    # NPB Japon — lag Asie ✓
    "aussierules_afl":                      "aussierules", # AFL — fenêtre AU morning
    "rugbyleague_nrl":                      "rugbyleague", # NRL — fenêtre AU evening
    "mma_mixed_martial_arts":               "mma",         # cartes ven-dim ; 0 crédit hors carte (pré-vol)
    "boxing_boxing":                        "boxing",      # idem
    # Phase 2 — 0 crédit tant que la saison/phase de ligue n'a pas commencé
    "americanfootball_nfl":                 "americanfootball",
    "soccer_uefa_champs_league":            "soccer",
    "soccer_uefa_europa_league":            "soccer",
    "basketball_euroleague":                "euroleague_basketball",
    # Phase 3 — NCAAF (tennis : clés dynamiques injectées par fetch_odds)
    "americanfootball_ncaaf":               "college_football",
}

# Portfolio Balancer — quotas max par sport par scan (6 sport-types actifs uniquement,
# pas à confondre avec les 19 SPORT_KEYS d'odds_api.py — voir constants.py KELLY_FRACTION)
# Baseball élevé : MLB+KBO+NPB = 3 ligues simultanées (~19 events/fetch)
# Soccer élevé  : FIFA WC 2026 + Copa Lib + Brasileirão + MLS
_QUOTA_FAST = {
    "soccer":      20,   # WC(5)+Copa Lib(2)+Brasileirão(2)+MLS(2)
    "baseball":    10,   # MLB(5) + KBO(3) + NPB(2)
    "basketball":   8,   # NBA Finals
    "hockey":       6,   # NHL Cup Finals
    "rugbyleague":  5,   # NRL
    "aussierules":  5,   # AFL
    "mma":          4,   # cartes UFC/PFL — flux OddsAPI depuis le 2026-08-22
    "boxing":       2,   # marché mince
    "americanfootball":      6,   # NFL — ~16 matchs/semaine, concentrés dim.
    "euroleague_basketball": 6,   # jeu/ven
    "college_football":      6,   # NCAAF — 50+ matchs/week-end, concentrés sam.
    "tennis":                8,   # Slams : 64–128 matchs par tour
}
_QUOTA_DEEP = {
    "soccer":      30,
    "baseball":    16,
    "basketball":  12,
    "hockey":       8,
    "rugbyleague":  8,
    "aussierules":  8,
    "mma":          6,
    "boxing":       3,
    "americanfootball":      10,
    "euroleague_basketball":  8,
    "college_football":      10,
    "tennis":                12,
}
SPORT_QUOTA = _QUOTA_DEEP if DEEP_SCAN else _QUOTA_FAST
# Telegram report order — sports les plus générateurs de signaux en tête
_SPORT_ORDER = ["soccer", "basketball", "hockey", "baseball", "americanfootball",
                "college_football", "tennis",
                "euroleague_basketball", "rugbyleague", "aussierules", "mma", "boxing"]

# Sessions marché (UTC) — alignées sur les fenêtres d'inefficience
_SESSIONS = {
    (6,  12): "EU-OPEN  📈",   # 06:00–11:59 — KBO/NPB morning + lignes EU fraîches
    (12, 18): "EU-MID   ⚡",   # 12:00–17:59 — SA + WC afternoon + MLB opening
    (18, 22): "EU-CLOSE 🎯",   # 18:00–21:59 — Copa/WC soirée + NBA/NHL pré-match
}

def _market_session(hour_utc: int) -> str:
    for (start, end), label in _SESSIONS.items():
        if start <= hour_utc < end:
            return label
    return "OVERNIGHT 🌙"      # 22:00–05:59 UTC — NBA/NHL tip-off + MLB late


# ── helpers ──────────────────────────────────────────────────────────

def _telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        log.warning("Telegram non configuré — message non envoyé")
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        r = requests.post(url, json={"chat_id": TELEGRAM_CHAT, "text": text, "parse_mode": "Markdown"}, timeout=10)
        if r.status_code != 200:
            log.error("Telegram HTTP %d: %s", r.status_code, r.text[:300])
        else:
            log.info("Telegram envoyé (%d chars)", len(text))
    except Exception as e:
        log.error("Telegram: %s", e)


def _refresh_ai_catalogues() -> None:
    """Découverte des catalogues IA au démarrage du run (mission 4).

    C'est ICI que se détecte la panne silencieuse : un fournisseur dont le
    modèle préféré a disparu bascule sur le suivant, et une lane qui tombe
    sous deux fournisseurs sains déclenche une alerte Telegram. Sans ce
    passage, un repli mort reste mort sans que personne ne le sache — c'était
    le cas d'OpenRouter jusqu'au 2026-08-22 (modèle `:free` retiré du
    catalogue, appels perdus à chaque run).

    Jamais bloquant : un routeur en panne ne doit pas empêcher un scan.
    """
    try:
        from core.ai_router import refresh_catalogues
        report = refresh_catalogues(alert=_telegram)
        actifs = [f"{n}={v['model']}" for n, v in report["providers"].items()]
        log.info("IA: %d fournisseur(s) actif(s) — %s",
                 len(actifs), " | ".join(actifs) or "aucun")
    except Exception as e:
        log.warning("IA: découverte des catalogues impossible (%s)", e)


# Columns optional at DB level — strip only as last-resort fallback
_OPTIONAL_COLS = {"selection_name", "kelly_pct", "advice", "sharp_sources", "consensus_score", "correlation_group"}


def _save(sb, signal) -> bool:
    """Écrit un signal : INSERT, et sur collision d'unicité, UPDATE ciblé.

    C'ÉTAIT UN SELECT PUIS UN UPDATE-OU-INSERT (jusqu'au 2026-08-27, B2).
    Entre la lecture et l'écriture, rien ne tenait : deux runs qui se
    chevauchent — et ils se chevauchent, le scan standard et le tick golden
    partagent des ligues — lisaient tous les deux « aucune ligne », puis
    inséraient tous les deux. Le dashboard affichait le même pari deux fois,
    `_portfolio_balance` le comptait deux fois dans son quota par sport, et
    `risk_manager` doublait son exposition sans le savoir. Aucune erreur
    n'était levée : le doublon était parfaitement valide pour le schéma.

    Désormais c'est la BASE qui arbitre, via l'index unique partiel de
    `sql/migrate_v10_7_signals_unique_active.sql`. On tente l'INSERT ; s'il
    est refusé pour violation d'unicité, c'est qu'une ligne ACTIVE existe déjà
    pour ce (match_id, market_key) et on la met à jour. La décision ne repose
    plus sur une lecture qui peut être périmée à l'instant où on s'en sert.

    ⚠️ POURQUOI PAS `upsert(on_conflict=…)`. PostgreSQL n'infère un index
    unique PARTIEL comme cible de `ON CONFLICT` que si l'ordre porte lui-même
    le prédicat de l'index ; PostgREST ne prend que des noms de colonnes et
    n'a aucun moyen de le transmettre — un upsert sur cet index échouerait en
    42P10. Or l'index DOIT être partiel : sans `status='active'`, une ligne
    déjà réglée empêcherait tout nouveau signal sur le même match. Le détour
    INSERT-puis-UPDATE donne le même effet et la même absence de course.

    ⚠️ SANS `match_id` NI `market_key`, aucune contrainte n'existe — l'index
    les exclut, parce que `_emit` écrit `match_id=""` par défaut et que deux
    matchs sans identifiant se seraient écrasés l'un l'autre. Ces lignes
    gardent l'ancien chemin, course comprise : documenté plutôt que masqué.
    Mesuré le 2026-08-27 : 90 lignes actives, 0 sans `match_id`.

    Le payload de `_emit` ne nomme ni `id`, ni `created_at`, ni les colonnes
    de clôture : elles survivent mécaniquement à un UPDATE.
    """
    from core.constants import MAX_DB_RETRIES, DELAY_DB_RETRY
    from core.db import update_signal_fields

    payload = dict(signal)
    # MAPPING EXPLICITE — le moteur nomme le prix `executable_odd` depuis le
    # 2026-08-27, parce que c'est ce qu'il est : la cote qu'on peut réellement
    # jouer. La COLONNE reste `xbet_odd` : la renommer demanderait une
    # migration et casserait le dashboard, `closing_line`, `settlement` et
    # `audit_engine`, qui relisent tous cette colonne. La traduction se fait
    # ICI, au point unique de persistance, plutôt qu'en laissant les deux noms
    # cohabiter dans le moteur — c'est précisément la confusion qu'on retire.
    if "executable_odd" in payload:
        payload["xbet_odd"] = payload.pop("executable_odd")

    mid  = payload.get("match_id", "")
    mkey = payload.get("market_key", "")
    sig_label = f"{payload.get('match', '?')}/{payload.get('market', '?')}"
    protege = bool(mid and mkey)

    def _rafraichir():
        """UPDATE de la ligne ACTIVE de ce (match_id, market_key).

        True si elle a été mise à jour, False sur échec d'écriture, et None si
        AUCUNE ligne active ne correspond — cas réel : elle vient d'être
        réglée entre notre INSERT refusé et cet UPDATE. L'emplacement actif
        est alors libre et il faut retenter l'INSERT, pas abandonner le signal.
        """
        champs = {k: v for k, v in payload.items()
                  if k not in ("match_id", "market_key")}
        try:
            res = (sb.table("signals").update(champs)
                   .eq("status", "active").eq("match_id", mid)
                   .eq("market_key", mkey).execute())
        except Exception as e:
            log.error("Supabase update (signal %s): %s", sig_label, str(e)[:100])
            return False
        if not (res.data or []):
            return None
        if DEBUG_MODE:
            log.debug("✓ Signal refreshed: %s [edge=%.2f%%]",
                      sig_label, payload.get("edge_pct", 0))
        return True

    def _rafraichir_sans_contrainte() -> bool:
        """Chemin des lignes SANS identifiant, que l'index ne protège pas : on
        retombe sur l'ancien select-then-update, course comprise. Le dire vaut
        mieux que de laisser croire que tout est couvert."""
        try:
            rows = (sb.table("signals").select("id").eq("status", "active")
                    .eq("match", payload["match"])
                    .eq("market", payload.get("market", ""))
                    .order("created_at", desc=True).limit(1).execute().data) or []
        except Exception as e:
            if DEBUG_MODE:
                log.debug("Supabase select (signal %s): %s", sig_label, str(e)[:80])
            return False
        if not rows:
            return False
        return update_signal_fields(sb, rows[0]["id"], payload,
                                    optional_cols=frozenset(_OPTIONAL_COLS))

    for attempt in range(1, MAX_DB_RETRIES + 1):
        if not protege and _rafraichir_sans_contrainte():
            return True
        try:
            sb.table("signals").insert(payload).execute()
            if DEBUG_MODE:
                log.debug("✓ Signal saved: %s [edge=%.2f%%]",
                          sig_label, payload.get("edge_pct", 0))
            return True
        except Exception as e:
            err = str(e)

            # La ligne active existe déjà : c'est le cas NOMINAL d'un re-scan,
            # pas une panne.
            if protege and _is_unique_violation(err):
                issue = _rafraichir()
                if issue is True:
                    return True
                if issue is None and attempt < MAX_DB_RETRIES:
                    continue      # réglée entre-temps, l'emplacement est libre
                if attempt < MAX_DB_RETRIES:
                    time.sleep(DELAY_DB_RETRY)
                    continue
                log.error("Supabase update FAILED after %d retries (signal %s)",
                          MAX_DB_RETRIES, sig_label)
                return False

            # Erreurs transitoires : on retente.
            if "FATAL" in err or "connection" in err.lower() or "timeout" in err.lower():
                if attempt < MAX_DB_RETRIES:
                    log.warning("Supabase transient error (signal %s, attempt %d/%d): %s",
                                sig_label, attempt, MAX_DB_RETRIES, err[:60])
                    time.sleep(DELAY_DB_RETRY)
                    continue

            # Schéma en retard : on réessaie sans les colonnes optionnelles.
            if "does not exist" in err or "column" in err.lower():
                core = {k: v for k, v in payload.items() if k not in _OPTIONAL_COLS}
                try:
                    sb.table("signals").insert(core).execute()
                    log.warning("Signal saved (schema fallback): %s", sig_label)
                    return True
                except Exception as e2:
                    log.error("Supabase insert FAILED after retry (signal %s): %s",
                              sig_label, str(e2)[:80])
            else:
                log.error("Supabase insert FAILED (signal %s): %s", sig_label, err[:80])

            return False

    return False


def _get_cached(sb, key: str, ttl_hours: float, empty_ttl_hours: float = 3.0):
    """Return cached list from Supabase meta if within TTL, else None.

    Un résultat VIDE est un résultat : il se met en cache lui aussi, avec un
    TTL plus court (`empty_ttl_hours`). Sans ça, une recherche web qui ne
    renvoie rien (quota Groq mort, pas d'événement UFC ce jour-là…) n'était
    jamais mémorisée, donc chacun des ~62 runs quotidiens la relançait :
    le 2026-07-22, MMA/Search partait à chaque tick Golden Hour (48/jour,
    ~3,5k tokens l'appel) et vidait à lui seul les 100 000 TPD de
    llama-3.3-70b-versatile avant que l'audit ait pu settler quoi que ce soit.
    """
    try:
        row = sb.table("meta").select("value, updated_at").eq("key", key).maybe_single().execute()
        if not row.data:
            return None
        updated = datetime.fromisoformat(row.data["updated_at"])
        age_h = (datetime.now(timezone.utc) - updated).total_seconds() / 3600
        data = json.loads(row.data["value"])
        ttl = ttl_hours if data else min(empty_ttl_hours, ttl_hours)
        if age_h < ttl:
            log.info("Cache HIT [%s] — age %.1fh < %.0fh TTL (%d items)", key, age_h, ttl, len(data))
            return data
    except Exception as e:
        log.debug("Cache get [%s]: %s", key, e)
    return None


def _set_cached(sb, key: str, value: list):
    """Store list in Supabase meta table."""
    try:
        sb.table("meta").upsert({
            "key":        key,
            "value":      json.dumps(value),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }, on_conflict="key").execute()
        log.debug("Cache SET [%s] — %d items", key, len(value))
    except Exception as e:
        log.warning("Cache set [%s]: %s", key, e)


# ── Slate soft en cache pour le mode REPRICE ─────────────────────────────
# Chaque scan COMPLET photographie son slate soft dans meta.cache_soft_slate ;
# le mode REPRICE (horaire, gratuit) le relit et le recompare à un prix sharp
# Matchbook FRAIS. Le côté rare du pipeline est le soft (budgets journaliers) ;
# le sharp est gratuit — c'est l'équivalent d'un « odds screen » de pro.
_SLATE_KEYS = ("id", "match", "home", "away", "league", "sport", "sport_id",
               "commence_time", "odds_1xbet", "totals_1xbet", "spreads_1xbet",
               "_soft_source")

# Le book publie une douzaine de handicaps et autant de totaux, et REPRICE a
# besoin de cette échelle : c'est elle qui permet de retrouver la ligne du
# Matchbook FRAIS dans le slate soft en cache (`_aligner_sur_meme_ligne`).
# Mais `_trim_soft_slate` existe pour BORNER le blob TEXT de meta, et une
# échelle entière × deux marchés × soixante matchs l'y ferait entrer par la
# fenêtre. On garde les lignes les plus ÉQUILIBRÉES, qui sont en tête : la
# ligne de référence d'un book sharp en fait toujours partie, les extrêmes
# (1.01 contre 8.60) ne sont la principale de personne.
_SLATE_LADDER_MAX = 8


def _trim_soft_slate(matches: list) -> list:
    """Slate soft minimal pour REPRICE — borne la taille du blob TEXT de meta.

    `odds_pinnacle` n'est sérialisé QUE s'il est réel et non-exchange
    (Pinnacle extrait d'api-sports/Titan007) : un prix posé par
    `_enrich_from_exchange` (`_exchange`) ou estimé par l'IA (`_estimated`)
    est exclu, sinon `_enrich_from_exchange` le verrait comme valide au tick
    suivant et SAUTERAIT le re-pricing — le sharp resterait figé 4h, l'edge
    serait calculé contre une référence morte. `totals_pinnacle`/
    `spreads_pinnacle` et `_oracle_price` tombent pour la même raison
    (absents de _SLATE_KEYS)."""
    out = []
    for m in matches:
        if not m.get("odds_1xbet"):
            continue                    # sans prix soft, rien à repricer
        row = {k: m[k] for k in _SLATE_KEYS if m.get(k) is not None}
        for cle in ("totals_1xbet", "spreads_1xbet"):
            if row.get(cle, {}).get("ladder"):
                row[cle] = {**row[cle], "ladder": row[cle]["ladder"][:_SLATE_LADDER_MAX]}
        pin = m.get("odds_pinnacle")
        if pin and not m.get("_estimated") and not m.get("_exchange"):
            row["odds_pinnacle"] = pin
        out.append(row)
    return out


# ── Horodatages meta : coupe-circuit harvest + alertes dédupliquées ─────
#
# 10-20 août 2026 : clé OddsAPI à 0 crédit, LineFeed 1xbet/Melbet injoignable,
# Tavily au plafond mensuel (432). Chacun des ~40 runs/jour relançait quand
# même le harvest web complet, brûlait les 100k tokens/jour des 3 clés Groq
# pour rien — et privait le SETTLEMENT (qui a besoin de Groq pour les scores)
# de tout budget. Le signal « 0 matchs — Melbet inaccessible » partait à
# chaque run sans jamais dire la vraie cause (clé OddsAPI morte), et
# personne n'a tourné la clé pendant dix jours.
#
# D'où : (1) un harvest qui n'a rien rendu n'est pas retenté avant
# HARVEST_EMPTY_TTL_H ; (2) les alertes portent la cause et ne se répètent
# pas avant _ALERT_TTL_H. Les deux vivent dans `meta` (clé → ISO timestamp).
_HARVEST_EMPTY_TTL_H = float(os.environ.get("HARVEST_EMPTY_TTL_H", "3"))
_ALERT_TTL_H         = float(os.environ.get("ALERT_TTL_H", "6"))


def _meta_stamp_age_h(sb, key: str) -> float | None:
    """Âge (heures) de l'horodatage ISO stocké dans meta[key] ; None si absent."""
    try:
        row = sb.table("meta").select("value").eq("key", key).maybe_single().execute()
        raw = (row.data or {}).get("value") if row and row.data else None
        if not raw:
            return None
        ts = datetime.fromisoformat(str(raw).strip('"').replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - ts).total_seconds() / 3600
    except Exception as e:
        log.debug("meta stamp get [%s]: %s", key, e)
        return None


def _meta_stamp(sb, key: str, value: str | None) -> None:
    """Pose (ISO maintenant) ou efface (None → "") l'horodatage meta[key]."""
    try:
        sb.table("meta").upsert({
            "key":        key,
            "value":      value if value is not None else "",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }, on_conflict="key").execute()
    except Exception as e:
        log.warning("meta stamp set [%s]: %s", key, e)


def _harvest_recently_empty(sb) -> float | None:
    """Âge de la dernière tentative Tier 2 vide si elle date de moins de
    HARVEST_EMPTY_TTL_H, sinon None (= on peut retenter)."""
    if not sb:
        return None
    age = _meta_stamp_age_h(sb, "harvest_empty_at")
    return age if age is not None and age < _HARVEST_EMPTY_TTL_H else None


def _note_harvest_result(sb, found: list) -> None:
    if not sb:
        return
    _meta_stamp(sb, "harvest_empty_at",
                None if found else datetime.now(timezone.utc).isoformat())


def _alert_once(sb, key: str, text: str, ttl_h: float = _ALERT_TTL_H) -> bool:
    """Telegram `text` au plus une fois par `ttl_h` (horodatage dans meta).
    Sans Supabase : envoie (mieux vaut un doublon qu'un silence)."""
    if sb:
        age = _meta_stamp_age_h(sb, key)
        if age is not None and age < ttl_h:
            log.info("Alerte [%s] déjà envoyée il y a %.1fh — silence", key, age)
            return False
    _telegram(text)
    if sb:
        _meta_stamp(sb, key, datetime.now(timezone.utc).isoformat())
    return True


_POOL_ALERT_TIERS = ((5.0, "alert_oddsapi_pool_5", "🔴"), (20.0, "alert_oddsapi_pool_20", "🟠"))


def _alert_oddsapi_pool_levels(sb) -> str | None:
    """Surveillance du quota OddsAPI — invisible (le widget de la page Sys a
    été retiré le 2026-08-22) mais PAS muette : une ligne de log à chaque
    run, et une alerte Telegram quand le pool passe sous 20 % puis sous 5 %,
    UNE seule par palier et par 24 h (dédup meta via _alert_once). Rappel de
    l'incident du 10→20 août 2026 : une clé à 0 crédit pendant dix jours sans
    que personne ne le voie. Rend la clé d'alerte envoyée, ou None."""
    c = _odds_pool_counters()
    if c.get("pct") is None:
        log.info("Quota OddsAPI : inconnu (aucune réponse OddsAPI observée ce run)")
        return None
    log.info("Quota OddsAPI : %d restantes / %d (%.1f%%) — clé active",
             c["remaining"], c["total"], c["pct"])
    for threshold, key, icon in _POOL_ALERT_TIERS:
        if c["pct"] < threshold:
            sent = _alert_once(
                sb, key,
                f"{icon} *OddsAPI : pool sous {threshold:.0f}%* — {c['remaining']} crédits "
                f"restants sur {c['total']} ({c['pct']:.1f}%).\n"
                f"Fenêtres favorables et closing line restent prioritaires ; le fond "
                f"s'espace (core/scan_windows). Ajouter une clé : "
                f"`python scripts/rotate_odds_key.py --add <clé>`",
                ttl_h=24.0)
            return key if sent else None
    return None


def _sports_with_imminent_signals(sb, now) -> set[str]:
    """Sport-types ayant un signal ACTIF à moins de CLOSING_LINE_WINDOW_MIN du
    coup d'envoi : leurs ligues ne sont jamais espacées par la politique de
    dépense — le scan sert la capture de closing line (capture_from_scan),
    prioritaire sur l'économie de fond."""
    if not sb:
        return set()
    try:
        horizon = (now + timedelta(minutes=_CLOSING_LINE_WINDOW_MIN)).isoformat()
        res = (sb.table("signals").select("sport")
               .eq("status", "active")
               .gte("match_time", now.isoformat())
               .lte("match_time", horizon)
               .limit(500).execute())
        return {r.get("sport") for r in (res.data or []) if r.get("sport")}
    except Exception as e:
        log.debug("imminent signals: %s", e)
        return set()


def _build_spend_policy(sb, now):
    """Politique de dépense OddsAPI (core/scan_windows) adossée aux
    horodatages meta `scan_paid_<ligue>`. Sans Supabase : None (on paie
    comme avant — mieux vaut un crédit de trop qu'un trou de couverture)."""
    if not sb:
        return None

    def _age_min(sport_key: str):
        age_h = _meta_stamp_age_h(sb, f"scan_paid_{sport_key}")
        return None if age_h is None else age_h * 60.0

    def _note(sport_key: str):
        _meta_stamp(sb, f"scan_paid_{sport_key}", datetime.now(timezone.utc).isoformat())

    return _SpendPolicy(_age_min, _note,
                        exempt_sports=_sports_with_imminent_signals(sb, now),
                        log=log)


_SYSTEM_ALERT_TTL_H = float(os.environ.get("SYSTEM_ALERT_TTL_H", "6"))


def _dedup_systems_for_telegram(sb, systems: list) -> list:
    """Écarte les systèmes déjà annoncés il y a moins de _SYSTEM_ALERT_TTL_H.

    Nécessaire depuis le mode REPRICE : le même slate est re-scanné chaque
    heure, donc le même combo repasserait par _telegram_systems ~23x/jour.
    La clé identifie le CONTENU du pari (jambes triées), pas le tick : un
    combo différent — nouvelle jambe, autre sélection — repart normalement.
    Sans Supabase, tout passe (mieux vaut un doublon qu'un silence)."""
    if not sb or not systems:
        return systems
    fresh = []
    for sys_ in systems:
        fingerprint = "|".join(sorted(
            f"{leg.get('match_id') or leg.get('match', '?')}:"
            f"{leg.get('market_key', '?')}:{leg.get('selection_name', '?')}"
            for leg in sys_.get("legs", [])))
        key = "alert_system_" + hashlib.md5(fingerprint.encode()).hexdigest()[:16]
        age = _meta_stamp_age_h(sb, key)
        if age is not None and age < _SYSTEM_ALERT_TTL_H:
            log.info("Système déjà annoncé il y a %.1fh — silence (%s)",
                     age, fingerprint[:60])
            continue
        _meta_stamp(sb, key, datetime.now(timezone.utc).isoformat())
        fresh.append(sys_)
    return fresh


def _alert_oddsapi_pool_if_dead(sb) -> None:
    """Tier 1 vide : si c'est parce que TOUTES les clés du pool sont mortes,
    le dire en clair — c'est une action humaine, pas une panne transitoire."""
    st = _odds_pool_status()
    if st["total"] == 0:
        _alert_once(sb, "alert_oddsapi_nokey",
                    "🔑 *OddsAPI : aucune clé configurée* — ni `ODDS_API_KEYS`/`ODDS_API_KEY` "
                    "dans app_secrets ni dans l'environnement. Tier 1 à l'arrêt.")
    elif st["live"] == 0:
        _alert_once(sb, "alert_oddsapi_pool_dead",
                    f"🔑 *OddsAPI : {st['dead']}/{st['total']} clé(s) épuisée(s)/invalide(s)* "
                    f"({st['reason']}).\nTier 1 à l'arrêt jusqu'à rotation :\n"
                    f"`python scripts/rotate_odds_key.py --add <nouvelle_clé>`\n"
                    f"(plusieurs clés = bascule automatique, plus de scan perdu)")


def _prob_home(block: dict) -> float:
    """Probabilité DÉVIGORISÉE du côté domicile pour un carnet 1X2 (ou 1-2).

    Sert à comparer deux sources sharp entre elles, et rien d'autre : côté
    sharp, dévigoriser est exactement ce qu'on cherche à faire (estimer une
    probabilité), à l'inverse du côté soft — voir `core.math_engine`.
    Rend 0.0 sur carnet inexploitable.
    """
    o1 = float(block.get("1") or 0)
    o2 = float(block.get("2") or 0)
    ox = float(block.get("X") or 0)
    if o1 <= 1.01 or o2 <= 1.01:
        return 0.0
    probs = _devig([o1, ox, o2]) if ox > 1.01 else _devig([o1, o2])
    return probs[0] if probs else 0.0


def _sharp_divergence_pts(a: dict, b: dict) -> float | None:
    """Écart entre deux carnets SHARP, en POINTS de probabilité.

    En points et non en pourcentage relatif : un seuil relatif crie au loup
    sur tout outsider (0,02 → 0,03 est +50 % relatif mais 1 point réel) et
    reste muet sur les favoris, où le point de probabilité coûte le plus cher.
    C'est la même leçon que `core/source_adapter.py`.

    Rend None quand la comparaison n'est pas possible — auquel cas on ne juge
    PAS : une source illisible n'est pas une source en désaccord.
    """
    pa, pb = _prob_home(a), _prob_home(b)
    if pa <= 0.0 or pb <= 0.0:
        return None
    return round(abs(pa - pb) * 100, 3)


def _poser_lignes_sharp(m: dict, bf: dict, log) -> None:
    """Pose les totals/handicaps de l'exchange sur le match, s'ils manquent.

    POURQUOI CE GESTE MANQUAIT À LA CONTRE-EXPERTISE (2026-08-27)
    ------------------------------------------------------------
    Ces deux affectations vivaient dans le seul rôle BOUCHE-TROU de
    `_enrich_from_exchange`. Conséquence : dès qu'un match avait DÉJÀ un prix
    sharp 1X2 — ce que servent titan007 et la recherche web sur la quasi-
    totalité du foot — l'enrichissement partait en contre-expertise, et les
    totals/handicaps de l'exchange étaient jetés au passage.

    Or aucune autre source ne les cote : titan007 ne rend que du 1X2, et le
    plan gratuit d'odds-api.io ne sert AUCUN book sharp (« sharp or exchange
    books are only available on our paid plans », relevé le 2026-08-27).
    `totals_pinnacle` et `spreads_pinnacle` restaient donc vides, la garde
    d'entrée de `run()` (« les deux côtés ou rien ») n'était jamais franchie,
    et `_process_totals`/`_process_spreads` n'étaient JAMAIS APPELÉS. Deux
    marchés sur trois étaient morts, en silence.

    Symptôme qui aurait dû alerter : le run du 2026-08-27 19:20 ne porte pas
    un seul `LINESKIP` — alors que Matchbook cotait ce jour-là 55 totals et
    40 handicaps. Une garde qui ne refuse jamais rien, ici, n'était pas
    franchie : elle n'était pas ATTEINTE.

    N'écrase jamais un prix sharp déjà posé par une autre source.
    """
    poses = []
    for cle, cible in (("totals", "totals_pinnacle"), ("spreads", "spreads_pinnacle")):
        if bf.get(cle) and not m.get(cible):
            m[cible] = bf[cle]
            poses.append(cle)
    if poses:
        log.info("LIGNES  | %s — %s pose %s (seule référence sharp sur ces marchés)",
                 m.get("match", "?"), bf.get("_source", "exchange"), " + ".join(poses))


def _enrich_from_exchange(items: list, prices: dict, log) -> int:
    """Confronte l'exchange au prix sharp, et le pose quand il n'y en a pas.

    DEUX RÔLES, et c'est le changement du 2026-08-27 (A5).

    1. CONTRE-EXPERTISE — le rôle qui compte. Jusqu'ici cette fonction faisait
       `continue` dès qu'un prix sharp existait : Matchbook n'était consulté
       que sur les matchs SANS Pinnacle. Or api-sports sert Pinnacle sur 100 %
       de ses matchs foot, et 100 % des signaux sont du foot — l'exchange
       était donc écarté PRÉCISÉMENT sur les matchs qui portent les signaux.
       Câblé en bouche-trou, il ne pouvait pas faire le seul travail qui
       compte : repérer un Pinnacle PÉRIMÉ, qui est la fabrique à faux edge.
       Un prix sharp périmé produit un edge qui n'existe pas, et rien en aval
       ne peut le distinguer d'un vrai.
       Désormais, quand les DEUX prix existent :
         · divergence > `_EXCHANGE_DIVERGENCE_PTS` points de probabilité → on
           REFUSE le match entier (`_sharp_conflict`). Deux avis sharp
           indépendants qui se contredisent ne peuvent pas être tous les deux
           à jour ; on ne choisit pas lequel croire, le désaccord EST
           l'information ;
         · sinon → l'exchange entre au CONSENSUS (`odds_exchange`) sans jamais
           écraser Pinnacle, qui reste la référence.

    2. BOUCHE-TROU — inchangé. Sans prix sharp (ou avec un prix seulement
       ESTIMÉ par l'IA), l'exchange le pose : c'est ce qui rend un edge
       calculable sur les matchs d'odds-api.io.

    Appelée DEUX fois par scan, et c'est le point important : une seule fois
    ne suffit pas. Le Tier 1.5 s'exécute avant le Tier 2, donc les matchs
    ramenés par odds-api.io/api-sports n'existaient pas encore au moment du
    premier passage — ils repartaient sans prix sharp, donc sans edge
    calculable, donc sans signal (constaté sur le run Golden Hour du
    2026-08-20 19:07 : 10 marchés Matchbook chargés, 0 match à enrichir).

    Le second appel a lieu AVANT `fetch_pinnacle_prices()` : chaque match
    servi par l'exchange est un match de moins à faire chercher sur le web,
    donc du quota Groq économisé pour le settlement.

    ⚠️ N'écrit toujours RIEN dans le prix de clôture : `capture_from_exchange`
    reçoit le dict de prix BRUT, jamais ces matchs enrichis (voir sa
    docstring). Le rôle 1 renforce cet invariant plutôt que de l'affaiblir —
    il n'écrase jamais `odds_pinnacle`.

    Renvoie le nombre de matchs ENRICHIS (rôle 2). Les contre-expertises ne
    sont pas comptées : elles ne posent aucun prix.
    """
    enriched = 0
    for m in items:
        bf = _lookup_exchange(m, prices)
        if not (bf and bf.get("1", 0) > 1.01 and bf.get("2", 0) > 1.01):
            continue

        pin = m.get("odds_pinnacle") or {}
        a_un_sharp = (pin.get("1", 0) > 1.01 and pin.get("2", 0) > 1.01
                      and not m.get("_estimated"))

        # ── Rôle 1 : contre-expertise ────────────────────────────────────
        if a_un_sharp:
            ecart = _sharp_divergence_pts(pin, bf)
            if ecart is None:
                continue          # incomparable : on ne juge pas
            if ecart > _EXCHANGE_DIVERGENCE_PTS:
                m["_sharp_conflict"] = {
                    "pts": ecart, "limite": _EXCHANGE_DIVERGENCE_PTS,
                    "source": bf.get("_source", "exchange"),
                }
                log.warning("CONFLIT SHARP | %s — Pinnacle et %s divergent de "
                            "%.2f pts de probabilité (> %.2f) — match REFUSÉ, "
                            "l'un des deux prix est périmé",
                            m.get("match", "?"), bf.get("_source", "exchange"),
                            ecart, _EXCHANGE_DIVERGENCE_PTS)
                continue
            m["odds_exchange"] = {"1": bf["1"], "X": bf.get("X", 0.0), "2": bf["2"]}
            # Loggé À CHAQUE comparaison, pas seulement aux refus : le seuil
            # a été posé sur 5 paires liquides et sa vraie distribution ne
            # peut venir que de la production (voir constants).
            log.info("CONTRE-EXP | %s — %s d'accord avec Pinnacle à %.2f pt "
                     "près, entre au consensus",
                     m.get("match", "?"), bf.get("_source", "exchange"), ecart)
            # L'exchange vient d'être reconnu D'ACCORD avec Pinnacle : son
            # carnet n'est pas périmé, et ses totals/handicaps sont la seule
            # référence sharp qui existe pour ces marchés — voir
            # `_poser_lignes_sharp`.
            _poser_lignes_sharp(m, bf, log)
            continue

        # ── Rôle 2 : bouche-trou ─────────────────────────────────────────
        src = bf.get("_source", "betfair")
        m["odds_pinnacle"] = {"1": bf["1"], "X": bf.get("X", 0.0), "2": bf["2"]}
        m["_exchange"] = src
        m["_betfair"] = True              # conservé : lu en aval/tests
        m.pop("_estimated", None)
        _poser_lignes_sharp(m, bf, log)
        enriched += 1
        log.info("💹 %s enrichi — %s (%.2f / %.2f)%s", src, m["match"], bf["1"], bf["2"],
                 "".join(f" +{k}" for k in ("totals", "spreads") if bf.get(k)))
    if enriched:
        log.info("💹 Exchange: %d matchs enrichis (prix sharp réel)", enriched)
    return enriched


def _heartbeat(sb, scan_time: datetime, matches: int | None, signals: int | None):
    """matches/signals à None = tick qui n'a PAS scanné (REPRICE à cache vide,
    sortie anticipée) : il rafraîchit `at` pour prouver sa vie, mais CONSERVE
    les comptes du dernier scan réel. Le step REPRICE du même tick écrasait
    « 41 matchs » par « 0 » six secondes après le scan — le dashboard annonçait
    un slate vide alors que le scan venait d'en voir 41 (2026-08-28)."""
    try:
        if matches is None or signals is None:
            prev = {}
            try:
                row = sb.table("meta").select("value").eq("key", "last_scan").maybe_single().execute()
                if row.data:
                    prev = json.loads(row.data["value"]) or {}
            except Exception:
                prev = {}
            matches = prev.get("matches", 0) if matches is None else matches
            signals = prev.get("signals", 0) if signals is None else signals
        sb.table("meta").upsert({
            "key":        "last_scan",
            "value":      json.dumps({
                "at":      scan_time.isoformat(),
                "matches": matches,
                "signals": signals,
            }),
            "updated_at": scan_time.isoformat(),
        }, on_conflict="key").execute()
        log.info("Heartbeat: last_scan updated (%d matchs, %d signaux)", matches, signals)
    except Exception as e:
        log.error("Supabase heartbeat: %s", e)




def _archive_before_purge(sb, signals: list):
    """Archive les signaux actifs dans ai_learning_ledger avant purge
    (CLV proxy, outcome=expired).

    Passe par core.db.log_to_ledger — l'insert manuel qu'il remplace
    OMETTAIT clv_pct_real, kelly_pct, sharp_prob et les closing_* : tous les
    expirés arrivaient au ledger avec un CLV réel NULL, ce qui affamait le
    `n` de learning_layer._clv_stats (le CLV converge ~3x plus vite que le
    win-rate — c'est l'échantillon qu'on ne peut pas se permettre de jeter).
    log_to_ledger porte aussi la dégradation colonne par colonne et le log
    CRITICAL en dernier recours : le contrat « un signal purgé ne disparaît
    jamais sans trace » reste tenu."""
    if not signals:
        return
    for sig in signals:
        orig_pin = sig.get("pinnacle_price") or 0.0
        clv = round((sig["xbet_odd"] / orig_pin - 1) * 100, 2) if orig_pin > 1.01 else 0.0
        _log_to_ledger(sb, sig, float(clv), "expired")
    log.info("Archived %d signals to ledger before purge", len(signals))



_STARVED_TTL_H = 24   # au-delà, le marqueur est considéré comme périmé


def _settlement_affame(sb) -> bool:
    """`meta.settlement_starved_at` est-il frais ? Jamais bloquant."""
    try:
        res = (sb.table("meta").select("value").eq("key", "settlement_starved_at")
               .limit(1).execute())
        rows = res.data or []
        if not rows:
            return False
        pose = datetime.fromisoformat(str(rows[0]["value"]).replace("Z", "+00:00"))
        if pose.tzinfo is None:
            pose = pose.replace(tzinfo=timezone.utc)
        frais = (datetime.now(timezone.utc) - pose) < timedelta(hours=_STARVED_TTL_H)
        if frais:
            log.warning("PURGE | famine de settlement signalée le %s — fenêtre portée "
                        "à 96 h pour ne pas détruire d'échantillon", pose.isoformat()[:16])
        return frais
    except Exception as e:                                       # noqa: BLE001
        log.debug("lecture settlement_starved_at: %s", e)
        return False

def _purge_old_signals(sb):
    """Delete stale signals. IMPROVED: batched operations + better logging."""
    cutoff_48h = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()

    # ── Archive active signals >48h before purging (preserve ledger history) ──
    try:
        stale = (sb.table("signals")
                 .select("*")
                 .eq("status", "active")
                 .lt("created_at", cutoff_48h)
                 .execute())
        _archive_before_purge(sb, stale.data or [])
    except Exception as e:
        log.debug("fetch stale for archive: %s", e)

    # ── Past-match purge — scoped to status=active AND a real grace window ──
    # BUGFIX #1 (earlier): this used to be `.lt("match_time", now_iso)` with
    # no status filter, deleting settled/closed/expired signals instantly.
    # Scoping to status=active fixed THAT, but not the actual starvation —
    # core/audit_engine.py's fetch_pending() deliberately waits
    # SETTLEMENT_GRACE_H (4h, see audit_engine.py) past match_time before a
    # signal is even eligible for settlement, and only runs every 6h. This
    # purge ran on every scan (golden_hour ~30min, engine ~2-3h) with a bare
    # `lt("match_time", now_iso)` cutoff — i.e. zero grace — so it deleted
    # every signal the instant its match ended, hours before audit's own
    # grace period even opened. Confirmed live 2026-07-09: signals 6994/6977
    # (WNBA, match_time 07-08 23:30 UTC) were gone by the next golden_hour
    # run at 01:09 UTC (+1h39), audit never got a chance — this is the real
    # root cause of the long-running ai_learning_ledger drought, not just a
    # one-off. BUGFIX #2: give audit generous headroom (grace period + several
    # 6h audit cycles, consistent with the 48h window used elsewhere in this
    # function) before purging an active-but-unsettled signal at all.
    # BUGFIX #3 (2026-07-21): 24h was too tight once audit_engine.py started
    # RETRYING signals it could not settle (EXPIRE_AFTER_H=36h) instead of
    # expiring them on the first search failure. A retried signal must survive
    # long enough to reach its terminal audit — purging at 24h would delete it
    # mid-retry and it would never reach ai_learning_ledger at all. 48h matches
    # the window already used elsewhere in this function and leaves a full 6h
    # audit cycle of headroom past EXPIRE_AFTER_H.
    # ── Famine de settlement : on ne purge pas ce qu'on n'a pas pu lire ──
    # Un signal non réglé part en `expired`, et learning_layer._clv_stats
    # exclut ces lignes : une panne de recherche de score ne retarde pas
    # l'apprentissage, elle DÉTRUIT l'échantillon. Le 2026-08-26, les deux
    # chemins de score étaient morts en même temps (Tavily HTTP 432, Groq
    # compound-mini en limite par minute) et un audit a rendu « 0 settled |
    # 52 skipped » — 52 échantillons condamnés par une panne d'API.
    # `core/audit_engine` pose `meta.settlement_starved_at` dans ce cas ; tant
    # qu'il est frais, on double la fenêtre. BORNÉ à 96 h : au-delà, le score
    # n'est plus retrouvable de toute façon et laisser gonfler la table
    # créerait une seconde panne pour en éviter une première.
    _heures_purge = 96 if _settlement_affame(sb) else 48
    purge_match_cutoff = (datetime.now(timezone.utc) - timedelta(hours=_heures_purge)).isoformat()
    try:
        sb.table("signals").delete().eq("status", "active").lt("match_time", purge_match_cutoff).execute()
    except Exception as e:
        log.debug("Supabase purge (past matches, active only): %s", str(e)[:60])

    # ── Purge Rules (more efficient than 13 individual calls) ──────────
    # active_only=True: these are data-quality gates meant to discard bad
    # ACTIVE candidates before betting — NOT to be applied to settled/closed/
    # expired rows, which must survive for the 48h /ledger + /audit window
    # (see the status='active' scoping rationale above). A settled signal
    # can legitimately have edge_pct > 10 (MAX_EDGE caps it at 15, not 10 —
    # 10-15% is merely a pre-bet SUSPECT_DATA trigger, not a data error once
    # the outcome is known) and must not be deleted for that reason alone.
    purge_rules = [
        ("eq",  "status", "pending",   "status=pending",                        False),
        ("lt",  "created_at", cutoff_48h, ">48h old",                           True),
        ("gt",  "edge_pct", 15.0,                                        "edge > 15% (hard cap)",           True),
        ("lte", "edge_pct", _PURGE_EDGE_FLOOR,                          f"edge <= {_PURGE_EDGE_FLOOR}% (bruit)", True),
        ("lte", "sharp_prob", 0.0,                                       "sharp_prob <= 0",                 True),
        ("is_", "market", "null",                                        "null market",                      True),
        ("is_", "sharp_prob", "null",                                    "sharp_prob=null",                 True),
        ("eq",  "risk_flag", "SUSPECT_DATA",                            "SUSPECT_DATA",                     True),
        ("lte", "xbet_odd", 1.01,                                       "xbet_odd <= 1.01",                True),
        ("lte", "pinnacle_price", 1.01,                                 "pinnacle_price <= 1.01",           True),
    ]

    for op_type, field, value, label, active_only in purge_rules:
        try:
            query = sb.table("signals").delete()
            if active_only:
                query = query.eq("status", "active")
            if op_type == "eq":
                query = query.eq(field, value)
            elif op_type == "lt":
                query = query.lt(field, value)
            elif op_type == "gt":
                query = query.gt(field, value)
            elif op_type == "lte":
                query = query.lte(field, value)
            elif op_type == "is_":
                query = query.is_(field, value)
            query.execute()

            if DEBUG_MODE:
                log.debug("Purged: %s", label)
        except Exception as e:
            log.debug("Supabase purge (%s): %s", label, str(e)[:60])

    # SUSPECT_DATA purge, scoped to match _emit()'s creation-time suspect_cap
    # exactly (h2h: 10%, totals/spreads: 15% — the >15% hard cap above already
    # covers everyone past that). A flat >10% purge here previously deleted
    # legitimate totals/spreads signals in the 10-15% band within one purge
    # cycle of being created — e.g. a HIGH_VALUE 11.4% totals signal purged
    # ~1h after creation, before it could ever settle. See _emit()'s comment
    # on suspect_cap for why 10-15% is valid for non-h2h markets.
    try:
        (sb.table("signals").delete()
         .eq("status", "active")
         .eq("market_key", "h2h")
         .in_("sport", list(_MAJOR_SPORTS))
         .gt("edge_pct", _SUSPECT_EDGE)
         .execute())
    except Exception as e:
        log.debug("Supabase purge (h2h SUSPECT): %s", str(e)[:60])

    # ── Legacy market_key cleanup ──────────────────────────────────────
    for legacy_key in ("totals", "spreads"):
        try:
            sb.table("signals").delete().eq("market_key", legacy_key).execute()
            if DEBUG_MODE:
                log.debug("Purged legacy market_key='%s'", legacy_key)
        except Exception as e:
            log.debug("Supabase purge (legacy %s): %s", legacy_key, str(e)[:60])
    try:
        sb.table("signals").delete().eq("sport", "soccer").eq("market", "Moneyline").execute()
        log.info("Purged: legacy soccer Moneyline")
    except Exception as e:
        log.error("Supabase purge (soccer Moneyline): %s", e)
    # sport-specific past-match purges removed — already covered by global
    # "delete where status=active AND match_time < now" above


def _emit(signals, sb, now, log, name, sport, league, mkt_key, mkt_label,
          executable_odd, pin_odd, sharp_prob, emoji, selection_name="", min_edge=None,
          match_time="", match_id="", sharp_sources=None, consensus_score=None,
          ah0_value: bool = False, sharp_prob_cons=None, dnb_draw_odd: float = 0.0):
    """Calcule l'EV, applique les gates de qualité, collecte le signal.

    `sharp_prob_cons` est la borne worst-case de devig_bounds() : quand elle
    est fournie, le signal doit rester EV-positif SOUS LA MÉTHODE DE
    DÉVIGORISATION LA PLUS DÉFAVORABLE pour sortir — c'est le filet standard
    des outils professionnels contre l'edge d'artefact de modèle."""
    # Le plancher EV_EDGE_FLOOR s'applique ICI et pas seulement au chargement
    # des seuils : la règle AH0 (h2h_min_edge=0.8) et tout futur appelant
    # passent par ce point unique. Sous le plancher, on parie l'erreur de
    # mesure du devig, pas un edge.
    effective_min = max(min_edge if min_edge is not None else MIN_EDGE, _EV_EDGE_FLOOR)
    if sport in _RETIRED_SPORTS:
        # Retirés le 2026-08-22 (bruit : prix de référence issu d'une recherche
        # web, jamais d'un book sharp). Le garde vit ICI, au point unique
        # d'émission, pour qu'un cache meta résiduel, un slate REPRICE ou un
        # harvest tiers ne puisse plus jamais produire un signal — le
        # settlement des lignes historiques, lui, ne passe pas par _emit.
        log.info("RETIRED | %s %s | %s — sport retiré, aucun signal", emoji, name, mkt_label)
        return
    if sharp_prob <= 0:
        log.info("DISCARD | %s %s | %s — sharp_prob=0 (stale/missing data)", emoji, name, mkt_label)
        return
    edge, status = compute_alpha(executable_odd, sharp_prob, min_edge=effective_min)
    if status == "DISCARD":
        log.info("DISCARD | %s %s | %s — EV %.2f%%", emoji, name, mkt_label, edge)
        return
    if sharp_prob_cons is not None:
        ev_cons = (sharp_prob_cons * executable_odd - 1) * 100
        if ev_cons <= 0:
            log.info("DISCARD | %s %s | %s — EV worst-case %.2f%% <= 0 (médiane %.2f%%) — "
                     "edge non robuste au choix de devig", emoji, name, mkt_label, ev_cons, edge)
            return

    # Safety Trigger: edge trop élevé = probable erreur de données.
    # H2H : seuil strict 10% (risque inversion team mapping).
    # Totals/Spreads : seuil large 15% (pas d'inversion possible, lag légitime sur baseball/KBO).
    suspect_cap = _SUSPECT_EDGE if mkt_key == "h2h" else _SUSPECT_EDGE * 1.5
    if edge > suspect_cap and sport in _MAJOR_SPORTS:
        log.warning("SUSPECT | %s %s | %s — Edge=+%.2f%% > %.0f%% — DISCARD",
                    emoji, name, mkt_label, edge, suspect_cap)
        return

    # Plafond d'edge APPRIS (core/learning_layer._top_band_verdict) — plus
    # serré que suspect_cap et propre à chaque sport. Le cap global à 10/15%
    # suppose qu'un edge intermédiaire est crédible ; le ledger dit le
    # contraire là où il a assez de résultats. Mesuré le 2026-08-02 : soccer
    # au-dessus de 6% affichait 36,7% de réussite sur 49 paris pour 47,8%
    # requis, quand la bande 1,5-4% gagnait. Un edge trop gros n'est pas une
    # inefficience, c'est un prix mal apparié.
    ceiling = _EDGE_CEILINGS.get(sport)
    if ceiling is not None and edge > ceiling:
        log.warning("PLAFOND | %s %s | %s — Edge=+%.2f%% > %.1f%% appris — DISCARD",
                    emoji, name, mkt_label, edge, ceiling)
        return

    # Plafond de COTE appris — au-dessus, la sélection n'est pas un favori mais
    # un quasi pile-ou-face que le ledger a prouvé perdant pour ce sport.
    odds_cap = _ODDS_CEILINGS.get(sport)
    if odds_cap is not None and executable_odd > odds_cap:
        log.warning("PLAFOND COTE | %s %s | %s — cote %.2f > %.2f appris — DISCARD",
                    emoji, name, mkt_label, executable_odd, odds_cap)
        return

    # J+72h filter: signaux très éloignés doivent être HIGH_VALUE (≥ 6%) pour justifier immobilisation capital
    # Fenêtre portée 36h→72h : capture WC + Copa Lib 2 jours avant match (meilleure liquidité pré-tournoi)
    if match_time:
        try:
            mt_dt = datetime.fromisoformat(match_time.replace("Z", "+00:00"))
            hours_ahead_mt = (mt_dt - now).total_seconds() / 3600
            # Match DÉJÀ COMMENCÉ — refus sec, aucune exception d'edge.
            #
            # Ce garde manquait : tout le bloc ne filtrait que le « trop loin »
            # (J+72h, lineup MLB), jamais le « déjà joué ». Constaté le
            # 2026-08-04 dans ai_learning_ledger : 20 paris réglés avec un
            # time_to_match_minutes NÉGATIF, jusqu'à -30 181 min (21 jours dans
            # le passé), exclusivement en mma/esports/tabletennis — les sports
            # dont le match_time vient de la recherche web (Groq/Tavily) et
            # n'est donc pas une donnée de flux vérifiée.
            #
            # Ces lignes « gagnaient » 15 fois sur 20 : on pariait sur des
            # événements déjà disputés, et le settlement retrouvait forcément le
            # bon résultat. C'est ce qui donnait au MMA un 8/8 flatteur et qui
            # gonflait la tranche golden_hour de faux gagnants — un filtre
            # `time_to_match_minutes < 120` embarque tous les négatifs.
            #
            # Corollaire pour toute analyse du ledger : borner par le bas
            # (`between 0 and 120`), les lignes négatives d'avant ce correctif
            # sont toujours en base.
            if hours_ahead_mt < 0:
                log.warning("MATCH PASSÉ | %s %s | %s — coup d'envoi il y a %.0f h "
                            "(match_time=%s) — signal refusé",
                            emoji, name, mkt_label, -hours_ahead_mt, match_time)
                return
            if hours_ahead_mt > 72 and edge < _ELITE_EDGE * 2:
                log.info("J+3 FILTER | %s %s | %s — edge %.2f%% < 6%% (T+%.0fh)",
                         emoji, name, mkt_label, edge, hours_ahead_mt)
                return
            # MLB Totals lineup filter: starters confirmed ~1h before first pitch.
            # Signals generated >6h before game time are pre-lineup — ERA/matchup
            # not yet priced in. Discard to avoid acting on stale total lines.
            if sport == "baseball" and "totals" in mkt_key and hours_ahead_mt > _MLB_LINEUP_WINDOW_H:
                log.info("MLB LINEUP FILTER | %s | %s — T+%.0fh > 6h (lineup unconfirmed)",
                         name, mkt_label, hours_ahead_mt)
                return
        except (ValueError, OverflowError):
            pass

    # Seuil VALUE sport-spécifique — évite LOW_VALUE invisible sur le dashboard
    if sport == "soccer":
        elite = _SOCCER_ELITE_EDGE        # 1.5% — AH0 marché serré
    elif sport in ("basketball", "euroleague_basketball"):
        elite = _BASKETBALL_ELITE_EDGE    # 2.0% — NBA Finales edges typiques 1.5–2.5% (Euroleague : mêmes mécaniques)
    else:
        elite = _ELITE_EDGE               # 2.5% — autres sports
    risk = _risk_flag(edge, elite)
    # Soccer AH0 Value Rule : si la cote DNB du favori > 1.5, upgrade LOW_VALUE → VALUE
    if ah0_value and risk == "LOW_VALUE":
        risk = "VALUE"

    # Mise via core/tax_engine (Kelly fiscalisé, optimisation bornée) — la
    # formule inline qu'il remplaçait ignorait TAX_RATE et surtout n'empêchait
    # pas d'émettre à mise nulle : 37 signaux sur 91 sont partis avec
    # kelly_pct=0, c'est-à-dire refusés par la couche de mise et publiés
    # quand même (constaté le 2026-08-22, concentrés sur les cotes 1,08-1,30).
    kelly_fraction = _KELLY_FRACTION.get(sport, 0.12)   # fraction ≤ 0,15 = réponse standard à l'erreur d'estimation
    kelly_pct = round(_optimal_stake_fraction(sharp_prob, executable_odd,
                                              tax_rate=_TAX_RATE,
                                              kelly_multiplier=kelly_fraction) * 100, 2)
    if kelly_pct <= 0:
        log.info("DISCARD | %s %s | %s — mise Kelly nulle (EV %.2f%% insuffisant après taxe) — "
                 "un signal qu'on ne miserait pas ne sort pas", emoji, name, mkt_label, edge)
        return

    # `dnb_draw_odd` n'est fourni que quand le prix d'entrée est un DNB
    # SYNTHÉTIQUE : il engage alors DEUX jambes chez le même book, et
    # `kelly_pct` est l'exposition TOTALE, pas une mise à poser sur l'équipe.
    # Taire la répartition ferait miser tout sur l'équipe et laisserait une
    # exposition au nul que le calcul d'EV n'a pas modélisée.
    advice = (
        f"EV +{edge:.1f}% — cote soft exécutable {executable_odd:.2f} vs sharp "
        f"{pin_odd:.2f} (prob. dévigorisée {sharp_prob * 100:.1f}%). "
        f"Mise conseillée {kelly_pct:.2f}% de bankroll (Kelly fractionnaire)."
    )
    if dnb_draw_odd and dnb_draw_odd > 1.01:
        part_nul, part_equipe = _dnb_leg_split(dnb_draw_odd)
        advice += (
            f" DNB synthétique — exposition TOTALE à répartir chez le MÊME book : "
            f"{part_equipe * 100:.1f}% sur {selection_name or name} et "
            f"{part_nul * 100:.1f}% sur le nul (@ {dnb_draw_odd:.2f}), "
            f"soit {kelly_pct * part_equipe:.2f}% et {kelly_pct * part_nul:.2f}% "
            f"de bankroll."
        )

    # Normalize match_time to ISO UTC (+00:00)
    mt = match_time.replace("Z", "+00:00") if match_time else ""

    log.info("SIGNAL  | %s %s | %s: Melbet=%.3f Pin=%.3f Edge=+%.2f%% Prob=%.0f%% %s",
             emoji, name, mkt_label, executable_odd, pin_odd, edge, sharp_prob * 100, risk)
    signal = {
        "match":          name,
        "league":         league or "",
        "sport":          sport,
        "market":         mkt_label,
        "market_key":     mkt_key,
        "executable_odd": float(executable_odd),
        "pinnacle_price": float(pin_odd),
        "sharp_prob":     float(sharp_prob),
        "edge_pct":       float(edge),
        "risk_flag":      risk,
        "scanned_at":     now.isoformat(),
        "match_time":     mt,
        "match_id":       match_id,
        "status":         "active",
        "selection_name": selection_name or name,
        "kelly_pct":      kelly_pct,
        "advice":         advice,
        "sharp_sources":  json.dumps(sharp_sources) if sharp_sources else None,
        "consensus_score": consensus_score,
        "correlation_group": _correlation_group(sport, league or "", mt),
    }
    signals.append(signal)


def _dnb_draw_odd(m: dict, sport: str) -> float:
    """
    Cote du nul QUAND le prix d'entrée est un DNB synthétique, 0.0 sinon.

    Miroir exact de la branche football de `core.math_engine.to_binary` : si la
    source expose un vrai AH 0.0 sur le côté favori, le pari n'a qu'une jambe
    et il n'y a aucune répartition à annoncer. Hors football non plus.
    Diverger de `to_binary` ici ferait annoncer une répartition sur un pari qui
    n'en a pas — ou taire celle d'un pari qui en a une.
    """
    if sport != "soccer":
        return 0.0
    o = m.get("odds_1xbet") or {}
    o1, o2 = float(o.get("1") or 0), float(o.get("2") or 0)
    if o1 <= 1.01 or o2 <= 1.01:
        return 0.0
    ah0 = float(o.get("ah0_1" if o1 <= o2 else "ah0_2") or 0)
    if ah0 > 1.01:
        return 0.0
    return float(o.get("X") or 0.0)


def _process_h2h(m, name, sport, league, home, away, emoji, signals, sb, now, log, min_edge=None):
    """H2H market: DNB for soccer, Moneyline for NBA/Tennis + Prob.Sharp filter."""
    prob_min    = SHARP_PROB_BY_MARKET.get("h2h_soccer" if sport == "soccer" else "h2h", 0.52)
    sources_found: dict = {}

    if "_oracle_price" in m:
        pin_price = m["_oracle_price"]
        executable_price, _, soft_fav = to_binary(m["odds_1xbet"], sport, home, away)
        pin_fav = m.get("_oracle_team", "")
        # Strict Matching: if oracle returned no team name, outcome alignment
        # is unverifiable — discard rather than risk a cross-outcome comparison.
        if not pin_fav:
            log.info("DISCARD | %s %s — oracle team unknown, outcome alignment unverifiable", emoji, name)
            return
        # Oracle returns a single-source price with no opposing-side odd to
        # devig against, so we can't compute a true Power-devigged probability.
        # Naive implied prob (1/price) is a conservative stand-in: unlike a
        # hardcoded 1.0, it still trips the sharp_prob quality gate below
        # and doesn't inflate the Kelly stake to the theoretical maximum.
        sharp_prob = round(1 / pin_price, 4) if pin_price > 1.01 else 0.0
        sharp_cons = sharp_prob   # proba implicite vigorisée = déjà une borne basse
    else:
        executable_price, _, soft_fav = to_binary(m["odds_1xbet"], sport, home, away)
        # Strict Matching: lock Pinnacle lookup to the same position as 1XBet
        # (fav_key "1"=home, "2"=away). Never run to_binary() on Pinnacle
        # independently — it could pick a different favourite and silently
        # compare Como's 1XBet odd against Parma's Pinnacle odd.
        fav_key = "1" if soft_fav == home else "2"
        opp_key = "2" if fav_key == "1" else "1"
        po = m.get("odds_pinnacle", {})
        if sport == "soccer":
            from core.math_engine import calc_dnb
            # Pinnacle DNB — correct 3-arg formula (fav, opp, draw)
            pin_fav_raw = float(po.get(fav_key, 0) or 0)
            pin_opp_raw = float(po.get(opp_key, 0) or 0)
            pin_draw    = float(po.get("X", 0) or 0)
            pin_price = calc_dnb(pin_fav_raw, pin_opp_raw, pin_draw)
            dnb_other = calc_dnb(pin_opp_raw, pin_fav_raw, pin_draw)

            # Weighted Power devig: build source prices for BOTH sides
            source_prices_fav = {"pinnacle": pin_price}
            source_prices_opp = {"pinnacle": dnb_other}
            # `odds_exchange` est posé par _enrich_from_exchange UNIQUEMENT
            # quand l'exchange s'accorde avec Pinnacle : sa contribution est
            # donc bornée par EXCHANGE_DIVERGENCE_PTS, par construction.
            for src_key, src_name in (("odds_circa", "circa"), ("odds_cris", "cris"),
                                      ("odds_exchange", "exchange")):
                so = m.get(src_key) or {}
                s_fav  = float(so.get(fav_key, 0) or 0)
                s_opp  = float(so.get(opp_key, 0) or 0)
                s_draw = float(so.get("X", 0) or 0)
                sp_f = calc_dnb(s_fav, s_opp, s_draw)
                sp_o = calc_dnb(s_opp, s_fav, s_draw)
                if sp_f > 1.01:
                    source_prices_fav[src_name] = sp_f
                if sp_o > 1.01:
                    source_prices_opp[src_name] = sp_o

            con_fav, sources_found, is_volatile, consensus_score = calculate_consensus_price(source_prices_fav, sport)
            if is_volatile:
                log.info("VOLATILE | %s %s — CV>1.2%% — DISCARD", emoji, name)
                return
            con_opp, _, _, _ = calculate_consensus_price(source_prices_opp, sport)
            if con_fav > 1.01:
                pin_price = con_fav
            if con_opp > 1.01:
                dnb_other = con_opp
            sharp_prob, sharp_cons = devig_bounds(pin_price, dnb_other)
        else:
            pin_price = float(po.get(fav_key, 0) or 0)
            opp_price = float(po.get(opp_key, 0) or 0)

            # Weighted Power devig: build source prices for BOTH sides
            source_prices_fav = {"pinnacle": pin_price}
            source_prices_opp = {"pinnacle": opp_price}
            for src_key, src_name in (("odds_circa", "circa"), ("odds_cris", "cris"),
                                      ("odds_exchange", "exchange")):
                so = m.get(src_key) or {}
                sp_f = float(so.get(fav_key, 0) or 0)
                sp_o = float(so.get(opp_key, 0) or 0)
                if sp_f > 1.01:
                    source_prices_fav[src_name] = sp_f
                if sp_o > 1.01:
                    source_prices_opp[src_name] = sp_o

            con_fav, sources_found, is_volatile, consensus_score = calculate_consensus_price(source_prices_fav, sport)
            if is_volatile:
                log.info("VOLATILE | %s %s — CV>1.2%% — DISCARD", emoji, name)
                return
            con_opp, _, _, _ = calculate_consensus_price(source_prices_opp, sport)
            if con_fav > 1.01:
                pin_price = con_fav
            opp_for_devig = con_opp if con_opp > 1.01 else opp_price
            sharp_prob, sharp_cons = devig_bounds(pin_price, opp_for_devig)

        pin_fav = soft_fav  # Same outcome guaranteed — no cross-book mismatch possible

    if executable_price <= 1.01 or pin_price <= 1.01:
        return
    if not strict_team_match(soft_fav, pin_fav):
        log.info("SPLIT   | %s %s — Melbet=%s Sharp=%s", emoji, name, soft_fav, pin_fav)
        return
    if sharp_prob < prob_min:
        log.info("LOWPROB | %s %s h2h — Prob.Sharp=%.0f%% < %.0f%%",
                 emoji, name, sharp_prob * 100, prob_min * 100)
        return

    # Soccer AH0 Value Rule : cote DNB fav > 1.5 → signal de valeur intrinsèque
    ah0_value = sport == "soccer" and executable_price > _AH0_VALUE_THRESHOLD
    # Abaisser le seuil d'edge à 0.8% pour ne pas étouffer ces signaux
    h2h_min_edge = min(min_edge if min_edge is not None else MIN_EDGE, 0.8) if ah0_value else min_edge

    lbl = market_label("h2h", "", 0.0, sport)
    _emit(signals, sb, now, log, name, sport, league,
          "h2h", lbl, executable_price, pin_price, sharp_prob, emoji,
          sharp_prob_cons=sharp_cons,
          selection_name=soft_fav, min_edge=h2h_min_edge,
          dnb_draw_odd=_dnb_draw_odd(m, sport),
          match_time=m.get("commence_time", ""), match_id=m.get("id", ""),
          sharp_sources=sources_found if sources_found else None,
          consensus_score=consensus_score if sources_found else None,
          ah0_value=ah0_value)


def _keep_best_side(sides: list, log, emoji, name) -> list:
    """Sur un marché à deux côtés opposés (Over/Under, handicap home/away),
    ne garder que celui au plus gros edge.

    Les deux côtés peuvent passer les filtres en même temps quand Melbet est
    moins margé que Pinnacle sur ce marché : le devig répartit alors une prob.
    sharp favorable des DEUX bords. C'est un artefact de marge, pas deux
    opportunités — et sur le dashboard ça produisait deux signaux
    contradictoires sur le même match ("Over 2.5 VALEUR" + "Under 2.5 VALEUR").
    """
    if not sides:
        return []
    best = max(sides, key=lambda s: s["edge_pct"])
    if len(sides) > 1:
        log.info("OPPOSITE | %s %s | %s — %d côtés positifs, on garde %s (+%.2f%%)",
                 emoji, name, best["market_key"], len(sides),
                 best["selection_name"], best["edge_pct"])
    return [best]


def _aligner_sur_meme_ligne(soft: dict, sharp: dict, marche: str, nom: str,
                            emoji: str, log) -> tuple[dict, dict]:
    """Fait coter la MÊME ligne aux deux books, quand ils l'ont tous les deux.

    LE PROBLÈME QUE ÇA RÉSOUT — ET CE QUE ÇA NE RÉSOUT PAS
    ------------------------------------------------------
    `_meme_ligne` refuse une paire de lignes différentes, et il a raison :
    deux handicaps différents sont deux paris différents (A6). Mais la
    divergence qu'il constatait était en grande partie FABRIQUÉE en amont.
    Chaque source cote une douzaine de lignes ; `core/odds_api_io.py` et
    `core/matchbook.py` n'en gardaient qu'une, « la plus équilibrée », chacune
    calculée sur SON carnet. Rien n'oblige un book soft à équilibrer sa cote
    sur le même handicap qu'un exchange — les deux choix tombaient donc
    souvent à côté l'un de l'autre, et la paire était refusée alors que la
    ligne du sharp était cotée chez le soft aussi : on venait juste de la
    jeter.

    Mesuré le 2026-08-27 sur les matchs communs aux deux sources : 1 total sur
    2, et 0 spread sur 2, survivaient à la comparaison.

    Cette fonction ne relâche AUCUNE garde : elle choisit dans les deux
    échelles une ligne RÉELLEMENT commune, puis laisse `_meme_ligne` trancher
    comme avant. Sans ligne commune, elle rend la paire telle quelle et le
    refus a lieu.

    QUELLE LIGNE COMMUNE. Celle du sharp d'abord, sinon la plus proche. JAMAIS
    celle qui donnerait le plus gros edge : parcourir une échelle en retenant
    la ligne la mieux payée, c'est retenir l'erreur de cote la plus grosse —
    la queue positive qu'A6 a précisément identifiée comme un artefact.
    """
    try:
        p_soft, p_sharp = float(soft.get("point")), float(sharp.get("point"))
    except (TypeError, ValueError):
        return soft, sharp          # ligne absente/illisible : `_meme_ligne` refusera
    if p_soft == p_sharp:
        return soft, sharp

    def _echelle(d: dict) -> dict:
        out = {}
        for r in d.get("ladder") or []:
            try:
                out[float(r["point"])] = r
            except (TypeError, ValueError, KeyError):
                continue
        return out

    ech_soft, ech_sharp = _echelle(soft), _echelle(sharp)
    communes = set(ech_soft) & set(ech_sharp)
    if not communes:
        return soft, sharp

    cible = min(communes, key=lambda p: (abs(p - p_sharp), abs(p - p_soft), abs(p)))
    log.info("ALIGNE  | %s %s %s — soft %+.2f / sharp %+.2f : les deux cotent "
             "%+.2f, comparaison sur cette ligne",
             emoji, nom, marche, p_soft, p_sharp, cible)
    return {**soft, **ech_soft[cible]}, {**sharp, **ech_sharp[cible]}


def _meme_ligne(soft: dict, sharp: dict, marche: str, nom: str, emoji: str,
                log) -> float | None:
    """La ligne du book SOFT et celle du SHARP sont-elles la MÊME ?

    Rend la ligne commune, ou None s'il faut refuser le marché.

    POURQUOI C'EST DEVENU STRICT (2026-08-27)
    -----------------------------------------
    La garde précédente s'écrivait :

        if xs_line and ps_line and abs(abs(xs_line) - abs(ps_line)) > 0.5

    Elle portait TROIS défauts, et chacun fabrique exactement l'objet qu'elle
    prétend écarter — un « edge » qui n'est que l'écart de prix entre deux
    paris DIFFÉRENTS :

      1. `if xs_line and ps_line` — en Python, **0.0 est faux**. Une ligne à
         AH 0.0 d'un côté DÉSACTIVAIT donc la garde entièrement : le moteur
         pouvait comparer un Draw No Bet soft à un handicap −1,5 sharp sans
         rien signaler. C'est le cas le plus fréquent du football.
      2. Une tolérance de 0,5 — AH −1,0 contre AH −1,5 passait. Sur un
         handicap, une demi-unité change le pari : l'un rembourse sur une
         victoire d'un but exact, l'autre la perd.
      3. `abs(abs(x) - abs(p))` — le double `abs` DÉTRUIT LE SIGNE. −0,5
         contre +0,5 passait, −1,0 contre +1,0 aussi. Ce sont les handicaps
         OPPOSÉS : on comparait le prix du favori chez un book à celui de
         l'outsider chez l'autre. L'écart est énorme et ressemble toujours à
         un edge.

    Mesuré le 2026-08-27 : sur le premier run du moteur corrigé, les 7 refus
    LINESKIP portaient TOUS sur des totals — pas un seul sur un spread, alors
    que les deux seuls signaux émis étaient des spreads (« SOC PS -0.0 » et
    « SOC PS -1.0 »).

    La règle est donc l'ÉGALITÉ EXACTE, signe compris. Deux handicaps
    différents sont deux paris différents ; il n'y a pas de « presque le même
    pari ». Et une ligne ABSENTE fait refuser au lieu de passer : on ne peut
    pas vérifier qu'on compare la même chose sans la voir. C'est le même
    contrat que le football sans prix de nul (A1) — le refus silencieux plutôt
    qu'un prix posé au hasard.
    """
    brut_soft, brut_sharp = soft.get("point"), sharp.get("point")
    if brut_soft is None or brut_sharp is None:
        log.info("LINESKIP | %s %s %s — ligne absente d'un côté "
                 "(soft=%s sharp=%s), impossible de vérifier qu'on compare "
                 "le même pari", emoji, nom, marche, brut_soft, brut_sharp)
        return None
    try:
        ligne_soft, ligne_sharp = float(brut_soft), float(brut_sharp)
    except (TypeError, ValueError):
        log.info("LINESKIP | %s %s %s — ligne illisible", emoji, nom, marche)
        return None
    # `+0.0 == -0.0` est vrai en Python, et c'est ce qu'on veut : les deux
    # écritures désignent le même handicap nul.
    if ligne_soft != ligne_sharp:
        log.info("LINESKIP | %s %s %s — soft %+.2f ≠ sharp %+.2f : deux paris "
                 "différents, l'écart de prix n'est pas un edge",
                 emoji, nom, marche, ligne_soft, ligne_sharp)
        return None
    return ligne_sharp


def _process_totals(m, name, sport, league, emoji, signals, sb, now, log, min_edge=None):
    """Over/Under market for all sports."""
    prob_min = SHARP_PROB_BY_MARKET["totals"]
    xt = m["totals_1xbet"]
    pt = m["totals_pinnacle"]

    # Les deux books doivent coter LE MÊME total — voir `_meme_ligne`. On
    # cherche d'abord la ligne commune dans leurs échelles respectives.
    xt, pt = _aligner_sur_meme_ligne(xt, pt, "totals", name, emoji, log)
    point = _meme_ligne(xt, pt, "totals", name, emoji, log)
    if point is None:
        return

    # Round-line push detection: integer totals (8.0, 9.0) can push.
    # Half-lines (.5) never push — no adjustment needed.
    # PUSH_PROB_ROUND_LINE (10%) is calibrated specifically for MLB/baseball
    # integer totals — applying it to other sports' round lines (basketball,
    # hockey, soccer, rugbyleague, aussierules) would bake in a push
    # probability with no empirical basis for those markets.
    is_round_line = sport == "baseball" and is_round_number_line(point)
    if is_round_line:
        log.info("ROUNDLINE | %s %s totals %.1f — P(push)=%.0f%% → sharp_prob adjusted",
                 emoji, name, point, _PUSH_PROB_ROUND_LINE * 100)

    circa_t = m.get("totals_circa") or {}
    cris_t  = m.get("totals_cris")  or {}

    sides: list = []
    for side, other in [("over", "under"), ("under", "over")]:
        x_odd = float(xt.get(side, 0))
        p_odd = float(pt.get(side, 0))
        p_lay = float(pt.get(other, 0))
        if x_odd <= 1.01 or p_odd <= 1.01:
            continue

        src_side  = {"pinnacle": p_odd}
        src_other = {"pinnacle": p_lay}
        for src, key in ((circa_t, "circa"), (cris_t, "cris")):
            sp_s = float(src.get(side, 0) or 0)
            sp_o = float(src.get(other, 0) or 0)
            if sp_s > 1.01:
                src_side[key] = sp_s
            if sp_o > 1.01:
                src_other[key] = sp_o

        con_side, sources_found, is_volatile, consensus_score = calculate_consensus_price(src_side, sport)
        if is_volatile:
            log.info("VOLATILE | %s %s totals — CV>1.2%% — DISCARD", emoji, name)
            continue
        con_other, _, _, _ = calculate_consensus_price(src_other, sport)
        if con_side > 1.01:
            p_odd = con_side
        if con_other > 1.01:
            p_lay = con_other

        sharp_prob, sharp_cons = devig_bounds(p_odd, p_lay)
        # Push-adjusted probability: P(win | no push) = P(win) / (1 - P(push))
        # This mechanically lowers EV on round lines vs half-lines, as intended.
        if is_round_line:
            sharp_prob = round(sharp_prob * (1 - _PUSH_PROB_ROUND_LINE), 4)
            sharp_cons = round(sharp_cons * (1 - _PUSH_PROB_ROUND_LINE), 4)
        if sharp_prob < prob_min:
            continue
        lbl = market_label("totals", side, point, sport)
        sel = f"{'Over' if side == 'over' else 'Under'}{(' ' + str(point)) if point else ''}"
        _emit(sides, sb, now, log, name, sport, league,
              f"totals_{side}", lbl, x_odd, p_odd, sharp_prob, emoji,
              sharp_prob_cons=sharp_cons,
              selection_name=sel, min_edge=min_edge,
              match_time=m.get("commence_time", ""), match_id=m.get("id", ""),
              sharp_sources=sources_found if sources_found else None,
              consensus_score=consensus_score if sources_found else None)

    signals.extend(_keep_best_side(sides, log, emoji, name))


def _process_spreads(m, name, sport, league, home, away, emoji, signals, sb, now, log, min_edge=None):
    """Spread/Handicap market for NBA + Soccer."""
    prob_min = SHARP_PROB_BY_MARKET["spreads"]
    xs = m["spreads_1xbet"]
    ps = m["spreads_pinnacle"]

    # Les deux books doivent coter LE MÊME handicap, SIGNE COMPRIS — voir
    # `_meme_ligne`. Le libellé du signal reprend cette ligne unique : quand
    # les deux divergeaient, l'ancien code étiquetait le pari avec la ligne
    # SHARP tout en misant au prix de la ligne SOFT.
    xs, ps = _aligner_sur_meme_ligne(xs, ps, "spreads", name, emoji, log)
    home_point = _meme_ligne(xs, ps, "spreads", name, emoji, log)
    if home_point is None:
        return
    away_point = -home_point

    circa_s = m.get("spreads_circa") or {}
    cris_s  = m.get("spreads_cris")  or {}

    sides: list = []
    for side, team, pt in [("home", home, home_point), ("away", away, away_point)]:
        x_odd = float(xs.get(side, 0))
        p_odd = float(ps.get(side, 0))
        p_lay = float(ps.get("away" if side == "home" else "home", 0))
        if x_odd <= 1.01 or p_odd <= 1.01:
            continue

        other_side = "away" if side == "home" else "home"
        src_side  = {"pinnacle": p_odd}
        src_other = {"pinnacle": p_lay}
        for src, key in ((circa_s, "circa"), (cris_s, "cris")):
            sp_s = float(src.get(side, 0) or 0)
            sp_o = float(src.get(other_side, 0) or 0)
            if sp_s > 1.01:
                src_side[key] = sp_s
            if sp_o > 1.01:
                src_other[key] = sp_o

        con_side, sources_found, is_volatile, consensus_score = calculate_consensus_price(src_side, sport)
        if is_volatile:
            log.info("VOLATILE | %s %s spreads — CV>1.2%% — DISCARD", emoji, name)
            continue
        con_other, _, _, _ = calculate_consensus_price(src_other, sport)
        if con_side > 1.01:
            p_odd = con_side
        if con_other > 1.01:
            p_lay = con_other

        sharp_prob, sharp_cons = devig_bounds(p_odd, p_lay)
        if sharp_prob < prob_min:
            continue
        lbl = market_label("spreads", side, pt, sport)
        pt_str = f"+{pt}" if pt > 0 else str(pt)
        _emit(sides, sb, now, log, name, sport, league,
              f"spreads_{side}", lbl, x_odd, p_odd, sharp_prob, emoji,
              sharp_prob_cons=sharp_cons,
              selection_name=f"{team} {pt_str}", min_edge=min_edge,
              match_time=m.get("commence_time", ""), match_id=m.get("id", ""),
              sharp_sources=sources_found if sources_found else None,
              consensus_score=consensus_score if sources_found else None)

    signals.extend(_keep_best_side(sides, log, emoji, name))


# ── Portfolio Balancer ────────────────────────────────────────────────

def _shadow_partition(signals: list, golden_hour: bool) -> tuple[list, list]:
    """Sépare (à_recommander, fantômes) — voir SHADOW_SPORTS en tête de fichier.

    Les fantômes ne sont PAS jetés : l'appelant les a déjà persistés, ils
    seront réglés et appris comme les autres. Seule la recommandation
    Telegram s'arrête. `golden_hour` est passé en paramètre plutôt que lu
    depuis le flag global pour que la règle soit testable sans manipuler
    l'environnement du processus.
    """
    golden_shadowed = SHADOW_GOLDEN_HOUR and golden_hour
    kept, shadowed = [], []
    for s in signals:
        (shadowed if golden_shadowed or s.get("sport") in SHADOW_SPORTS
         else kept).append(s)
    return kept, shadowed


def _portfolio_balance(candidates: list) -> list:
    """
    Enforce per-sport quota and sort by edge descending.
    A +5% NBA edge beats a +3% soccer edge even if soccer starts sooner.
    Returns at most SPORT_QUOTA[sport] signals per sport.
    """
    by_sport: dict[str, list] = {}
    for s in sorted(candidates, key=lambda x: x["edge_pct"], reverse=True):
        sport = s.get("sport", "soccer")
        by_sport.setdefault(sport, []).append(s)

    result = []
    for sport in _SPORT_ORDER:
        quota = SPORT_QUOTA.get(sport, 3)
        result.extend(by_sport.get(sport, [])[:quota])
    # Any sport not in _SPORT_ORDER (future-proofing)
    for sport, sigs in by_sport.items():
        if sport not in _SPORT_ORDER:
            result.extend(sigs[:SPORT_QUOTA.get(sport, 3)])
    return result


_RISK_DOT = {"HIGH_VALUE": "🟢", "VALUE": "🟡", "LOW_VALUE": "⚪", "SUSPECT_DATA": "🔴"}


def _urgency_sort(s: dict, now) -> tuple:
    """Sort key: signaux < 4h d'abord (par heure ASC), puis par edge DESC."""
    mt = s.get("match_time") or ""
    try:
        mt_dt = datetime.fromisoformat(mt.replace("Z", "+00:00"))
        mins  = (mt_dt - now).total_seconds() / 60
        if 0 < mins <= 240:
            return (0, mins, -(s.get("edge_pct") or 0))
    except (ValueError, OverflowError):
        pass
    return (1, 9999.0, -(s.get("edge_pct") or 0))
_SESSION_ICON = {"EU-OPEN": "📈", "EU-MID": "⚡", "EU-CLOSE": "🎯", "OVERNIGHT": "🌙"}


def _session_icon(session: str) -> str:
    for k, v in _SESSION_ICON.items():
        if k in session:
            return v
    return ""


def _window_key(sig: dict) -> str:
    """Hourly bucket (YYYY-MM-DDTHH) for grouping signals into one system
    suggestion — combining a match kicking off in 2h with one in 2 days
    into the same bet slip would blur the time-sensitive urgency that
    makes either edge real. Signals with no match_time fall into one
    'unscheduled' bucket rather than being silently dropped."""
    mt = sig.get("match_time") or ""
    return mt[:13] if mt else "unscheduled"


def _suggest_systems_by_window(signals: list, log, sb=None) -> list:
    """
    Group signals by time window and ask core.tax_engine.suggest_system()
    for the best net-of-tax-viable accumulator in each window — replacing
    the old "alert every signal independently" model (PAIM v9.5, Task 2).
    A window with no tax-viable combo contributes nothing; individual
    signals are still persisted to Supabase by the caller regardless (see
    run()) for settlement/learning, this only gates what Telegram sees.

    Task 7: the bankroll passed into suggest_system() is reduced by
    whatever's already committed to other active signals (core.risk_manager
    exposure cap), computed once per run rather than once per window — so
    a portfolio already at its cap naturally sizes every new system down
    to stake=0 instead of stacking risk on top of risk.

    Sizing-base note (2026-07-11, operator decision: dashboard is
    canonical — see core.risk_manager's module docstring DESIGN NOTE for
    the full explanation): the headroom subtracted here is computed from
    every active signal's solo kelly_pct (each signal's own would-be
    stake, also what the dashboard shows per-signal). The system stake
    this function returns (one combined, numerically-optimal figure per
    window) is no longer sized independently of that basis —
    core.tax_engine.suggest_system() caps it at the sum of its own legs'
    kelly_pct, so what Telegram recommends can never exceed what the
    dashboard already implies for the same signals.
    """
    effective_bankroll = _BANKROLL_REF
    if sb is not None:
        try:
            headroom = _risk_manager.get_exposure_headroom(sb, _BANKROLL_REF)
            effective_bankroll = max(0.0, headroom)
            if effective_bankroll < _BANKROLL_REF:
                log.info("Exposure cap: %.0f/%.0f bankroll headroom remaining", effective_bankroll, _BANKROLL_REF)
        except Exception as e:
            log.warning("get_exposure_headroom: %s — using full bankroll", e)

    windows: dict[str, list] = {}
    for s in signals:
        windows.setdefault(_window_key(s), []).append(s)

    systems = []
    for key, group in windows.items():
        sports_in_group = {s.get("sport", "soccer") for s in group}
        kelly_mult = min((_KELLY_FRACTION.get(sp, 0.12) for sp in sports_in_group), default=0.12)
        result = _suggest_system(group, bankroll=effective_bankroll, tax_rate=_TAX_RATE,
                                  kelly_multiplier=kelly_mult)
        if result is None:
            log.info("SYSTEM  | window %s — no tax-viable combo (%d candidate legs)", key, len(group))
            continue
        # Final go/no-go re-check right before it's ever eligible for
        # Telegram — defense in depth even though suggest_system() already
        # filtered every candidate combo on this internally.
        legs_for_check = [{"true_prob": s.get("sharp_prob", 0), "odds": s.get("executable_odd", 0),
                           "correlation_group": s.get("correlation_group")}
                          for s in result["legs"]]
        if not _is_combo_tax_viable(legs_for_check, tax_rate=_TAX_RATE):
            log.warning("SYSTEM  | window %s — combo failed final tax-viability re-check, discarding", key)
            continue
        result["window"] = key
        systems.append(result)
        log.info("SYSTEM  | window %s | %d legs | combined %.2f @ %.1f%% | stake %.0f | EV +%.2f",
                 key, result["k"], result["combined_odds"], result["combined_prob"] * 100,
                 result["stake"], result["ev"])

    return systems


def _refresh_leg_price(leg: dict, fresh_by_sport: dict, log) -> float | None:
    """
    Re-derive the current soft-book price for one h2h leg from a freshly
    re-fetched batch (Task 8). Returns None if unchanged/unresolvable —
    only h2h is handled: totals/spreads need the same line-matching logic
    _process_totals/_process_spreads use to pick the right price for the
    original selection, which isn't worth duplicating for a last-look
    check; those legs are sent at their scan-time price, unchanged.
    """
    if not (leg.get("market_key") or "").startswith("h2h"):
        return None
    from core.harvester import SPORT_IDS, _fuzzy_match_event
    sport_id = next((sid for sid, name in SPORT_IDS.items() if name == leg.get("sport")), None)
    fresh_events = fresh_by_sport.get(sport_id) if sport_id is not None else None
    if not fresh_events:
        return None
    match = leg.get("match", "")
    if " vs " not in match:
        return None
    home, away = [x.strip() for x in match.split(" vs ", 1)]
    fresh = _fuzzy_match_event({"home": home, "away": away}, fresh_events)
    if not fresh:
        return None
    sel = leg.get("selection_name") or ""
    is_home = strict_team_match(sel, home)
    # Le prix d'un leg est le prix EXÉCUTABLE (DNB synthétique en football).
    # Relire la cote 1X2 brute — ce que faisait cette fonction jusqu'au
    # 2026-08-27 — comparerait deux grandeurs différentes : la marge du book
    # passerait pour un mouvement de ligne favorable, et le last-look
    # laisserait passer des combos que le prix réel condamne. On reprixe donc
    # le CÔTÉ MISÉ avec `executable_price`, la règle même qui a fixé l'entrée.
    new_odd = _executable_price(fresh.get("odds_1xbet", {}),
                                leg.get("sport", ""), "1" if is_home else "2")
    return new_odd if new_odd > 1.01 else None


def _last_look_reprice(systems: list, log) -> list:
    """
    Task 8 — right before Telegram send, re-fetch the soft-book price for
    each h2h leg and re-verify the system is STILL tax-viable at CURRENT
    prices, not the prices captured at scan time — which can be minutes
    old by send time (this run alone adds 1.5-4s of deliberate jitter on
    top of however long the scan/persistence steps took). A system whose
    price moved against the bettor enough to no longer clear
    tax_engine.is_combo_tax_viable is dropped rather than sent stale.
    """
    if not systems:
        return systems

    from core.harvester import SPORT_IDS, _fetch_multi_book

    sport_ids_needed = {sid for sid, name in SPORT_IDS.items()
                        if any(leg.get("sport") == name for sys_ in systems for leg in sys_["legs"])}
    fresh_by_sport: dict = {}
    for sid in sport_ids_needed:
        try:
            fresh_by_sport[sid] = _fetch_multi_book(sid)
        except Exception as e:
            log.warning("Last-look reprice: sport_id=%s fetch failed: %s", sid, e)

    survivors = []
    for sys_ in systems:
        repriced_legs = []
        changed = False
        for leg in sys_["legs"]:
            new_odd = _refresh_leg_price(leg, fresh_by_sport, log)
            if new_odd and new_odd != leg.get("executable_odd"):
                repriced_legs.append({**leg, "executable_odd": new_odd})
                changed = True
            else:
                repriced_legs.append(leg)

        combo_legs = [{"true_prob": leg.get("sharp_prob", 0), "odds": leg.get("executable_odd", 0),
                       "correlation_group": leg.get("correlation_group")} for leg in repriced_legs]
        if _is_combo_tax_viable(combo_legs, tax_rate=_TAX_RATE):
            if changed:
                log.info("SYSTEM  | window %s — last-look reprice applied, still viable", sys_.get("window"))
            sys_["legs"] = repriced_legs
            survivors.append(sys_)
        else:
            log.warning("SYSTEM  | window %s — cancelled at last-look, price moved against the combo",
                       sys_.get("window"))

    return survivors


def _kickoff(leg: dict, now) -> str:
    """' · 21:00 UTC' for a match today, ' · 22/07 21:00 UTC' otherwise.
    Empty string when match_time is unusable — never a fabricated hour."""
    raw = leg.get("match_time") or ""
    if not raw:
        return ""
    try:
        mt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return ""
    if mt.tzinfo is None:
        mt = mt.replace(tzinfo=timezone.utc)
    same_day = mt.date() == now.date()
    return f" · {mt.strftime('%H:%M') if same_day else mt.strftime('%d/%m %H:%M')} UTC"


def _favourite(leg: dict) -> str:
    """Team the sharp price makes favourite, or '' when it isn't derivable.

    Only h2h carries a favourite we actually hold data for: sharp_prob is the
    probability of THIS selection, so >= 50% means the pick is the favourite
    and < 50% means the opponent is. Totals/spreads signals price a line, not
    a side — we never store the two teams' moneyline prices for them, so
    there is nothing to name and this returns ''. Guessing one from the home
    team would be inventing information the operator would read as fact.
    """
    if leg.get("market_key") != "h2h":
        return ""
    prob  = leg.get("sharp_prob") or 0.0
    match = leg.get("match") or ""
    sel   = leg.get("selection_name") or ""
    if not prob or " vs " not in match:
        return ""
    home, away = (p.strip() for p in match.split(" vs ", 1))
    if prob >= 0.5:
        return sel or home
    is_home = resolve_selection_side(sel, home, away)
    if is_home is None:
        return ""
    return away if is_home else home


def _telegram_systems(systems: list, now, session: str, matches: int,
                      sharp_source: str, no_pin_count: int):
    """Send tax-viable system suggestions only, one per time window.
    Individual signals are still persisted to Supabase for settlement/
    learning (see run()) — Telegram now only ever recommends a combo that
    has already cleared tax_engine.is_combo_tax_viable(), or nothing.

    Format (operator request 2026-07-21, "simple lisible et compréhensible"):
    event, favourite, pick, odds, kick-off, value — and nothing else. Stakes
    and euro EV are deliberately gone: bankroll sizing printed "Mise 0€ · EV
    net taxe +0.01€" on every line, which is noise at best and misleading at
    worst. Value is expressed as a percentage, which needs no bankroll.
    """
    sess   = session.strip()
    icon   = _session_icon(sess)

    if not systems:
        _telegram(
            f"⚫ PREDATOR · {now.strftime('%H:%M')} UTC · {sess} {icon}\n"
            f"Aucun pari de valeur · {matches} matchs analysés"
        )
        return

    no_pin = f"\n⚠️ {no_pin_count} sans confirmation Pinnacle" if no_pin_count > 0 else ""
    header = (
        f"📡 *PREDATOR* · {now.strftime('%H:%M')} UTC · {sess} {icon}\n"
        f"{len(systems)} pari(s) de valeur · {matches} matchs analysés{no_pin}\n"
    )

    def _system_urgency(sys_):
        return min((_urgency_sort(leg, now) for leg in sys_["legs"]), default=(1, 9999.0, 0))

    body_parts: list[str] = []
    for i, sys_ in enumerate(sorted(systems, key=_system_urgency), start=1):
        legs = sys_["legs"]
        combi = "" if sys_["k"] == 1 else f" — combiné {sys_['k']} sélections"
        lines: list[str] = [f"\n🎯 *Pari {i}*{combi}\n"]
        for leg in legs:
            emoji = SPORT_EMOJI.get(leg.get("sport", "?"), "🎯")
            sel   = leg.get("selection_name") or leg["match"]
            lines.append(f"{emoji} *{leg.get('match', '?')}*{_kickoff(leg, now)}\n")
            # Ligne "Favori" seulement quand le favori n'est PAS le pari —
            # sinon elle répète mot pour mot la ligne suivante.
            fav = _favourite(leg)
            if fav and fav != sel:
                lines.append(f"   Favori : {fav}\n")
            tag = " (favori)" if fav and fav == sel else ""
            lines.append(f"   → {sel}{tag} `@ {leg['executable_odd']:.2f}` · valeur `+{leg.get('edge_pct', 0):.1f}%`\n")
        if sys_["k"] > 1:
            combo_value = (sys_["combined_odds"] * sys_["combined_prob"] - 1) * 100
            lines.append(f"   *Combiné* `@ {sys_['combined_odds']:.2f}` · valeur `+{combo_value:.1f}%`\n")
        body_parts.append("".join(lines))

    body = "".join(body_parts)
    # Telegram 4096-char limit: send in chunks if needed
    full = header + body
    if len(full) <= 4000:
        _telegram(full)
    else:
        _telegram(header)
        chunk = ""
        for part in body_parts:
            if len(chunk) + len(part) > 3800:
                _telegram(chunk)
                chunk = part
            else:
                chunk += part
        if chunk.strip():
            _telegram(chunk)


def _segment_min_edge(dyn_thresholds: dict, dyn_segment_thresholds: dict,
                       sport: str, market_family: str) -> float:
    """
    Segment (sport, market family) MIN_EDGE if core/learning_layer.py has
    gathered enough samples for it yet, else the coarser sport-level
    threshold — see core/learning_layer.py:compute_and_save()'s segment
    layer. h2h/totals/spreads within the same sport can have very different
    reliability (e.g. push risk on totals), so they shouldn't necessarily
    share one bar once there's enough data to tell them apart.
    """
    sport_floor = dyn_thresholds.get(sport, MIN_EDGE)
    learned = dyn_segment_thresholds.get(f"{sport}:{market_family}", sport_floor)
    # Un seuil appris peut monter au-dessus du plancher, jamais descendre en
    # dessous — voir EV_EDGE_FLOOR dans core/constants.py.
    return max(learned, _EV_EDGE_FLOOR)


# ── main ─────────────────────────────────────────────────────────────

def run():
    budget = _arm_global_timeout()
    now     = datetime.now(timezone.utc)
    session = _market_session(now.hour)
    log.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    # REPRICE en TÊTE : si deux flags sont posés ensemble par erreur (dispatch
    # manuel), le mode le plus restrictif — zéro source payante — l'emporte.
    if REPRICE:
        mode = "REPRICE 💹 Matchbook vs slate soft"
        if GOLDEN_HOUR or GUERRILLA:
            log.warning("REPRICE posé avec GOLDEN_HOUR/GUERRILLA — REPRICE prime, "
                        "les autres flags sont ignorés")
    elif GUERRILLA:
        mode = "GUERRILLA 🥷 Sans OddsAPI"
    elif GOLDEN_HOUR:
        mode = "GOLDEN HOUR ⚡ T-120min"
    elif DEEP_SCAN:
        mode = "DEEP 48h"
    else:
        mode = "FAST 72h"
    log.info("PAIM v8.8 — %s | Multi-Sport + Portfolio Balancer | Session: %s | "
             "budget %ds", mode, session, budget)
    _refresh_ai_catalogues()
    log.info("Scan start: %s | max_events=%d | quotas=%s",
             now.strftime("%Y-%m-%d %H:%M:%S UTC"), MAX_MATCHES,
             " ".join(f"{k}={v}" for k, v in SPORT_QUOTA.items()))

    # Credential failure must NOT block Telegram below — it's intentionally
    # decoupled from Supabase (see "toujours envoyé" further down) and stays
    # the user's only signal feed if the DB is misconfigured. So we log
    # CRITICAL and keep going with sb=None, but still fail the job (non-zero
    # exit) at the very end so GitHub Actions surfaces it loudly instead of
    # a silent multi-hour string of per-signal RLS errors.
    credentials_failed = False
    try:
        sb = get_db(write=True)
    except MissingCredentialsError as e:
        log.critical("%s", e)
        sb = None
        credentials_failed = True
    if sb:
        try:
            _purge_old_signals(sb)
        except Exception as e:
            log.error("Purge failed (continuing): %s", e)

    # Load sport-specific MIN_EDGE thresholds from learning layer
    dyn_thresholds: dict[str, float] = {}
    dyn_segment_thresholds: dict[str, float] = {}
    sport_ranking: list[str] = []   # meilleur d'abord ; [] = pas d'historique
    if sb:
        try:
            dyn_thresholds = _load_thresholds(sb)
            if any(v != MIN_EDGE for v in dyn_thresholds.values()):
                log.info("Dynamic thresholds: %s",
                         " | ".join(f"{k}={v:.2f}%" for k, v in dyn_thresholds.items()))
        except Exception as e:
            log.warning("load_thresholds: %s — using default %.1f%%", e, MIN_EDGE)
        try:
            dyn_segment_thresholds = _load_segment_thresholds(sb)
            if dyn_segment_thresholds:
                log.info("Segment thresholds: %s",
                         " | ".join(f"{k}={v:.2f}%" for k, v in dyn_segment_thresholds.items()))
        except Exception as e:
            log.warning("load_segment_thresholds: %s — sport-level thresholds only", e)
        try:
            _EDGE_CEILINGS.update(_load_edge_ceilings(sb))
            if _EDGE_CEILINGS:
                log.info("Plafonds d'edge appris : %s",
                         " | ".join(f"{k}<={v:.1f}%" for k, v in _EDGE_CEILINGS.items()))
            _ODDS_CEILINGS.update(_load_odds_ceilings(sb))
            if _ODDS_CEILINGS:
                log.info("Plafonds de cote appris : %s",
                         " | ".join(f"{k}<={v:.2f}" for k, v in _ODDS_CEILINGS.items()))
        except Exception as e:
            log.warning("load_edge_ceilings: %s — bornes globales seules", e)
        try:
            sport_ranking = _load_sport_ranking(sb)
            if sport_ranking:
                log.info("Classement sports (réussite ledger) : %s",
                         " > ".join(sport_ranking))
        except Exception as e:
            log.warning("load_sport_ranking: %s — ordre par défaut", e)

    # ══ SOURCE PIPELINE — 3 NIVEAUX ══════════════════════════════════
    # Tier 1: The Odds API  → real 1XBet + Pinnacle, même event (idéal)
    # Tier 2: Recherche web → batch Pinnacle (groq/compound-mini + Tavily)
    # Tier 3: Estimateur IA → probabilités internes, toujours disponible

    matches        = []
    xbet_matches   = []   # declared here so Tier 3 can reuse Tier 2's result safely
    no_pin_count   = 0
    sharp_source   = "?"
    tier1_ok       = False  # OddsAPI a-t-il rendu quelque chose ? Voir le Tier 2.

    # ── Tier 1: The Odds API ──────────────────────────────────────────
    if REPRICE:
        hours_ahead = int(os.environ.get("HOURS_AHEAD", 24))
        log.info("💹 REPRICE — slate soft en cache + Matchbook frais, zéro source payante")
    elif GUERRILLA:
        hours_ahead = int(os.environ.get("HOURS_AHEAD", 48))
        log.info("🥷 GUERRILLA — OddsAPI ignoré, Tier 2 direct (1XBet + Pinnacle/recherche web)")
    elif not ODDS_API_ENABLED:
        # OddsAPI obsolète : le Tier 1 ne s'exécute plus. GOLDEN_HOUR garde
        # sa fenêtre T-120min, qui a du sens pour les sources gratuites aussi
        # (c'est l'approche du coup d'envoi qu'elle vise, pas un fournisseur).
        hours_ahead = 2 if GOLDEN_HOUR else int(os.environ.get("HOURS_AHEAD", 24))
        log.info("🚫 OddsAPI OBSOLÈTE — Tier 1 éteint, sources gratuites "
                 "uniquement (%dh window)%s",
                 hours_ahead, " | GOLDEN HOUR" if GOLDEN_HOUR else "")
    elif GOLDEN_HOUR:
        hours_ahead  = 2  # T-120min window only
        scan_keys    = GOLDEN_SPORT_KEYS
        log.info("⚡ GOLDEN HOUR — OddsAPI (%dh window) | %d sports ciblés: %s",
                 hours_ahead, len(scan_keys), " ".join(scan_keys.keys()))
    else:
        # Fenêtre ramenée 72h/48h → 24h le 2026-08-04. Deux mesures, pas une
        # intuition :
        #
        # 1) COÛT — le tarif OddsAPI se paie par LIGUE peuplée, pas par match.
        #    Relevé sur les 19 sport-keys via /events (endpoint gratuit) :
        #    24h → 4 ligues = 9 crédits | 48h → 7 ligues = 18 | 72h → 7 = 18.
        #    72h et 48h coûtaient donc STRICTEMENT la même chose : seul le
        #    passage à 24h coupe la facture (moitié). Avec un plan 500/mois
        #    (voir core/odds_api.py), ça double la durée de vie d'une clé.
        #
        # 2) RENDEMENT — ai_learning_ledger, 201 signaux réglés depuis le
        #    2026-07-06, découpés par avance à l'émission :
        #      2-24h  : 90 réglés, 60,0% de réussite, ROI +8,6%  ← la zone utile
        #      24-48h : 24 réglés, 20,8% pour 51,2% requis, ROI -62,8% (p=0,0023)
        #      >48h   : 17 signaux, 17 'expired', ZÉRO jamais réglé
        #    Au-delà de 24h on payait donc soit des paris perdants, soit des
        #    signaux qui expiraient avant le coup d'envoi.
        #
        # Ce n'est PAS le gouverneur de quota supprimé le 2026-08-01 (décision
        # opérateur « ne pas rationner ») : aucun signal rentable n'est sacrifié
        # ici, on cesse d'acheter une tranche mesurée déficitaire. Élargir reste
        # possible sans toucher au code via HOURS_AHEAD — le filtre J+72h de
        # _emit() plus haut reste en place précisément pour ce cas.
        hours_ahead = int(os.environ.get("HOURS_AHEAD", 24))
        scan_keys   = None  # Use default SPORT_KEYS (19 ligues)
        log.info("⚡ Tier 1 — The Odds API (%dh window)...", hours_ahead)

    # ODDS_API_ENABLED en tête : obsolète = aucun appel, et surtout AUCUNE
    # alerte de pool. Les alertes vivent dans ce bloc, donc les éteindre est
    # automatique — c'est voulu : un pool mort n'est plus une panne, c'est
    # l'état nominal. Sans ça, Telegram recevrait « rotation requise » à
    # chaque scan, pour toujours (même leçon que le mode REPRICE muet).
    if ODDS_API_ENABLED and not GUERRILLA and not REPRICE:
        spend_policy = _build_spend_policy(sb, now)
        oddsapi_events = fetch_odds(hours_ahead=hours_ahead, sport_keys=scan_keys,
                                    spend_policy=spend_policy)
        if spend_policy is not None and spend_policy.skipped:
            log.info("DÉPENSE | %d ligue(s) peuplée(s) non payée(s) ce scan : %s",
                     len(spend_policy.skipped),
                     ", ".join(k for k, _ in spend_policy.skipped))
        _alert_oddsapi_pool_levels(sb)
        if not oddsapi_events:
            _alert_oddsapi_pool_if_dead(sb)
        if oddsapi_events:
            matches      = oddsapi_events[:MAX_MATCHES]
            tier1_ok     = True
            sharp_source = "OddsAPI/Pinnacle"
            sports_found = set(e.get("sport","?") for e in matches)
            log.info("✅ Tier 1 OK — %d/%d events | sports: %s",
                     len(matches), len(oddsapi_events), " ".join(sorted(sports_found)))

            # ── Closing line — free ride on the payload we just paid for ──
            # Runs on the FULL event list, before MAX_MATCHES truncation and
            # before the portfolio balancer: pricing bets already placed is
            # unrelated to how many new ones this scan may emit. Golden Hour's
            # T-120min window means these scans sit squarely inside the
            # closing-line neighbourhood, and unlike run_closing_line.py's
            # oracle this prices totals/spreads too — the markets that were
            # structurally unmeasurable until now (see core/closing_line.py).
            if sb:
                try:
                    capture_from_scan(sb, oddsapi_events, now)
                except Exception as e:
                    log.warning("Closing-line capture: %s", e)


    # ── REPRICE : le slate soft vient du cache, pas d'une source payante ──
    # Sortie AVANT le fetch Matchbook et sans AUCUNE alerte : un cache
    # vide/expiré n'est pas une panne (le prochain scan complet le remplit),
    # et un tick muet ne doit ni spammer Telegram ni toucher harvest_empty_at.
    if REPRICE:
        matches = (_get_cached(sb, "cache_soft_slate", _TTL_SOFT_SLATE) or []) if sb else []
        if not matches:
            log.info("💹 REPRICE — cache_soft_slate vide/expiré → exit (rien à repricer)")
            if sb:
                _heartbeat(sb, now, None, None)
            if credentials_failed:
                raise SystemExit(1)
            return
        log.info("💹 REPRICE — %d matchs soft relus du cache", len(matches))

    # ── Tier 1.5: exchanges (prix sharp pair-à-pair) ───────────────────
    # Remplace un prix Pinnacle ESTIMÉ par l'IA — ou absent — par un vrai
    # prix d'exchange. Deux fournisseurs, dans cet ordre :
    #
    #   Betfair   — seulement si BETFAIR_APP_KEY est posée. Rappel : la clé
    #               « Live » coûte 499 £ et Betfair refuse les IP américaines
    #               (BETTING_RESTRICTED_LOCATION), donc sur les runners
    #               GitHub cette branche ne s'exécute jamais en pratique.
    #   Matchbook — aucune clé, aucun compte, 700 req/min. Le milieu
    #               back/lay donne une marge d'environ 0,1 %, meilleure que
    #               Pinnacle (~2 %) : c'est une référence sharp de premier
    #               ordre, et la seule qui survive à un pool OddsAPI mort.
    #
    # Betfair reste prioritaire quand il répond (intégration historique,
    # prix ajustés de la commission) ; Matchbook comble le reste.
    betfair_prices: dict = {}
    if os.environ.get("BETFAIR_APP_KEY"):
        log.info("💹 Tier 1.5 — Betfair Exchange (commission -5%%)...")
        betfair_prices = fetch_betfair_prices(
            sports=["soccer", "tennis", "basketball", "hockey", "mma", "cricket"],
            hours_ahead=hours_ahead,
        )
        if betfair_prices:
            log.info("💹 Betfair OK — %d marchés Betfair chargés", len(betfair_prices))

    if not _MATCHBOOK_OFF:
        mb_prices = fetch_matchbook_prices(
            sports=["soccer", "basketball", "baseball", "hockey", "tennis", "mma"],
            hours_ahead=hours_ahead,
        )
        for _k, _v in mb_prices.items():
            betfair_prices.setdefault(_k, _v)   # Betfair d'abord s'il existe
        if mb_prices:
            log.info("💹 Matchbook OK — %d marchés sharp (total exchange : %d)",
                     len(mb_prices), len(betfair_prices))

    if betfair_prices:
        if _enrich_from_exchange(matches, betfair_prices, log) and \
                sharp_source in ("AI/Estimateur", "Aucune"):
            sharp_source = "Exchange+AI"

        # ── Closing line — free ride on the sharp prices we just loaded ────
        # Depuis l'obsolescence d'OddsAPI (2026-08-26) la voie gratuite et
        # exacte de capture_from_scan est morte : il ne restait que l'oracle
        # web-search, h2h favori, sur le budget Groq des scans. Ces prix
        # d'exchange sont déjà là, à chaque scan et REPRICE compris — c'est le
        # tick horaire, celui qui compte.
        #
        # On passe le dict de prix BRUT, pas `matches` enrichis : voir la
        # docstring de capture_from_exchange. _enrich_from_exchange n'écrase
        # `odds_pinnacle` que sur les matchs SANS prix sharp, or api-sports
        # sert Pinnacle sur 100 % de ses matchs foot — lire le match enrichi
        # stockerait le prix d'ENTRÉE comme prix de clôture, avec un CLV
        # nul, une exécution verte et aucune trace.
        if sb:
            try:
                capture_from_exchange(sb, matches, betfair_prices, now)
            except Exception as e:
                log.warning("Closing-line exchange: %s", e)
    else:
        log.info("💹 Exchange: 0 marché sharp (Betfair absent/refusé, Matchbook vide ou géobloqué)")

    # ── Golden Hour : PLUS de sortie anticipée sur « 0 event OddsAPI » ──
    # Jusqu'au 2026-09-01 un tick golden dont le Tier 1 rendait 0 event dans
    # T-2h sortait ici (« lignes stables »). Ce raisonnement datait d'un Tier 2
    # fait de recherche web (lente, rate-limitée). Depuis, le Tier 2 c'est
    # api-sports, odds-api.io, titan007 et Matchbook — les sources qui portent
    # TOUT le volume depuis l'obsolescence d'OddsAPI (2026-08-26). Rallumer
    # le Tier 1 avec cette garde aurait rendu ces sources muettes 24 fois par
    # jour dès que le pool est vide, hors fenêtre, ou simplement sans match
    # dans 2 h — exactement le no-op horaire déjà constaté en prod (run
    # 32965494280). Un Tier 1 vide descend donc au Tier 2 dans TOUS les
    # modes ; gardien : tests/test_oddsapi_obsolete.py.

    # ── REPRICE : seuls les matchs repricés par l'exchange continuent ────
    # Aucune recherche web n'est autorisée en reprice : un match que
    # Matchbook ne couvre pas (et sans Pinnacle réel conservé du scan
    # complet) est simplement écarté — fetch_pinnacle_prices n'est JAMAIS
    # atteint dans ce mode, le quota Groq/Tavily reste au settlement.
    if REPRICE:
        before = len(matches)
        matches = [m for m in matches
                   if (m.get("odds_pinnacle") or {}).get("1", 0) > 1.01
                   and (m.get("odds_pinnacle") or {}).get("2", 0) > 1.01]
        if before - len(matches):
            log.info("💹 REPRICE — %d/%d matchs sans prix sharp écartés "
                     "(aucune recherche web en reprice)", before - len(matches), before)
        if not matches:
            log.info("💹 REPRICE — 0 match repricé par l'exchange → exit")
            if sb:
                _heartbeat(sb, now, None, None)
            if credentials_failed:
                raise SystemExit(1)
            return
        sharp_source = "Matchbook/Reprice"

    # ── Tier 2: recherche web (Groq/Tavily) — activé si OddsAPI vide/GUERRILLA ──
    # Gardé sur `tier1_ok` et non sur `matches` : les blocs MMA/eSports/sports
    # alternatifs ci-dessus alimentent `matches` AVANT ce test, donc un seul
    # combat trouvé suffisait à sauter tout le harvest Melbet. Run 30768093911 :
    # 1 combat UFC remonté → 0 match foot/tennis/basket scanné, 0 signal, là où
    # le run précédent (0 combat) en avait sorti 12 matchs et 1 signal.
    # REPRICE saute TOUT le tier : coupe-circuit ni lu ni écrit, budgets des
    # sources payantes intacts, aucune alerte « 0 matchs ».
    if not tier1_ok and not REPRICE:
        # Coupe-circuit : une tentative vide il y a < HARVEST_EMPTY_TTL_H ne
        # se rejoue pas — voir le bloc « Horodatages meta » plus haut. Quand
        # LineFeed, Tavily et Groq sont morts ensemble, 40 runs/jour ne
        # trouveront pas plus que 8, mais brûleront le quota du settlement.
        skipped_age = _harvest_recently_empty(sb)
        if skipped_age is not None:
            # Le coupe-circuit ne vise QUE le harvest coûteux (LineFeed +
            # recherche web Groq/Tavily, dont le quota est partagé avec le
            # settlement). api-sports est authentifié par clé, gratuit, et
            # dispose d'un quota PROPRE par sport : le sauter ne protège
            # rien et prive le scan de sa dernière source réelle. Constaté
            # en production le 2026-08-20 (run 18:30) — le coupe-circuit
            # posé le matin même court-circuitait api-sports par ricochet.
            log.warning("📡 Tier 2 — harvest web SAUTÉ : dernière tentative vide il y a "
                        "%.1fh (< %.0fh) — quota Groq/Tavily préservé ; api-sports "
                        "et odds-api.io restent interrogés (authentifiés par clé, "
                        "quotas séparés, gratuits)",
                        skipped_age, _HARVEST_EMPTY_TTL_H)
            # Titan007 fait partie du chemin économique pour la même raison
            # qu'api-sports/odds-api.io : budget journalier propre, aucun
            # quota partagé avec Groq/Tavily. L'oublier ici reproduit le bug
            # corrigé par a0767c8 (source saine court-circuitée par ricochet).
            xbet_matches = (_api_sports_all(hours_ahead=hours_ahead)
                            + _odds_api_io_all(hours_ahead=hours_ahead)
                            + _titan007_fetch(hours_ahead=hours_ahead))
        else:
            log.info("📡 Tier 2 — Harvest Melbet + api-sports + recherche web Pinnacle...")
            xbet_matches = fetch_matches()
            _note_harvest_result(sb, xbet_matches)
        # Abandon seulement si RIEN n'a été trouvé nulle part : depuis que ce
        # tier n'est plus gardé par `matches`, il peut s'exécuter alors que la
        # recherche MMA/eSports/alternatifs a déjà rempli `matches`. Sortir ici
        # jetterait ces événements-là.
        if not xbet_matches and not matches:
            if ODDS_API_ENABLED:
                msg = "📡 PREDATOR: 0 matchs trouvés — Tier 1 vide et harvest (1xbet/Melbet/API-Football/recherche web) sans résultat."
                # Cause OddsAPI : n'a de sens que si on l'interroge encore.
                # Obsolète, un pool mort n'explique plus rien — l'afficher
                # enverrait chercher une clé dont le pipeline n'a plus besoin.
                st = _odds_pool_status()
                if st["total"] and st["live"] == 0:
                    msg += (f"\n🔑 CAUSE : {st['dead']}/{st['total']} clé(s) OddsAPI épuisée(s) "
                            f"({st['reason']}) — rotation requise : "
                            f"`python scripts/rotate_odds_key.py --add <clé>`")
            else:
                msg = ("📡 PREDATOR: 0 matchs trouvés — sources gratuites "
                       "(api-sports, odds-api.io, titan007, Matchbook, harvest soft) "
                       "sans résultat. OddsAPI est obsolète : ce n'est PAS une "
                       "histoire de clé.")
            if gemini_quota_dead():
                msg += "\n⚠️ Quota IA journalier épuisé (Groq) — fallback recherche web indisponible."
            log.warning(msg)
            # Dédupliqué : ce message partait à CHAQUE run (40/jour) sans
            # jamais nommer la cause ; une fois par _ALERT_TTL_H avec la cause
            # vaut mieux que 40 fois sans.
            _alert_once(sb, "alert_no_matches", msg)
            if sb:
                _heartbeat(sb, now, 0, 0)
            # CONTRAT DE FIN (B5). Zéro match n'est un échec que si des sources
            # ont RÉPONDU : un créneau réellement creux reste vert. Matchbook
            # sert de témoin — gratuit, illimité, il rend 141 à 202 marchés
            # quand le réseau et le pipeline vont bien (INCIDENTS.md). Des marchés
            # chargés et zéro match, c'est nous qui avons perdu la donnée.
            _terminer_run(verdict_de_fin(
                sources_joignables=bool(betfair_prices),
                matches_vus=0), contexte="scan")
            if credentials_failed:
                raise SystemExit(1)
            return

        # Second passage de l'exchange : les matchs du Tier 2 viennent
        # d'apparaître, ils n'existaient pas lors du premier. Fait AVANT la
        # recherche web pour que chaque match servi ici n'y soit pas envoyé.
        if xbet_matches and betfair_prices:
            _enrich_from_exchange(xbet_matches, betfair_prices, log)

        if xbet_matches:
            log.info("%d matchs Melbet | Requête Pinnacle → recherche web...", len(xbet_matches))
        # Les matchs qui arrivent DÉJÀ prixés côté sharp (API-Football livre
        # Pinnacle dans la même réponse) ne passent pas par la recherche web.
        pinnacle_map = fetch_pinnacle_prices([m for m in xbet_matches if not m.get("odds_pinnacle")])

        MAX_ORACLE = _MAX_ORACLE
        oracle_used = 0
        # Les sports hors OddsAPI passent devant : un match de foot écarté ici
        # sera repris par le prochain scan Tier 1, un combat UFC ne le sera
        # jamais. L'ordre de `matches` n'a pas d'incidence en aval,
        # _portfolio_balance() re-trie par edge décroissant.
        # Melbet expose le MMA (sport_id=5) et OddsAPI (mma_mixed_martial_arts)
        # livre la même carte : les deux sources peuvent rendre le même
        # événement. Le doublon compterait deux fois dans le quota par sport
        # de _portfolio_balance().
        seen = {m.get("match", "").strip().lower() for m in matches}
        # Ordre de dépense du budget : (1) les sports hors OddsAPI, seule
        # occasion qu'ils auront jamais d'être prixés ; (2) à égalité, le sport
        # dont le ledger montre la meilleure réussite. Un sport absent du
        # classement (historique insuffisant) se place entre les deux plutôt
        # qu'en dernier — il est INCONNU, pas mauvais, et le reléguer
        # l'empêcherait d'acquérir l'historique qui le départagerait.
        def _oracle_rank(m: dict) -> tuple[int, int]:
            sport = m.get("sport") or ""
            tier = 0 if sport in _NO_ODDSAPI_SPORTS else 1
            try:
                return tier, sport_ranking.index(sport)
            except ValueError:
                return tier, len(sport_ranking)

        oracle_order = sorted(
            (m for m in xbet_matches[:MAX_MATCHES]
             if m.get("match", "").strip().lower() not in seen),
            key=_oracle_rank,
        )
        for m in oracle_order:
            pin_odds = m.get("odds_pinnacle") or pinnacle_map.get(m["match"])
            if pin_odds:
                m["odds_pinnacle"] = pin_odds
                matches.append(m)
            elif oracle_used < MAX_ORACLE:
                oracle_used += 1  # count the attempt, not just successes — else a run of
                                  # failures never trips MAX_ORACLE and every remaining
                                  # match falls through to an uncapped oracle call
                sport = m.get("sport", "soccer")
                pin_price, pin_team = get_pinnacle_price(
                    m["match"], sport=sport, league=m.get("league", "")
                )
                if pin_price and pin_price > 1.01:
                    m["_oracle_price"] = pin_price
                    m["_oracle_team"]  = pin_team or ""
                    matches.append(m)
                    log.info("ORACLE  | %s — %.3f", m["match"], pin_price)
                else:
                    no_pin_count += 1
                    log.warning("⚠️ %s ignoré : Échec prix Sharp", m["match"])
            else:
                no_pin_count += 1
                log.warning("⚠️ %s ignoré : Échec prix Sharp", m["match"])

        if matches:
            sharp_source = "Search/Pinnacle"
            log.info("✅ Tier 2 OK — %d matchs avec prix Sharp", len(matches))

    # ── Tier 3: Estimateur IA — fallback direct si Tier 1 vide ───
    if not matches:
        # MMA a déjà appelé la recherche web au-dessus — petite pause anti rate-limit
        time.sleep(20)
        log.info("🧠 Tier 3 — Estimateur IA (connaissance interne, marge 2%%)...")
        if not xbet_matches:
            xbet_matches = fetch_matches()
        if not xbet_matches:
            msg = "📡 PREDATOR v8.8: 0 matchs — toutes sources épuisées."
            if gemini_quota_dead():
                msg += "\n⚠️ Quota IA journalier épuisé (Groq)."
            log.warning(msg)
            _telegram(msg)
            if sb:
                _heartbeat(sb, now, 0, 0)
            if credentials_failed:
                raise SystemExit(1)
            return
        estimated_map = fetch_estimated_prices(xbet_matches)
        for m in xbet_matches[:MAX_MATCHES]:
            est_odds = estimated_map.get(m["match"])
            if est_odds:
                m["odds_pinnacle"] = est_odds
                m["_estimated"]    = True
                matches.append(m)
        if matches:
            sharp_source = "AI/Estimateur"
            log.info("✅ Tier 3 OK — %d matchs estimés (non-arbitrage, value)", len(matches))

    if not matches:
        msg = "📡 PREDATOR v8.8: 0 signaux — toutes sources épuisées."
        if gemini_quota_dead():
            msg += "\n⚠️ Quota IA journalier épuisé (Groq)."
        log.warning(msg)
        _telegram(msg)
        if sb:
            _heartbeat(sb, now, 0, 0)
        if credentials_failed:
            raise SystemExit(1)
        return

    # ── Photographie du slate soft pour le mode REPRICE ──────────────────
    # Chaque scan COMPLET (engine/deep/guerrilla) écrit ici. Ni golden hour
    # (fenêtre 2h, slate partiel — il ÉCRASERAIT le slate 24h du dernier
    # scan complet), ni reprice (ré-écrire rafraîchirait updated_at à chaque
    # tick horaire : le TTL ne serait jamais atteint et des cotes soft
    # mortes seraient repricées indéfiniment).
    if sb and not GOLDEN_HOUR and not REPRICE:
        try:
            slate = _trim_soft_slate(matches)
            _set_cached(sb, "cache_soft_slate", slate)
            log.info("💹 Slate soft photographié pour REPRICE — %d matchs", len(slate))
        except Exception as e:
            log.warning("cache_soft_slate: %s", e)

    candidates = []

    for m in matches:
        try:
            # Contre-expertise d'exchange (A5) : deux avis sharp indépendants
            # en désaccord ne peuvent pas être tous les deux à jour. Le refus
            # porte sur le MATCH ENTIER, pas sur un marché : c'est le prix de
            # référence qui est suspect, donc h2h, totals et spreads le sont
            # tous les trois. Filtrer ici plutôt que dans chaque _process_*
            # garantit qu'aucun marché futur ne puisse s'y soustraire.
            conflit = m.get("_sharp_conflict")
            if conflit:
                log.info("SKIP    | %s — conflit sharp %.2f pts > %.2f, aucun "
                         "marché évalué", m.get("match", "?"),
                         conflit["pts"], conflit["limite"])
                continue

            name     = m["match"]
            sport    = m.get("sport", "soccer")
            league   = m.get("league", "")
            home     = m.get("home", "")
            away     = m.get("away", "")
            emoji    = SPORT_EMOJI.get(sport, "🎯")

            # ── H2H market ───────────────────────────────────────
            _process_h2h(m, name, sport, league, home, away, emoji,
                         candidates, sb, now, log,
                         min_edge=_segment_min_edge(dyn_thresholds, dyn_segment_thresholds, sport, "h2h"))

            # ── Totals market (Over/Under) ────────────────────────
            if "totals_1xbet" in m and "totals_pinnacle" in m:
                _process_totals(m, name, sport, league, emoji,
                                candidates, sb, now, log,
                                min_edge=_segment_min_edge(dyn_thresholds, dyn_segment_thresholds, sport, "totals"))

            # ── Spreads market (Handicap) ─────────────────────────
            if "spreads_1xbet" in m and "spreads_pinnacle" in m:
                _process_spreads(m, name, sport, league, home, away, emoji,
                                 candidates, sb, now, log,
                                 min_edge=_segment_min_edge(dyn_thresholds, dyn_segment_thresholds, sport, "spreads"))

        except Exception as e:
            log.error("Match error [%s]: %s", m.get("match", "?"), e)
            continue

    # ── Portfolio Balancer — apply quota + alpha priority ─────────────
    signals = _portfolio_balance(candidates)
    discarded = len(candidates) - len(signals)
    if discarded:
        log.info("Portfolio Balancer: %d candidates → %d kept (%d quota-trimmed)",
                 len(candidates), len(signals), discarded)
    sport_counts = {}
    for s in signals:
        sport_counts[s.get("sport", "?")] = sport_counts.get(s.get("sport", "?"), 0) + 1
    if sport_counts:
        log.info("Allocation: %s", " | ".join(
            f"{SPORT_EMOJI.get(sp,'🎯')} {sp}={n}" for sp, n in sport_counts.items()))

    # ── B. Bulk-save balanced signals to Supabase ─────────────────────
    saved_count = 0
    if sb and signals:
        for s in signals:
            if _save(sb, s):
                saved_count += 1
        log.info("Supabase: %d/%d signals persisted", saved_count, len(signals))
        if saved_count == 0:
            log.error("All %d signals failed to persist to Supabase — Telegram will still send them", len(signals))

    # ── C. Telegram — combo-only, tax-viable systems (PAIM v9.5, Task 2) ──
    # Individual signals are still persisted to Supabase above for
    # settlement/learning regardless of what gets announced here — Telegram
    # itself now only ever recommends one tax-viable accumulator per time
    # window (or nothing for that window), instead of alerting every signal
    # as an independent bet. Shadow Layer: execution jitter (1.5–4.0s).
    time.sleep(random.uniform(1.5, 4.0))

    # Task 7 — circuit breaker: a rolling drawdown blowup pauses emission
    # entirely until a human clears it (core.risk_manager.resume_emission).
    # Checked here, not upstream, so scanning/persisting/learning keep
    # running as normal — only the outward Telegram recommendation stops.
    if sb and _risk_manager.check_circuit_breaker(sb):
        log.critical("Circuit breaker active — system emission paused, manual resume required")
        _telegram(
            f"🔴 *PAUSE AUTOMATIQUE* · {now.strftime('%H:%M')} UTC\n"
            f"Drawdown roulant > {_risk_manager.DRAWDOWN_LIMIT_PCT*100:.0f}% sur les "
            f"{_risk_manager.DRAWDOWN_WINDOW_N} derniers signaux réglés.\n"
            f"Émission de nouveaux systèmes suspendue — reprise manuelle uniquement."
        )
    else:
        # Per-sport circuit breaker: a sport in its own genuine bad streak
        # (core.risk_manager.check_circuit_breaker_by_sport) stops
        # recommending on its own, without pausing every other sport — the
        # global check above can dilute/hide that. Signals still persist to
        # Supabase for settlement/learning either way (see comment above);
        # only the outward Telegram recommendation is filtered.
        emit_signals = signals
        if sb:
            paused_sports = {s for s in {sig.get("sport") for sig in signals}
                             if s and _risk_manager.check_circuit_breaker_by_sport(sb, s)}
            if paused_sports:
                emit_signals = [s for s in signals if s.get("sport") not in paused_sports]
                log.warning("Sport circuit breaker active for %s — excluded from Telegram systems",
                           ", ".join(sorted(paused_sports)))
                _telegram(
                    f"🟠 *PAUSE PARTIELLE* · {now.strftime('%H:%M')} UTC\n"
                    f"Sport(s) en pause: `{', '.join(sorted(paused_sports))}` "
                    f"(drawdown roulant > {_risk_manager.DRAWDOWN_LIMIT_PCT*100:.0f}%).\n"
                    f"Autres sports non affectés — reprise manuelle uniquement pour ces sports."
                )
        # Mode FANTÔME — segments mesurés déficitaires (voir SHADOW_SPORTS /
        # SHADOW_GOLDEN_HOUR en tête de fichier pour les chiffres). Filtré
        # ici, au même endroit et pour la même raison que le disjoncteur
        # ci-dessus : tout reste persisté, réglé et appris, seule la
        # recommandation sortante s'arrête. Pas de message Telegram — un
        # fantôme est un réglage permanent, pas un incident à annoncer à
        # chaque scan.
        emit_signals, shadowed = _shadow_partition(emit_signals, GOLDEN_HOUR)
        if shadowed:
            by_sport: dict[str, int] = {}
            for s in shadowed:
                by_sport[s.get("sport", "?")] = by_sport.get(s.get("sport", "?"), 0) + 1
            log.info("FANTÔME | %d signaux mesurés mais non recommandés (%s)%s",
                     len(shadowed),
                     " ".join(f"{sp}={n}" for sp, n in sorted(by_sport.items())),
                     " — golden_hour intégral" if (SHADOW_GOLDEN_HOUR and GOLDEN_HOUR) else "")

        if shadowed and not emit_signals:
            # Run intégralement fantôme (cas normal de golden_hour, 24×/jour).
            # Surtout NE PAS appeler _telegram_systems ici : sur une liste vide
            # il annonce « Aucun pari de valeur », ce qui serait un mensonge —
            # il y en avait, on a décidé de ne pas les recommander — et ce
            # serait 24 notifications/jour de bruit.
            log.info("FANTÔME | run intégralement fantôme — aucun message Telegram")
        else:
            systems = _suggest_systems_by_window(emit_signals, log, sb)
            systems = _last_look_reprice(systems, log)
            already_announced = len(systems)
            systems = _dedup_systems_for_telegram(sb, systems)
            already_announced -= len(systems)
            if REPRICE and not systems:
                # Un tick REPRICE sans rien de NEUF se tait — sinon le mode
                # horaire enverrait ~23 « Aucun pari de valeur »/jour, ou
                # ré-annoncerait le même combo à chaque tick.
                log.info("💹 REPRICE — rien de neuf à annoncer (%d déjà signalé(s))",
                         already_announced)
            else:
                _telegram_systems(systems, now, session, len(matches), sharp_source, no_pin_count)

    elite = [s for s in signals if s["edge_pct"] >= ELITE_EDGE]
    log.info("Done. %d candidates | %d balanced | %d elite.",
             len(candidates), len(signals), len(elite))
    log.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    if sb:
        _heartbeat(sb, now, len(matches), len(signals))

    # CONTRAT DE FIN (B5). `saved_count` ne vaut 0 avec `signals` non vide que
    # si CHAQUE écriture a échoué — c'est l'incident du 2026-07-07, ~17 h de
    # « 0/N signals persisted » en vert, chaque écriture refusée une par une
    # par une RLS 42501. Le message d'erreur existait déjà ; c'est le code de
    # sortie qui manquait.
    _terminer_run(verdict_de_fin(signaux_emis=len(signals),
                                 signaux_persistes=saved_count),
                  contexte="scan")

    if credentials_failed:
        # Telegram already sent above — now fail the job so GitHub Actions
        # shows red instead of a silent green run that persisted nothing.
        raise SystemExit(1)


if __name__ == "__main__":
    run()
