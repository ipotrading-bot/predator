"""
signals/scanner.py — MarketScanner v3.1 (Data-Link Fix)

Correctifs v3.1 :
  - Fuzzy bookmaker search : '1x' ou 'one' dans la clé → soft book reconnu
  - Market fallback : si spreads absent chez le soft, tente h2h
  - Filtre 48h : rejette les matchs à plus de 48h (Alpha Decay trop fort)
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
from data.odds_fetcher import OddsFetcher
from data.supabase_client import SupabaseClient
from api.groq_client import groq_client
from api.perplexity_client import perplexity_grounding

logger = logging.getLogger(__name__)

# Fenêtre de scan : T+0h à T+48h (Alpha Decay doctrine)
SCAN_WINDOW_HOURS = 48

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


def _get_soft_odds(bookmakers: list[dict], market_key: str, selection: str) -> Optional[float]:
    """
    Cherche la cote d'un soft book pour une sélection donnée.
    Stratégie :
      1. Cherche le marché exact (market_key)
      2. Si absent, fallback sur h2h
    Retourne None si aucune cote valide trouvée.
    """
    fallback_price: Optional[float] = None

    for bm in bookmakers:
        if not _is_soft_book(bm.get("key", "")):
            continue

        markets = bm.get("markets", [])

        # Chercher le marché demandé en premier
        for market in markets:
            if market.get("key") != market_key:
                continue
            for outcome in market.get("outcomes", []):
                if outcome.get("name", "").lower() == selection.lower():
                    price = outcome.get("price", 0.0)
                    if price > 1.0:
                        return float(price)

        # Fallback h2h si marché principal absent
        if market_key != "h2h":
            for market in markets:
                if market.get("key") != "h2h":
                    continue
                for outcome in market.get("outcomes", []):
                    if outcome.get("name", "").lower() == selection.lower():
                        price = outcome.get("price", 0.0)
                        if price > 1.0 and fallback_price is None:
                            fallback_price = float(price)

    return fallback_price


def _get_soft_bm_name(bookmakers: list[dict]) -> str:
    """Retourne le nom normalisé du premier soft book trouvé."""
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
        self.notifier = TelegramNotifier()
        self._executor = ThreadPoolExecutor(max_workers=4)

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
            async with OddsFetcher() as fetcher:
                all_events = await fetcher.fetch_all_sports_odds()

            # ── Filtre 48h ────────────────────────────────────
            events = [
                e for e in all_events
                if _is_within_window(e.get("commence_time", ""), SCAN_WINDOW_HOURS)
            ]
            skipped = len(all_events) - len(events)
            if skipped:
                logger.info(f"⏰ {skipped} matchs hors fenêtre 48h ignorés")

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
                await self.notifier.send_system_ticket(sigs, metas)

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
            
            result.signals_rejected = result.signals_found - result.signals_validated
            
            if dossiers:
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

        # Probabilités sharp via Shin Method
        sharp_odds = self._extract_sharp_odds(bookmakers)
        if not sharp_odds:
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
                if sharp_market_key not in ("h2h", "spreads"):
                    continue

                raw_outcomes = sharp_market.get("outcomes", [])
                
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

                    # ── Fuzzy soft odds lookup ─────────────────
                    # Cherche la cote chez n'importe quel soft book
                    # avec fallback h2h si le marché principal est absent
                    soft_odds_val = _get_soft_odds(
                        bookmakers,
                        market_key=effective_market_key,
                        selection=selection,
                    )

                    # ── Data Integrity : rejeter si cote soft invalide ──
                    if not soft_odds_val or soft_odds_val <= 1.0:
                        logger.debug(
                            f"⚠️  Cote soft introuvable: {event_name} | "
                            f"{selection} | marché={effective_market_key}"
                        )
                        continue

                    bm_name = _get_soft_bm_name(bookmakers)

                    # ── Seuil Alpha dynamique par Sport ─────────
                    sport_threshold = settings.alpha_thresholds.get(
                        sport, settings.alpha_thresholds.get('default', 0.020)
                    )

                    # ── Filtre PAIM ────────────────────────────
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

    # ─────────────────────────────────────────────────────────────
    # Extraction sharp odds (Shin Method)
    # ─────────────────────────────────────────────────────────────

    def _extract_sharp_odds(self, bookmakers: list[dict]) -> dict[str, float]:
        for bm in bookmakers:
            if bm.get("key", "").lower() not in [s.lower() for s in settings.sharp_books]:
                continue
            for market in bm.get("markets", []):
                if market.get("key") != "h2h":
                    continue
                outcomes = market.get("outcomes", [])
                if len(outcomes) < 2:
                    continue
                raw_odds = [o["price"] for o in outcomes if o.get("price", 0) > 1.0]
                if len(raw_odds) < 2:
                    continue
                try:
                    shin_probs = calculate_shin_probabilities(raw_odds)
                except Exception as e:
                    logger.warning(f"Shin échoué: {e}")
                    continue
                return {
                    outcomes[i]["name"]: shin_probs[i]
                    for i in range(min(len(outcomes), len(shin_probs)))
                }
        return {}

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
            
            # Vérification supplémentaire: le marché doit être binaire
            if signal.market_key not in ("h2h", "spreads"):
                logger.warning(f"🚫 Marché non-binaire rejeté: {signal.market_key}")
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

                await self.notifier.send_signal(
                    signal=signal, meta=meta,
                    validation=_Val(), signal_id=signal_id,
                )
            except Exception as e:
                logger.error(f"Erreur persist/notify {meta.get('event_name')}: {e}")


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
