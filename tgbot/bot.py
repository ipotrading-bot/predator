"""
telegram/bot.py — Bot Telegram pour les alertes Tickets d'Élite
Format Système 7/9 — riche et lisible
"""
from __future__ import annotations

import logging
from typing import Optional

from telegram import Bot
from telegram.constants import ParseMode

from config import settings
from core.paim_engine import PAIMSignal

logger = logging.getLogger(__name__)


class TelegramNotifier:
    """Envoi des tickets PAIM via le bot Telegram privé."""

    def __init__(self):
        self.bot = Bot(token=settings.telegram_bot_token)
        self.chat_id = settings.telegram_chat_id

    async def send_signal(
        self,
        signal: PAIMSignal,
        meta: dict,
        validation,
        signal_id: Optional[str] = None,
    ) -> None:
        """Envoie un signal formaté en Markdown Telegram."""
        ev_emoji = "🔥" if signal.ev_plus >= 0.15 else "✅"
        snr_bar = self._snr_bar(signal.snr_ratio)

        text = (
            f"*🦅 PREDATOR PAIM — SIGNAL ÉLITE*\n"
            f"{'─' * 28}\n"
            f"📌 *{meta.get('event_name', 'N/A')}*\n"
            f"🏟️ Sport: `{meta.get('sport', 'N/A')}`\n"
            f"🕐 Match: `{meta.get('commence_time', 'N/A')}`\n"
            f"{'─' * 28}\n"
            f"{ev_emoji} *Sélection:* `{signal.selection.upper()}`\n"
            f"📊 *Bookmaker:* `{signal.bookmaker_target.upper()}`\n"
            f"{'─' * 28}\n"
            f"📈 *EV+:* `{signal.ev_plus:.2%}`\n"
            f"🎯 *Prob. Sharp:* `{signal.sharp_prob:.3f}`\n"
            f"💧 *Prob. Soft:* `{signal.implied_prob_soft:.3f}`\n"
            f"📡 *SNR:* {snr_bar} `{signal.snr_ratio:.2f}`\n"
            f"📉 *CLV Est.:* `{signal.clv_estimate:.2%}`\n"
            f"{'─' * 28}\n"
            f"💶 *Mise conseillée:* `{signal.recommended_stake:.0f}€`\n"
            f"{'─' * 28}\n"
            f"🤖 *IA Contexte:* _{validation.context_summary}_\n"
        )

        if signal_id:
            text += f"\n🔐 ID: `{signal_id[:8]}...`"

        try:
            await self.bot.send_message(
                chat_id=self.chat_id,
                text=text,
                parse_mode=ParseMode.MARKDOWN,
            )
            logger.info(f"📨 Signal Telegram envoyé: {meta.get('event_name')}")
        except Exception as e:
            logger.error(f"Erreur Telegram: {e}")

    async def send_system_ticket(self, signals: list[PAIMSignal], metas: list[dict]) -> None:
        """Envoie le Ticket Système 7/9 groupé."""
        lines = ["*🦅 TICKET SYSTÈME 7/9 — PREDATOR PAIM*\n" + "═" * 28]

        for i, (sig, meta) in enumerate(zip(signals, metas), 1):
            ev_icon = "🔥" if sig.ev_plus >= 0.15 else "✅"
            lines.append(
                f"{ev_icon} *#{i}* {meta.get('event_name', '?')} "
                f"— `{sig.selection.upper()}` "
                f"| EV `{sig.ev_plus:.1%}`"
            )

        n = len(signals)
        lines.append(
            f"\n{'─' * 28}\n"
            f"📋 *Format:* Système {settings.system_min_wins}/{n}\n"
            f"🎯 *Combinaisons:* `{self._combinations(n, settings.system_min_wins)}`\n"
            f"⚡ *Objectif:* Profit dès {settings.system_min_wins} bons résultats"
        )

        try:
            await self.bot.send_message(
                chat_id=self.chat_id,
                text="\n".join(lines),
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception as e:
            logger.error(f"Erreur Telegram ticket système: {e}")

    async def send_killswitch_alert(self, drawdown: float, balance: float) -> None:
        """Alerte critique Kill-Switch."""
        text = (
            f"🛑 *KILL-SWITCH DÉCLENCHÉ*\n"
            f"{'─' * 28}\n"
            f"📉 Drawdown: `{drawdown:.1%}`\n"
            f"💰 Balance: `{balance:.0f}€`\n"
            f"⚠️ _Système arrêté automatiquement. Intervention manuelle requise._"
        )
        try:
            await self.bot.send_message(
                chat_id=self.chat_id, text=text, parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e:
            logger.error(f"Erreur alerte kill-switch: {e}")

    # ── Helpers ───────────────────────────────────────────────

    @staticmethod
    def _snr_bar(snr: float, max_snr: float = 5.0) -> str:
        filled = min(int(snr / max_snr * 5), 5)
        return "█" * filled + "░" * (5 - filled)

    @staticmethod
    def _combinations(n: int, k: int) -> int:
        from math import comb
        return sum(comb(n, i) for i in range(k, n + 1))
