"""
core/notifications.py — Notifications Telegram centralisées
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def send_telegram_ticket(signals: list, token: str, chat_id: str) -> None:
    """Envoie le ticket système 7/9 via Telegram."""
    if not token or not chat_id:
        logger.warning("⚠️ TELEGRAM_BOT_TOKEN ou TELEGRAM_CHAT_ID manquant — envoi ignoré.")
        return

    from tgbot.bot import TelegramNotifier
    notifier = TelegramNotifier()
    metas = [{"event_name": getattr(s, "event_id", "?"), "sport": "", "commence_time": ""} for s in signals]
    await notifier.send_system_ticket(signals, metas)
    logger.info(f"📨 Ticket système envoyé — {len(signals)} signaux")
