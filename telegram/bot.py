"""
telegram/bot.py — Notificateur Telegram (stub — à compléter)
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class TelegramNotifier:
    async def send_signal(self, signal, meta: dict, validation, signal_id) -> None:
        logger.info(f"📨 Telegram stub | {meta.get('event_name')} | {signal.label}")
