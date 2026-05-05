# tgbot/bot.py
"""
Pont de compatibilité : scanner.py importe depuis tgbot.bot
mais TelegramNotifier est dans core/notifications.py
"""
from core.notifications import TelegramNotifier

__all__ = ["TelegramNotifier"]