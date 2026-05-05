"""
signals/scanner.py — Orchestrateur du scan de marchés
Coordonne: OddsFetcher → PAIM Engine → GeminiValidator → Obfuscator → Telegram
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from config import settings
from core.paim_engine import PAIMEngine, PAIMSignal, MarketOdds
from core.risk_manager import RiskManager, SystemState
from core.signal_validator import GeminiValidator
from data.odds_fetcher import OddsFetcher
from data.supabase_client import SupabaseClient
from signals.obfuscator import Obfuscator
from tgbot.bot import TelegramNotifier

logger = logging.getLogger(__name__)


@dataclass
class ScanResult:
    timestamp: float
    events_analyzed: int
    signals_found: int
    signals_validated: int
    signals_rejected: int
    signals: list[PAIMSignal] = field(default_factory=list)
    duration_seconds: float = 0.0


class MarketScanner:
    """
    Pipeline complet d'un scan PAIM.
    Analyse 100% du catalogue cible, filtre et transmet les signaux validés.
    """

    def __init__(self, bankroll: float):
        self.engine = PAIMEngine(
            kelly_fraction=settings.kelly_fraction,
            min_ev=settings.min_ev_threshold,
            min_snr=settings.min_snr_ratio,
            stake_base=settings.stake_rounding_base,
        )
        self.risk = RiskManager(
            starting_bankroll=bankroll,
            max_drawdown_pct=settings.max_drawdown_pct,
        )
        self.validator = GeminiValidator()
        self.db = SupabaseClient()
        self.notifier = TelegramNotifier()
        self.obfuscator = Obfuscator(
            jitter_min=settings.jitter_min_seconds,
            jitter_max=settings.jitter_max_seconds,
            stake_base=settings.stake_rounding_base,
        )

    async def run_scan(self) -> ScanResult:
        """Exécute un scan complet."""
        if self.risk.state == SystemState.KILLED:
            logger.critical("🛑 Kill-Switch actif — scan annulé.")
            return ScanResult(timestamp=time.time(), events_analyzed=0,
                              signals_found=0, signals_validated=0, signals_rejected=0)

        start = time.monotonic()
        result = ScanResult(timestamp=time.time(), events_analyzed=0,
                            signals_found=0, signals_validated=0, signals_rejected=0)

        async with OddsFetcher() as fetcher:
            events = await fetcher.fetch_all_sports_odds()
            result.events_analyzed = len(events)
            logger.info(f"📊 {len(events)} événements analysés...")

            validated_signals: list[tuple] = []

            for event in events:
                signals = await self._process_event(event, fetcher)
                for sig, meta in signals:
                    result.signals_found += 1

                    val = await self.validator.validate(
                        signal=sig,
                        event_name=meta.get("event_name", ""),
                        sport=meta.get("sport", ""),
                        match_time_iso=meta.get("commence_time", ""),
                        recent_news=None,
                    )

                    if not val.is_approved or val.trap_detected:
                        result.signals_rejected += 1
                        logger.info(f"❌ Signal rejeté: {val.context_summary}")
                        continue

                    ok, reason = self.risk.validate_stake(sig.recommended_stake)
                    if not ok:
                        result.signals_rejected += 1
                        logger.warning(f"⚠️ Stake rejeté: {reason}")
                        continue

                    sig.ai_context = val.context_summary
                    result.signals_validated += 1
                    validated_signals.append((sig, meta, val))

            top_signals = self._select_system_candidates(validated_signals)
            result.signals = [s for s, _, _ in top_signals]

            for sig, meta, val in top_signals:
                await self.obfuscator.jitter_delay()
                signal_id = await self.db.insert_signal(
                    signal=sig,
                    event_name=meta.get("event_name", ""),
                    sport=meta.get("sport", ""),
                    match_time_iso=meta.get("commence_time", ""),
                    ai_context=val.context_summary,
                )
                await self.notifier.send_signal(sig, meta, val, signal_id)

        result.duration_seconds = time.monotonic() - start
        logger.info(
            f"✅ Scan terminé en {result.duration_seconds:.1f}s | "
            f"{result.signals_validated} signaux validés"
        )
        return result

    # ── Helpers ───────────────────────────────────────────────

    async def _process_event(
        self, event: dict, fetcher: OddsFetcher
    ) -> list[tuple[PAIMSignal, dict]]:
        """Analyse un événement contre tous les Softs."""
        signals = []
        meta = {
            "event_name": f"{event.get('home_team')} vs {event.get('away_team')}",
            "sport": event.get("sport_key", ""),
            "commence_time": event.get("commence_time", ""),
        }

        for market_key in ["h2h", "spreads"]:
            for sharp_book in settings.sharp_books:
                sharp_odds = OddsFetcher.parse_to_market_odds(event, sharp_book, market_key)
                if not sharp_odds:
                    continue

                for soft_book in settings.soft_books:
                    soft_odds = OddsFetcher.parse_to_market_odds(event, soft_book, market_key)
                    if not soft_odds:
                        continue

                    signal = self.engine.process(
                        sharp=sharp_odds,
                        soft=soft_odds,
                        bankroll=self.risk.current_bankroll,
                        n_confirming_books=2,
                        sport_key=meta.get("sport", ""),
                    )
                    if signal:
                        signals.append((signal, meta))

        return signals

    def _select_system_candidates(self, signals: list[tuple]) -> list[tuple]:
        """
        Sélectionne les 9 meilleurs signaux pour le Système 7/9.
        Tri par EV+ décroissant.
        """
        sorted_sigs = sorted(signals, key=lambda x: x[0].ev_plus, reverse=True)
        return sorted_sigs[: settings.system_size]
