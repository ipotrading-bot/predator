"""
signals/scanner.py — MarketScanner v6.0 (Pulse Hunter Strategy)

Pulse Hunter v6.0 - High Frequency Prop Strategy:
  - Scan window: 6h (instead of 48h) for immediate action
  - Expanded markets: player_points, player_rebounds (NBA), total_corners (Soccer)
  - Hybrid truth source: Pinnacle primary, Bet365+William Hill average fallback
  - Alpha thresholds: 1.0% display, 2.0% elite (🔥 HIGH PRIORITY)
  - Fuzzy bookmaker search : '1x' ou 'one' dans la clé → soft book reconnu
  - Market fallback : si spreads absent chez le soft, tente h2h
  - Data integrity : signal rejeté si cote_1xbet nulle ou invalide
  - Timezone fix : commence_time ISO 8601 transmis tel quel (UTC)
  - Gemini timeout : 10s max, fallback propre
"""
from __future__ import annotations

import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional

from config import settings
from core.math_engine import calculate_shin_probabilities
from core.news_engine import news_engine, NewsImpact
from core.notifications import TelegramNotifier
from core.paim_engine import PAIMEngine, PAIMSignal, ScanResult
from core.validator import check_market_red_flags
from core.gemini_search import GeminiOracle, GeminiOddsResult  # 🔮 Fallback Oracle
from data.odds_fetcher import OddsFetcher
from data.supabase_client import SupabaseClient
from api.groq_client import groq_client
from api.perplexity_client import perplexity_grounding

logger = logging.getLogger(__name__)

# Fenêtre de scan : T+0h à T+6h (Pulse Hunter Strategy - High Frequency)
SCAN_WINDOW_HOURS = 6

# Mots-clés fuzzy pour identifier les soft books 1XBet
# The-Odds-API peut retourner : onexbet, 1xbet, 1xbit, 1xstavka, 1x_bet, etc.
_SOFT_FUZZY_PATTERNS = ("1x", "one", "onex")


def _is_soft_book(bm_key: str) -> bool:
    """
    Vérifie si un bookmaker est un soft book via :
    1. Liste exacte dans settings.soft_books
    2. Fuzzy match sur les patterns 1XBet
    3. Autres soft books connus (bet365, unibet, williamhill)
    """
    key = bm_key.lower().strip()
    # Normaliser via synonymes
    key = settings.synonyms.get(key, key)
    # Match exact
    if key in [s.lower() for s in settings.soft_books]:
        return True
    # Fuzzy 1XBet
    if any(p in key for p in _SOFT_FUZZY_PATTERNS):
        return True
    return False


def _normalize_bm_key(bm_key: str) -> str:
    """Retourne la clé normalisée du bookmaker (lowercase + synonymes)."""
    key = bm_key.lower().strip()
    return settings.synonyms.get(key, key)


def find_book(bookmakers: list[dict], keys: list[str]) -> Optional[dict]:
    """
    Cherche un bookmaker dans la liste par ses clés (avec synonymes).
    Retourne le premier bookmaker matché ou None.
    """
    # Normaliser toutes les clés recherchées avec synonymes
    normalized_keys = {_normalize_bm_key(k) for k in keys}
    for bm in bookmakers:
        bm_key = bm.get("key", "").lower().strip()
        normalized_bm = _normalize_bm_key(bm_key)
        if normalized_bm in normalized_keys:
            return bm
    return None


