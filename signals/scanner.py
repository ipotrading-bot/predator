"""
signals/scanner.py — MarketScanner v2.0
Point d'entrée du pipeline de détection de signaux PAIM.

Pipeline :
  OddsFetcher → Shin Method → PAIMEngine → GroqFilter → GeminiContext → Supabase → Telegram
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from config import settings
from core.math_engine import calculate_shin_probabilities
from core.paim_engine import PAIMEngine, PAIMSignal, ScanResult, ValidationResult
from data.odds_fetcher import OddsFetcher
from data.supabase_client import SupabaseClient

logger = logging.getLogger(__name__)


class MarketScanner:
    """
    Orchestrateur principal du scan de marchés sportifs.
    Exécuté 3x/jour (Asie 00:00 / Europe 08:00 / USA 16:00 UTC).
    """

    def __init__(self, bankroll: float = 10_000.0):
        self.bankroll = bankroll
        self.engine = PAIMEngine(
            kelly_fraction=settings.kelly_fraction,
            max_stake_pct=settings.max_single_stake_pct,
        )
        self.db = SupabaseClient()

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

    # ── Scan principal ────────────────────────────────────────

    async def run_scan(self) -> ScanResult:
        """Lance un cycle complet de scan multi-sports."""
        start = time.monotonic()
        result = ScanResult()

        logger.info("🔍 Démarrage scan MarketScanner...")

        try:
            async with OddsFetcher() as fetcher:
                events = await fetcher.fetch_all_sports_odds()

            result.events_analyzed = len(events)
            logger.info(f"📡 {len(events)} événements récupérés")

            signals_batch: list[tuple[PAIMSignal, dict]] = []

            for event in events:
                try:
                    found = await self._process_event(event)
                    signals_batch.extend(found)
                    result.signals_found += len(found)
                except Exception as e:
                    logger.debug(f"Erreur event {event.get('id', '?')}: {e}")

            # Traitement asynchrone des signaux en batch
            validated = await self._validate_and_persist(signals_batch)
            result.signals_validated = validated
            result.signals_rejected = result.signals_found - validated

        except Exception as e:
            logger.error(f"Erreur critique scan: {e}", exc_info=True)
            raise

        result.duration_seconds = time.monotonic() - start
        logger.info(
            f"✅ Scan terminé | {result.events_analyzed} events | "
            f"{result.signals_validated} signaux validés | "
            f"{result.duration_seconds:.1f}s"
        )
        return result

    # ── Traitement d'un événement ─────────────────────────────

    async def _process_event(
        self, event: dict
    ) -> list[tuple[PAIMSignal, dict]]:
        """
        Analyse un événement : compare Pinnacle (sharp) vs Soft Books.
        Retourne les signaux EV+ détectés.
        """
        from data.odds_fetcher import OddsFetcher

        signals = []
        event_id = event.get("id", "")
        home = event.get("home_team", "?")
        away = event.get("away_team", "?")
        event_name = f"{home} vs {away}"
        sport = event.get("sport_title", "")
        commence_time_str = event.get("commence_time", "")
        
        # 24h filter
        if commence_time_str:
            try:
                # ISO format usually ends in Z for UTC
                commence_time = datetime.fromisoformat(commence_time_str.replace("Z", "+00:00"))
                now = datetime.now(timezone.utc)
                if commence_time > now + timedelta(hours=24):
                    return []
            except ValueError:
                logger.warning(f"Format de date invalide pour event {event_id}: {commence_time_str}")

        bookmakers = event.get("bookmakers", [])
        pinnacle = self.find_book(bookmakers, ["pinnacle", "betfair"])
        if not pinnacle:
            return []

        for market in pinnacle.get("markets", []):
            mkey = market.get("key", "")
            # Doctrine : binaire uniquement (h2h) ou spreads
            if mkey not in ("h2h", "spreads"):
                continue

            pin_outcomes = market.get("outcomes", [])
            pin_odds = [o["price"] for o in pin_outcomes]
            if len(pin_odds) < 2:
                continue

            # Shin Method : probabilités sans marge
            try:
                sharp_probs = calculate_shin_probabilities(pin_odds)
            except Exception:
                continue

            # Comparer avec chaque soft book
            for soft_bm in bookmakers:
                if soft_bm["key"] not in settings.soft_books:
                    continue

                soft_market = next(
                    (m for m in soft_bm.get("markets", []) if m["key"] == mkey),
                    None,
                )
                if not soft_market:
                    continue

                soft_outcomes = soft_market.get("outcomes", [])
                for i, soft_out in enumerate(soft_outcomes):
                    if i >= len(sharp_probs):
                        break

                    sharp_p = sharp_probs[i]
                    soft_odds_val = soft_out.get("price", 0)
                    selection_name = soft_out.get("name", f"outcome_{i}")

                    signal = self.engine.evaluate_signal(
                        event_id=event_id,
                        market_key=mkey,
                        selection=selection_name,
                        bookmaker_target=soft_bm["key"],
                        sharp_prob=sharp_p,
                        soft_odds=soft_odds_val,
                        bankroll=self.bankroll,
                        min_ev=settings.min_ev_threshold,
                        min_snr=settings.min_snr_ratio,
                    )

                    if signal:
                        meta = {
                            "event_name": event_name,
                            "sport": sport,
                            "commence_time": commence_time,
                        }
                        signals.append((signal, meta))

        return signals

    # ── Validation & Persistance ──────────────────────────────

    async def _validate_and_persist(
        self, signals_batch: list[tuple[PAIMSignal, dict]]
    ) -> int:
        """Filtre IA, persiste en DB, envoie notifications. Retourne le nb validés."""
        if not signals_batch:
            return 0

        validated = 0
        groq = self._get_groq()
        notifier = self._get_notifier()

        for signal, meta in signals_batch:
            try:
                # Filtre Groq (rapide)
                ai_approved = True
                ai_context = ""

                if groq and groq.enabled:
                    groq_result = await groq.quick_filter({
                        "ev_plus": signal.ev_plus,
                        "sharp_prob": signal.sharp_prob,
                        "implied_prob": signal.implied_prob_soft,
                        "sport": meta.get("sport", ""),
                        "market": signal.market_key,
                    })
                    ai_approved = groq_result.get("approved", True)
                    ai_context = groq_result.get("reason", "")

                if not ai_approved:
                    logger.info(f"⛔ Signal rejeté par Groq: {meta.get('event_name')}")
                    continue

                # Contexte Gemini (red flags)
                try:
                    from core.validator import check_market_red_flags
                    flag = check_market_red_flags(
                        meta.get("event_name", ""),
                        f"{signal.market_key} {signal.selection}"
                    )
                    if "RED FLAG" in str(flag).upper():
                        logger.warning(f"🚨 Red flag détecté: {flag}")
                        ai_context = flag
                        # On garde le signal mais on note le flag
                except Exception:
                    pass

                validation = ValidationResult(
                    approved=True,
                    context_summary=ai_context or "✅ Signal PAIM validé",
                )

                # Persist Supabase
                signal_id = await self.db.insert_signal(
                    signal=signal,
                    event_name=meta.get("event_name", ""),
                    sport=meta.get("sport", ""),
                    match_time_iso=meta.get("commence_time", ""),
                    ai_context=ai_context,
                )

                # Notification Telegram
                if notifier and notifier.enabled:
                    await notifier.send_signal(signal, meta, validation, signal_id)

                validated += 1

            except Exception as e:
                logger.error(f"Erreur validation signal: {e}", exc_info=True)

        # Ticket système 7/9 si assez de signaux
        if validated >= settings.system_size and notifier and notifier.enabled:
            elite_signals = [s for s, _ in signals_batch[:settings.system_size] if s.is_elite]
            if elite_signals:
                metas = [m for _, m in signals_batch[:len(elite_signals)]]
                await notifier.send_system_ticket(elite_signals, metas)

        return validated