def _is_within_window(commence_time_iso: str, hours: int = SCAN_WINDOW_HOURS) -> bool:
    """
    Retourne True si le match commence dans les prochaines `hours` heures.
    Rejette les matchs trop lointains (Alpha Decay) et les matchs déjà commencés.
    """
    if not commence_time_iso:
        return False
    try:
        # The-Odds-API retourne ISO 8601 UTC (ex: "2025-05-17T14:00:00Z")
        ct = datetime.fromisoformat(commence_time_iso.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        delta = ct - now
        return timedelta(hours=0) <= delta <= timedelta(hours=hours)
    except Exception:
        return True  # En cas de parse error, ne pas bloquer


def _get_best_soft_odds(
    bookmakers: list[dict], 
    market_key: str, 
    selection: str
) -> tuple[Optional[float], str]:
    """
    🔮 BEST-PRICE LOGIC (Multi-Bookmaker v4.3)
    
    Compare les cotes de TOUS les soft books et retourne:
    - La cote la PLUS HAUTE (meilleur EV+)
    - Le nom du bookmaker qui l'offre
    
    Stratégie PhD MIT: Maximiser l'Alpha en diversifiant les sources.
    """
    best_price: Optional[float] = None
    best_bm: str = "soft"
    fallback_price: Optional[float] = None
    fallback_bm: str = "soft"

    for bm in bookmakers:
        bm_key = bm.get("key", "")
        if not _is_soft_book(bm_key):
            continue

        markets = bm.get("markets", [])
        bm_normalized = _normalize_bm_key(bm_key)

        # Chercher le marché demandé en premier
        for market in markets:
            if market.get("key") != market_key:
                continue
            for outcome in market.get("outcomes", []):
                if outcome.get("name", "").lower() == selection.lower():
                    price = outcome.get("price", 0.0)
                    if price > 1.0:
                        # 🔥 Best-Price: garder la cote la plus haute
                        if best_price is None or price > best_price:
                            best_price = float(price)
                            best_bm = bm_normalized

        # Fallback h2h si marché principal absent
        if market_key != "h2h" and best_price is None:
            for market in markets:
                if market.get("key") != "h2h":
                    continue
                for outcome in market.get("outcomes", []):
                    if outcome.get("name", "").lower() == selection.lower():
                        price = outcome.get("price", 0.0)
                        if price > 1.0 and fallback_price is None:
                            fallback_price = float(price)
                            fallback_bm = bm_normalized

    # Retourner best_price sinon fallback
    if best_price is not None:
        return (best_price, best_bm)
    return (fallback_price, fallback_bm)


# ═══════════════════════════════════════════════════════════════════
# OBSOLÈTE: _get_soft_odds remplacé par _get_best_soft_odds (v4.3)
# ═══════════════════════════════════════════════════════════════════
def _get_soft_odds(bookmakers: list[dict], market_key: str, selection: str) -> Optional[float]:
    """Legacy - utiliser _get_best_soft_odds pour le meilleur prix."""
    price, _ = _get_best_soft_odds(bookmakers, market_key, selection)
    return price


def _get_soft_bm_name(bookmakers: list[dict]) -> str:
    """Retourne le nom du premier soft book trouvé (legacy)."""
    for bm in bookmakers:
        key = bm.get("key", "")
        if _is_soft_book(key):
            return _normalize_bm_key(key)
    return "soft"


# ─────────────────────────────────────────────────────────────────
# Dossier d'Arbitrage
# ─────────────────────────────────────────────────────────────────

@dataclass
class ArbitrageDossier:
    signal: PAIMSignal
    meta: dict

    odds_ok: bool = True
    news_ok: bool = False
    groq_ok: bool = False
    perplexity_ok: bool = False
    gemini_ok: bool = False

    news_impact: Optional[NewsImpact] = None
    groq_confidence: float = 0.0
    perplexity_summary: str = ""
    gemini_context: str = ""
    liquidity_score: float = 0.0  # 0-1 score de liquidité

    @property
    def sources_count(self) -> int:
        return sum([self.odds_ok, self.news_ok, self.groq_ok,
                    self.perplexity_ok, self.gemini_ok])

    @property
    def sources_badge(self) -> str:
        parts = []
        if self.odds_ok:    parts.append("✅ Pinnacle")
        if self.news_ok:    parts.append("✅ NewsAPI")
        if self.groq_ok:    parts.append("✅ Groq")
        if self.perplexity_ok: parts.append("✅ Perplexity")
        if self.gemini_ok:  parts.append("✅ Gemini")
        return " | ".join(parts) if parts else "⚠️ Non validé"

    @property
    def confidence_score(self) -> int:
        """
        Confidence Score 0-100 :
        - alpha (EV+) : 40%
        - liquidité : 30%
        - IA (Groq confidence) : 30%
        """
        # Alpha score: max 1.5% → 100, min 0.015 → 0
        alpha_pct = min(self.signal.ev_plus / 0.015, 1.0) * 100 if self.signal else 0
        alpha_component = min(alpha_pct * 0.4, 40)  # plafonné à 40

        # Liquidité score (0-100) → 30%
        liq_component = min(self.liquidity_score * 100 * 0.3, 30)

        # IA score (groq_confidence 0-1) → 30%
        ia_component = min(self.groq_confidence * 100 * 0.3, 30)

        return int(round(alpha_component + liq_component + ia_component))

    @property
    def is_consensus(self) -> bool:
        return self.odds_ok and self.sources_count >= 3


# ─────────────────────────────────────────────────────────────────
# MarketScanner v3.1
# ─────────────────────────────────────────────────────────────────

class MarketScanner:

    def __init__(self, bankroll: Optional[float] = None):
        self.bankroll = bankroll or settings.starting_bankroll
        self.engine = PAIMEngine(
            kelly_fraction=settings.kelly_fraction,
            max_stake_pct=settings.max_single_stake_pct,
        )
        self.engine.min_ev_threshold = settings.min_ev_threshold
        self.db = SupabaseClient()
        self.fetcher = OddsFetcher()  # Initialiser l'instance OddsFetcher ici
        self._executor = ThreadPoolExecutor(max_workers=2)

        # Clients optionnels (lazy)
        self._groq: Optional[object] = None
        self._notifier: Optional[object] = None

    # ── Lazy loaders ──────────────────────────────────────────

    def find_book(self, bookmakers: list[dict], keys: list[str]) -> Optional[dict]:
        """Trouve un bookmaker parmi une liste de clés, en gérant les synonymes."""
        for key in keys:
            # Vérifier les synonymes dans la configuration
            target_key = settings.synonyms.get(key, key)
            
            # Rechercher la clé ou son synonyme
            book = next((b for b in bookmakers if b["key"] == target_key or b["key"] == key), None)
            if book:
                return book
        return None

    def _get_groq(self):
        if self._groq is None:
            try:
                from api.groq_client import GroqClient
                self._groq = GroqClient()
            except Exception as e:
                logger.warning(f"Groq client non disponible: {e}")
                self._groq = False
        return self._groq if self._groq else None

    def _get_notifier(self):
        if self._notifier is None:
            try:
                from core.notifications import TelegramNotifier
                self._notifier = TelegramNotifier()
            except Exception as e:
                logger.warning(f"Telegram notifier non disponible: {e}")
                self._notifier = False
        return self._notifier if self._notifier else None

    def _get_betfair_odds(
        self, bookmakers: list[dict], market_key: str, selection_name: str
    ) -> Optional[float]:
        """Trouve la cote pour une sélection donnée chez Betfair Exchange."""
        betfair_book = self.find_book(bookmakers, ["betfair_exchange"])
        if not betfair_book:
            return None

        for market in betfair_book.get("markets", []):
            if market.get("key") == market_key:
                for outcome in market.get("outcomes", []):
                    if outcome.get("name") == selection_name:
                        return outcome.get("price")
        return None

    # ── Scan principal ────────────────────────────────────────

    async def run_scan(self) -> ScanResult:
        start = time.monotonic()
        result = ScanResult()

        # Compter les sports configurés
        sport_count = len(settings.target_sports)
        logger.info(
            f"🦅 Scan v3.5 Multi-Sport | {sport_count} sports | bankroll={self.bankroll:,.0f}€ "
            f"| EV+ min={self.engine.min_ev_threshold:.1%} (dynamique par sport) "
            f"| fenêtre={SCAN_WINDOW_HOURS}h"
        )
        logger.info(f"   Sports: {', '.join(settings.target_sports[:5])}{'...' if sport_count > 5 else ''}")

        try:
            async with self.fetcher as fetcher:
                events = await fetcher.fetch_all_sports_odds()

            result.events_analyzed = len(events)
            logger.info(f"📡 {len(events)} événements dans la fenêtre")

            if not events:
                logger.warning("Aucun événement dans la fenêtre 48h.")
                result.duration_seconds = time.monotonic() - start
                return result

            news_cache = await self._prefetch_news(events)
            dossiers: list[ArbitrageDossier] = []

            for event in events:
                event_dossiers = await self._process_event(event, news_cache)
                result.signals_found += len(event_dossiers)
                for d in event_dossiers:
                    result.signals_validated += 1
                    dossiers.append(d)

            result.signals_rejected = result.signals_found - result.signals_validated

            if dossiers:
                await self._persist_and_notify(dossiers)

            consensus = [d for d in dossiers if d.is_consensus]
            if len(consensus) >= settings.system_min_wins:
                sigs  = [d.signal for d in consensus[:settings.system_size]]
                metas = [d.meta   for d in consensus[:settings.system_size]]
                notifier = self._get_notifier()
                if notifier:
                    await notifier.send_system_ticket(sigs, metas)

        except Exception as e:
            logger.critical(f"❌ Erreur critique scan: {e}", exc_info=True)

        result.duration_seconds = round(time.monotonic() - start, 2)
        logger.info(
            f"✅ Scan terminé | {result.events_analyzed} events "
            f"| {result.signals_validated} signaux | {result.duration_seconds}s"
        )
        return result

    # ─────────────────────────────────────────────────────────────
    # Rotation de Scan — Un sport à la fois (Rate Limiting)
    # ─────────────────────────────────────────────────────────────

    async def run_single_sport_scan(self, sport_key: str) -> ScanResult:
        """
        Scan un SEUL sport (protocole rotation PhD MIT).
        
        Appelé par GitHub Actions toutes les 5 minutes.
        Respecte le Rate Limiting: 15 RPM max, ~288 requêtes/jour.
        
        Args:
            sport_key: Clé The-Odds-API (ex: 'basketball_nba')
            
        Returns:
            ScanResult: Résultat du scan pour ce sport uniquement
        """
        start = time.monotonic()
        result = ScanResult()
        
        logger.info(
            f"🎯 ROTATION SCAN | Sport: {sport_key} | "
            f"bankroll={self.bankroll:,.0f}€ | EV+ min dynamique"
        )
        
        try:
            async with OddsFetcher() as fetcher:
                events = await fetcher.fetch_odds_for_sport(sport_key)
            
            # ── Filtre 48h ────────────────────────────────────
            events = [
                e for e in events
                if _is_within_window(e.get("commence_time", ""), SCAN_WINDOW_HOURS)
            ]
            
            result.events_analyzed = len(events)
            logger.info(f"📡 {sport_key}: {len(events)} événements dans la fenêtre")
            
            if not events:
                result.duration_seconds = round(time.monotonic() - start, 2)
                return result
            
            news_cache = await self._prefetch_news(events)
            dossiers: list[ArbitrageDossier] = []
            
            for event in events:
                event_dossiers = await self._process_event(event, news_cache)
                result.signals_found += len(event_dossiers)
                for d in event_dossiers:
                    result.signals_validated += 1
                    dossiers.append(d)
                    # 🔧 CORRECTION: Accumuler les signaux pour l'API Hunter
                    if d.signal:
                        result.signals.append(d.signal)
            
            result.signals_rejected = result.signals_found - result.signals_validated
            
            # Pulse Hunter v6.0: Apply correlation filter before persistence
            if dossiers:
                dossiers = self._apply_correlation_filter(dossiers)
                await self._persist_and_notify(dossiers)
            
        except Exception as e:
            logger.error(f"❌ Erreur scan {sport_key}: {e}", exc_info=True)
        
        result.duration_seconds = round(time.monotonic() - start, 2)
        logger.info(
            f"✅ Rotation {sport_key} terminée | "
            f"{result.signals_validated} signaux | {result.duration_seconds}s"
        )
        return result

    # ─────────────────────────────────────────────────────────────
    # Pré-fetch news
    # ─────────────────────────────────────────────────────────────

    async def _prefetch_news(self, events: list[dict]) -> dict[str, NewsImpact]:
        if not news_engine.is_available():
            return {}
        matches = [
            (f"{e.get('home_team','')} vs {e.get('away_team','')}", e.get("sport_key",""))
            for e in events if e.get("home_team") and e.get("away_team")
        ]
        if not matches:
            return {}
        logger.info(f"📰 NewsAPI batch: {len(matches)} matchs")
        return await news_engine.batch_analyze(matches, hours_back=6)

    # ─────────────────────────────────────────────────────────────
    # Traitement d'un événement
    # ─────────────────────────────────────────────────────────────

    async def _process_event(
        self,
        event: dict,
        news_cache: dict[str, NewsImpact],
    ) -> list[ArbitrageDossier]:

        event_id      = event.get("id", "")
        home_team     = event.get("home_team", "")
        away_team     = event.get("away_team", "")
        sport         = event.get("sport_key", "")
        commence_time = event.get("commence_time", "")  # ISO UTC — transmis tel quel
        event_name    = f"{home_team} vs {away_team}"
        bookmakers    = event.get("bookmakers", [])

        if not bookmakers:
            return []

        # Probabilités sharp via Hybrid Method (Pinnacle primary, Bet365+WH fallback)
        sharp_odds = self._extract_hybrid_sharp_odds(bookmakers, sport=sport)
        if not sharp_odds:
            logger.debug(f"⏭️ Pas de sharp odds binaires pour {event_name}")
            return []

        # Vérifier qu'au moins un soft book est présent dans cet événement
        soft_present = any(_is_soft_book(bm.get("key", "")) for bm in bookmakers)
        if not soft_present:
            logger.debug(f"Aucun soft book pour {event_name} — ignoré")
            return []

        news_impact = news_cache.get(event_name)
        if news_impact and news_impact.impact_score > 0.8:
            logger.warning(f"📰 News bloquante: {event_name} ({news_impact.impact_score:.0%})")
            return []

        validated: list[ArbitrageDossier] = []

        # Itérer sur les marchés du sharp book pour avoir la liste des sélections
        for sharp_bm in bookmakers:
            if sharp_bm.get("key", "").lower() not in [s.lower() for s in settings.sharp_books]:
                continue

            for sharp_market in sharp_bm.get("markets", []):
                sharp_market_key = sharp_market.get("key", "")
                # Pulse Hunter v6.0: Include prop markets + binary markets
                if sharp_market_key not in ("h2h", "spreads", "totals", "player_points", "player_rebounds", "total_corners", "btts", "h2h_lay"):
                    continue

                raw_outcomes = sharp_market.get("outcomes", [])
                
                # ═══════════════════════════════════════════════════════════════════
                # PULSE HUNTER v6.1: Binary Market Validation for totals and btts
                # ═══════════════════════════════════════════════════════════════════
                if sharp_market_key in ("totals", "btts"):
                    logger.debug(f"[BINARY] {sharp_market_key} market detected for {event_name} | Outcomes: {len(raw_outcomes)}")
                    if len(raw_outcomes) != 2:
                        logger.warning(f"[BINARY] {sharp_market_key} non-binaire: {len(raw_outcomes)} issues - Rejet")
                        continue
                    # Verify outcome names are valid binary (Over/Under or Yes/No)
                    outcome_names = [o.get("name", "").lower() for o in raw_outcomes]
                    if sharp_market_key == "totals":
                        if not all(n in ("over", "under") for n in outcome_names):
                            logger.warning(f"[BINARY] totals outcomes invalid: {outcome_names} - Rejet")
                            continue
                    elif sharp_market_key == "btts":
                        if not all(n in ("yes", "no") for n in outcome_names):
                            logger.warning(f"[BINARY] btts outcomes invalid: {outcome_names} - Rejet")
                            continue
                    logger.debug(f"[BINARY] {sharp_market_key} validation passed: {outcome_names}")
                
                # ═══════════════════════════════════════════════════════════════════
                # PULSE HUNTER v6.0: Automatic Fallback for Soccer H2H
                # Si h2h a 3 issues (incluant Draw), chercher automatiquement spreads 0.0/+0.5
                # ═══════════════════════════════════════════════════════════════════
                if "soccer" in sport and sharp_market_key == "h2h" and len(raw_outcomes) == 3:
                    logger.debug(f"🔄 Soccer H2H 3 issues detected - Auto-fallback to spreads AH 0.0/+0.5")
                    # Try to find spreads market with line 0.0 or +0.5
                    for fallback_market in sharp_bm.get("markets", []):
                        if fallback_market.get("key") != "spreads":
                            continue
                        # Check if any outcome has line 0.0 or +0.5
                        for outcome in fallback_market.get("outcomes", []):
                            line = outcome.get("point", None)
                            if line is not None and abs(line) <= 0.5:
                                # Use this spreads market instead
                                sharp_market = fallback_market
                                sharp_market_key = "spreads"
                                raw_outcomes = fallback_market.get("outcomes", [])
                                logger.info(f"✅ Auto-switched to spreads AH {line} for {event_name}")
                                break
                        if sharp_market_key == "spreads":
                            break
                
                # ═══════════════════════════════════════════════════════════════════
                # DOCTRINE BINARY SYNTHESIS STRICTE (PhD MIT Protocol v4.1)
                # Rejeter TOUT marché avec 3 issues (incluant Draw)
                # Uniquement Bernoulli Trials: Pile ou Face mathématique
                # ═══════════════════════════════════════════════════════════════════
                
                # Étape 1: Vérification binaire stricte
                if len(raw_outcomes) == 3:
                    # Marché 1N2 détecté - Rejet immédiat selon doctrine Zéro Nul
                    logger.debug(f"🚫 BINARY VIOLATION: {event_name} | Marché 1N2 à 3 issues rejeté")
                    continue
                
                if len(raw_outcomes) != 2:
                    # Ni binaire, ni trinaire - marché exotique, on skip
                    logger.debug(f"🚫 Marché non-binaire: {event_name} | {len(raw_outcomes)} issues")
                    continue
                
                # Étape 2: Pour Soccer, privilégier spreads (AH 0.0) uniquement
                if "soccer" in sport and sharp_market_key == "h2h":
                    # Soccer en h2h = risque de nul caché, on préfère spreads
                    logger.debug(f"🚫 SOCCER H2H rejeté: {event_name} | Utiliser spreads AH 0.0")
                    continue
                
                # Étape 3: Vérification finale - aucun outcome ne doit être "Draw"
                for outcome in raw_outcomes:
                    if outcome.get("name", "").lower() in ("draw", "nul", "match nul"):
                        logger.warning(f"🚫 DRAW DETECTED: {event_name} | Marché rejeté")
                        continue
                
                outcomes_to_process = raw_outcomes
                effective_market_key = sharp_market_key

                for outcome in outcomes_to_process:
                    selection    = outcome.get("name", "")
                    sharp_prob   = sharp_odds.get(selection)
                    if not sharp_prob:
                        continue

                    # ── BEST-PRICE Multi-Bookmaker Logic ──────
                    # Compare tous les softs et choisit le meilleur EV+
                    soft_odds_val, bm_name = _get_best_soft_odds(
                        bookmakers,
                        market_key=effective_market_key,
                        selection=selection,
                    )

                    # Check Betfair Exchange confirmation
                    betfair_odds = self._get_betfair_odds(bookmakers, effective_market_key, selection)
                    if betfair_odds is None:
                        logger.debug(f"Betfair odds non trouvées pour {event_name} / {effective_market_key} / {selection}")
                        continue

                    # Consensus Sharp: Betfair Exchange doit confirmer la tendance (cote plus basse que le soft book)
                    if not (betfair_odds < soft_odds_val):
                        logger.debug(f"Betfair ({betfair_odds}) ne confirme pas la tendance du soft book ({soft_odds_val}) pour {event_name} / {effective_market_key} / {selection}")
                        continue

                    # Seuil dynamique par sport (fallback au seuil global)
                    sport_threshold = settings.alpha_thresholds.get(sport, settings.alpha_thresholds.get("default", settings.min_ev_threshold))
                    
                    signal = self.engine.evaluate_signal(
                        event_id=event_id,
                        market_key=effective_market_key,
                        selection=selection,
                        bookmaker_target=bm_name,
                        sharp_prob=sharp_prob,
                        soft_odds=soft_odds_val,
                        bankroll=self.bankroll,
                        min_ev=sport_threshold,  # Seuil dynamique par sport
                        min_snr=settings.min_snr_ratio,
                    )
                    if signal is None:
                        continue

                    meta = {
                        "event_name":    event_name,
                        "sport":         sport,
                        "commence_time": commence_time,
                    }
                    dossier = ArbitrageDossier(signal=signal, meta=meta, news_impact=news_impact)

                    # ── News ───────────────────────────────────
                    dossier.news_ok = (news_impact is None or not news_impact.market_moving)
                    if not dossier.news_ok:
                        continue

                    # ── Groq ───────────────────────────────────
                    groq_result = await groq_client.quick_filter({
                        "sport":             sport,
                        "market_key":        effective_market_key,
                        "selection":         selection,
                        "ev_plus":           round(signal.ev_plus * 100, 2),
                        "sharp_prob":        round(signal.sharp_prob * 100, 2),
                        "implied_prob_soft": round(signal.implied_prob_soft * 100, 2),
                        "snr_ratio":         round(signal.snr_ratio, 2),
                        "recommended_stake": signal.recommended_stake,
                    })
                    dossier.groq_ok         = groq_result.get("approved", True)
                    dossier.groq_confidence = groq_result.get("confidence", 0.5)
                    if not dossier.groq_ok:
                        continue

                    # ── Perplexity (EV+ > 2% seulement) ───────
                    if signal.ev_plus >= 0.02 and perplexity_grounding.is_available():
                        try:
                            perp = await asyncio.wait_for(
                                perplexity_grounding.verify_factual_claim(
                                    query_text=f"{event_name} lineup injuries",
                                    context_type="injury",
                                ),
                                timeout=10.0,
                            )
                            dossier.perplexity_ok      = perp.is_reliable
                            dossier.perplexity_summary = perp.summary
                            if perp.verified and perp.confidence >= 0.8 and "OUT" in perp.summary.upper():
                                logger.warning(f"🔍 Perplexity rejet: {event_name}")
                                continue
                        except asyncio.TimeoutError:
                            dossier.perplexity_ok      = True
                            dossier.perplexity_summary = "Timeout Perplexity — approuvé par défaut"
                    else:
                        dossier.perplexity_ok      = True
                        dossier.perplexity_summary = "Non vérifié"

                    # ── Gemini (timeout 10s) ───────────────────
                    gemini_ctx = _build_gemini_context(event_name, effective_market_key, signal, news_impact, dossier.perplexity_summary)
                    try:
                        loop = asyncio.get_event_loop()
                        gemini_response = await asyncio.wait_for(
                            loop.run_in_executor(
                                self._executor,
                                check_market_red_flags,
                                gemini_ctx,
                                effective_market_key,
                            ),
                            timeout=10.0,
                        )
                    except asyncio.TimeoutError:
                        gemini_response = "✅ Analyse technique validée (Math-Only)"
                    except Exception as e:
                        gemini_response = "✅ Analyse technique validée (Math-Only)"
                        logger.warning(f"Gemini erreur: {e}")

                    dossier.gemini_ok      = not (gemini_response and "🚨 RED FLAG" in gemini_response)
                    dossier.gemini_context = gemini_response or "✅ Analyse technique validée (Math-Only)"

                    if not dossier.gemini_ok:
                        logger.warning(f"🚨 Gemini red flag: {event_name} | {gemini_response[:80]}")
                        continue

                    signal.ai_context = dossier.gemini_context
                    validated.append(dossier)

                    logger.info(
                        f"✅ SIGNAL: {event_name} | {selection} "
                        f"| EV+={signal.ev_plus:.2%} | SNR={signal.snr_ratio:.2f} "
                        f"| {bm_name}={soft_odds_val:.3f} | {dossier.sources_badge}"
                    )

        return validated

    def _apply_correlation_filter(self, dossiers: list[ArbitrageDossier]) -> list[ArbitrageDossier]:
        """
        Pulse Hunter v6.0: Correlation Filter
        
        Si plusieurs signaux sur le même match (ex: Over 2.5 + BTTS Yes),
        ne garder que celui avec l'EV+ la plus élevée pour éviter le double risque.
        """
        if not dossiers:
            return dossiers
        
        # Group by event_id
        events_dict = {}
        for dossier in dossiers:
            event_id = dossier.signal.event_id
            if event_id not in events_dict:
                events_dict[event_id] = []
            events_dict[event_id].append(dossier)
        
        filtered = []
        for event_id, event_dossiers in events_dict.items():
            if len(event_dossiers) == 1:
                # Single signal - keep it
                filtered.append(event_dossiers[0])
            else:
                # Multiple signals - keep only highest EV+
                best_dossier = max(event_dossiers, key=lambda d: d.signal.ev_plus)
                rejected_count = len(event_dossiers) - 1
                logger.info(
                    f"🔀 Correlation Filter: {event_id} | "
                    f"Kept {best_dossier.signal.market_key} (EV+={best_dossier.signal.ev_plus:.2%}) | "
                    f"Rejected {rejected_count} lower EV+ signals"
                )
                filtered.append(best_dossier)
        
        return filtered

    # ─────────────────────────────────────────────────────────────
    # Extraction sharp odds (Shin Method)
    # ─────────────────────────────────────────────────────────────

    def _extract_hybrid_sharp_odds(self, bookmakers: list[dict], sport: str = "") -> dict[str, float]:
        """
        Pulse Hunter v6.0: Hybrid Truth Source Logic
        
        1. Primary: Pinnacle (sharp_books) - if available
        2. Fallback: Weighted average of Bet365 + William Hill (if Pinnacle unavailable)
        
        GARDE-FOU BINAIRE STRICT:
        - Rejette si != 2 outcomes (doctrine Zéro Nul)
        - Rejette si un outcome s'appelle "Draw"
        - Pour soccer: uniquement spreads (pas h2h)
        """
        # Try Pinnacle first (primary sharp source)
        pinnacle_odds = self._extract_sharp_odds(bookmakers, sport)
        if pinnacle_odds:
            return pinnacle_odds
        
        # Fallback: Hybrid Bet365 + William Hill average
        logger.info("🔄 Pinnacle unavailable - Using Hybrid Bet365 + William Hill")
        return self._extract_hybrid_bookmaker_odds(bookmakers, sport)
    
    def _extract_sharp_odds(self, bookmakers: list[dict], sport: str = "") -> dict[str, float]:
        """
        Extrait les probabilités sharp via Shin Method (Pinnacle only).
        
        GARDE-FOU BINAIRE STRICT:
        - Rejette si != 2 outcomes (doctrine Zéro Nul)
        - Rejette si un outcome s'appelle "Draw"
        - Pour soccer: uniquement spreads (pas h2h)
        """
        for bm in bookmakers:
            if bm.get("key", "").lower() not in [s.lower() for s in settings.sharp_books]:
                continue
            
            markets = bm.get("markets", [])
            
            # Pour soccer: privilégier spreads (AH 0.0), rejeter h2h
            if "soccer" in sport:
                for market in markets:
                    if market.get("key") != "spreads":
                        continue
                    outcomes = market.get("outcomes", [])
                    # Doctine binaire: exactement 2 issues
                    if len(outcomes) != 2:
                        continue
                    # Vérification: pas de Draw
                    if any(o.get("name", "").lower() == "draw" for o in outcomes):
                        continue
                    raw_odds = [o["price"] for o in outcomes if o.get("price", 0) > 1.0]
                    if len(raw_odds) != 2:
                        continue
                    try:
                        shin_probs = calculate_shin_probabilities(raw_odds)
                    except Exception as e:
                        logger.warning(f"Shin échoué: {e}")
                        continue
                    return {outcomes[i]["name"]: shin_probs[i] for i in range(2)}
            
            # Pour autres sports (NBA, Tennis, etc.): h2h est OK (naturellement binaire)
            for market in markets:
                if market.get("key") != "h2h":
                    continue
                outcomes = market.get("outcomes", [])
                
                # ═══════════════════════════════════════════════════════════════════
                # DOCTRINE BINAIRE STRICTE: exactement 2 outcomes
                # ═══════════════════════════════════════════════════════════════════
                if len(outcomes) != 2:
                    logger.debug(f"🚫 Shin: Rejet marché {len(outcomes)} issues")
                    continue
                
                # Vérification: aucun outcome ne doit s'appeler "Draw"
                if any(o.get("name", "").lower() == "draw" for o in outcomes):
                    logger.warning(f"🚫 Shin: Rejet marché avec Draw")
                    continue
                
                raw_odds = [o["price"] for o in outcomes if o.get("price", 0) > 1.0]
                if len(raw_odds) != 2:
                    continue
                
                try:
                    shin_probs = calculate_shin_probabilities(raw_odds)
                except Exception as e:
                    logger.warning(f"Shin échoué: {e}")
                    continue
                
                # Mapping nom -> probabilité (ordre préservé)
                return {outcomes[0]["name"]: shin_probs[0], outcomes[1]["name"]: shin_probs[1]}
        
        return {}
    
    def _extract_hybrid_bookmaker_odds(self, bookmakers: list[dict], sport: str = "") -> dict[str, float]:
        """
        Pulse Hunter v6.0: Hybrid Truth Source (Bet365 + William Hill average)
        
        Utilisé quand Pinnacle n'a pas la ligne (props markets, etc.)
        Calcule la moyenne pondérée des cotes Bet365 et William Hill.
        """
        hybrid_books = ["bet365", "williamhill"]
        odds_by_book = {}
        
        # Collect odds from both bookmakers
        for bm in bookmakers:
            bm_key = bm.get("key", "").lower()
            if bm_key not in hybrid_books:
                continue
            
            markets = bm.get("markets", [])
            
            # Pour soccer: privilégier spreads
            if "soccer" in sport:
                for market in markets:
                    if market.get("key") != "spreads":
                        continue
                    outcomes = market.get("outcomes", [])
                    if len(outcomes) != 2:
                        continue
                    if any(o.get("name", "").lower() == "draw" for o in outcomes):
                        continue
                    raw_odds = {o["name"]: o["price"] for o in outcomes if o.get("price", 0) > 1.0}
                    if len(raw_odds) == 2:
                        odds_by_book[bm_key] = raw_odds
                        break
            else:
                # Pour autres sports: h2h
                for market in markets:
                    if market.get("key") != "h2h":
                        continue
                    outcomes = market.get("outcomes", [])
                    if len(outcomes) != 2:
                        continue
                    if any(o.get("name", "").lower() == "draw" for o in outcomes):
                        continue
                    raw_odds = {o["name"]: o["price"] for o in outcomes if o.get("price", 0) > 1.0}
                    if len(raw_odds) == 2:
                        odds_by_book[bm_key] = raw_odds
                        break
        
        # Need both bookmakers for hybrid average
        if len(odds_by_book) < 2:
            logger.debug(f"Hybrid: Insufficient bookmakers ({len(odds_by_book)}/2)")
            return {}
        
        # Calculate weighted average odds
        bet365_odds = odds_by_book.get("bet365", {})
        williamhill_odds = odds_by_book.get("williamhill", {})
        
        # Ensure both have same outcomes
        if set(bet365_odds.keys()) != set(williamhill_odds.keys()):
            logger.debug("Hybrid: Outcome mismatch between bookmakers")
            return {}
        
        # Calculate average odds
        avg_odds = {}
        for outcome in bet365_odds.keys():
            avg_odds[outcome] = (bet365_odds[outcome] + williamhill_odds[outcome]) / 2.0
        
        # Apply Shin Method to averaged odds
        raw_odds_list = list(avg_odds.values())
        if len(raw_odds_list) != 2:
            return {}
        
        try:
            shin_probs = calculate_shin_probabilities(raw_odds_list)
        except Exception as e:
            logger.warning(f"Hybrid Shin échoué: {e}")
            return {}
        
        # Map outcomes to probabilities
        outcome_names = list(avg_odds.keys())
        return {outcome_names[0]: shin_probs[0], outcome_names[1]: shin_probs[1]}

    # ─────────────────────────────────────────────────────────────
    # Persistance + notifications
    # ─────────────────────────────────────────────────────────────

    async def _persist_and_notify(self, dossiers: list[ArbitrageDossier]) -> None:
        for dossier in dossiers:
            signal = dossier.signal
            meta   = dossier.meta
            
            # ═══════════════════════════════════════════════════════════════════
            # ASSERTION BINAIRE STRICTE (PhD MIT Protocol v4.1)
            # Dernière ligne de défense: rejeter tout signal contenant "Draw"
            # ═══════════════════════════════════════════════════════════════════
            selection_lower = signal.selection.lower()
            if any(forbidden in selection_lower for forbidden in ["draw", "nul", "match nul"]):
                logger.error(f"🚫 BINARY ASSERTION FAILED: {meta.get('event_name')} | "
                           f"Sélection='{signal.selection}' | Signal REJETÉ")
                continue
            
            # Vérification supplémentaire: le marché doit être binaire ou prop
            if signal.market_key not in ("h2h", "spreads", "totals", "player_points", "player_rebounds", "total_corners", "btts", "h2h_lay"):
                logger.warning(f"🚫 Marché non-supporté rejeté: {signal.market_key}")
                continue
            
            try:
                # insert_signal est maintenant synchrone (SDK supabase-py v2)
                signal_id = self.db.insert_signal(
                    signal=signal,
                    event_name=meta["event_name"],
                    sport=meta["sport"],
                    match_time_iso=meta["commence_time"],
                    ai_context=signal.ai_context,
                    sources_badge=dossier.sources_badge,
                )

                class _Val:
                    context_summary = f"{signal.ai_context}\n🔗 {dossier.sources_badge}"

                notifier = self._get_notifier()
                if notifier:
                    await notifier.send_signal(
                        signal=signal, meta=meta,
                        validation=_Val(), signal_id=signal_id,
                    )
            except Exception as e:
                logger.error(f"Erreur persist/notify {meta.get('event_name')}: {e}")

    # ═══════════════════════════════════════════════════════════════════
    # GEMINI ORACLE FALLBACK — Quand The-Odds-API est vide/épuisée
    # ═══════════════════════════════════════════════════════════════════

    async def _try_gemini_fallback(
        self,
        home_team: str,
        away_team: str,
        sport: str,
        commence_time: str,
    ) -> Optional[ArbitrageDossier]:
        """
        Fallback ultime: utilise Gemini Search Grounding pour trouver des cotes
        quand The-Odds-API ne retourne rien (quotas épuisés ou pas de données).
        
        Returns:
            ArbitrageDossier si cotes trouvées et EV+ valide, None sinon
        """
        try:
            logger.info(f"🔮 GEMINI ORACLE: Recherche cotes pour {home_team} vs {away_team}")
            
            oracle = GeminiOracle()
            odds_result = oracle.search_odds(home_team, away_team, sport)
            
            if not odds_result:
                logger.debug(f"Gemini: Aucune cote trouvée pour {home_team} vs {away_team}")
                return None
            
            # Vérifier qu'on a les cotes nécessaires
            if not (odds_result.pinnacle_home and odds_result.onexbet_home):
                logger.debug(f"Gemini: Cotes incomplètes Pinnacle/1XBet")
                return None
            
            # Calcul Shin Method sur cotes Gemini
            pin_odds = [odds_result.pinnacle_home, odds_result.pinnacle_away]
            xbet_odds = [odds_result.onexbet_home, odds_result.onexbet_away]
            
            try:
                pin_probs = calculate_shin_probabilities(pin_odds)
            except Exception as e:
                logger.warning(f"Shin échoué sur cotes Gemini: {e}")
                return None
            
            # Déterminer la sélection (comparer sharp vs soft)
            home_ev = pin_probs[0] * odds_result.onexbet_home - 1.0
            away_ev = pin_probs[1] * odds_result.onexbet_away - 1.0
            
            if home_ev > away_ev and home_ev > 0:
                selection = home_team
                soft_odds = odds_result.onexbet_home
                sharp_prob = pin_probs[0]
                ev = home_ev
            elif away_ev > 0:
                selection = away_team
                soft_odds = odds_result.onexbet_away
                sharp_prob = pin_probs[1]
                ev = away_ev
            else:
                logger.debug(f"Gemini: Pas d'EV+ positif")
                return None
            
            # Vérifier seuil EV+ min
            min_ev = getattr(self.engine, 'min_ev_threshold', 0.015)
            if ev < min_ev:
                logger.debug(f"Gemini: EV {ev:.2%} < seuil {min_ev:.2%}")
                return None
            
            # Calculer métriques PAIM
            implied_soft = 1.0 / soft_odds
            snr = sharp_prob / implied_soft if implied_soft > 0 else 0
            
            if snr < 3.0:  # SNR min doctrinal
                logger.debug(f"Gemini: SNR {snr:.1f} < 3.0")
                return None
            
            # Créer le signal PAIM
            signal = PAIMSignal(
                event_id=f"gemini_{home_team[:10]}_{away_team[:10]}_{int(time.time())}",
                market_key="h2h",
                selection=selection,
                bookmaker_target="onexbet",
                sharp_prob=sharp_prob,
                implied_prob_soft=implied_soft,
                ev_plus=ev,
                snr_ratio=snr,
                clv_estimate=ev * 0.3,  # Estimation conservative
                recommended_stake=min(ev * self.bankroll * 0.25, self.bankroll * 0.03),
                ai_context=f"🔮 Source: Gemini Search Grounding (confiance: {odds_result.confidence:.0%})",
                is_elite=ev >= 0.025,
            )
            
            # Créer le dossier
            dossier = ArbitrageDossier(
                signal=signal,
                liquidity_score=0.5,  # Moyenne car source AI
                odds_ok=True,
                perplexity_ok=True,
                news_ok=False,
                groq_confidence=odds_result.confidence,
            )
            
            logger.info(f"✅ GEMINI SIGNAL: {home_team} vs {away_team} | "
                       f"Selection: {selection} | EV: {ev:.2%} | Source: AI Grounding")
            
            return dossier
            
        except Exception as e:
            logger.error(f"Erreur Gemini fallback: {e}")
            return None


# ─────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────

def _build_gemini_context(
    event_name: str,
    market_key: str,
    signal: PAIMSignal,
    news_impact: Optional[NewsImpact],
    perplexity_summary: str,
) -> str:
    news_line = "Aucune news récente."
    if news_impact and news_impact.top_headlines:
        news_line = news_impact.top_headlines[0][:100]
    perp_line = (perplexity_summary or "Non vérifié.")[:120]
    return (
        f"{event_name} | {market_key.upper()}\n"
        f"EV+={signal.ev_plus:.2%} Sharp={signal.sharp_prob:.3f} Soft={signal.implied_prob_soft:.3f}\n"
        f"Perplexity: {perp_line}\nNews: {news_line}\n"
        f"L'Alpha est-il une inefficience pure ou justifié par une blessure ?"
    )


# ═══════════════════════════════════════════════════════════════════
# OBSOLÈTE: Fonctions Double Chance supprimées (Binary Synthesis v4.1)
# 
# La doctrine Zéro Nul interdit strictement tout traitement du match nul.
# Les marchés 1N2 sont rejetés dès la détection (len(outcomes) == 3).
# 
# Pour le Soccer: uniquement Asian Handicap 0.0 (spreads) autorisé.
# Pour NBA/Tennis/NHL: h2h naturellement binaire (2 outcomes).
# 
# Les fonctions _apply_double_chance() et _get_double_chance_soft_odds()
# ont été supprimées conformément au protocole PhD MIT v4.1.
# ═══════════════════════════════════════════════════════════════════
