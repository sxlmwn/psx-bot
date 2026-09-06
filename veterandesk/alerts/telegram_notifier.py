"""
Telegram Notifier Re-export Module.
Allows importing TelegramService and telegram_service directly from telegram_notifier.
"""

from veterandesk.alerts.telegram import (
    DeliveryStatus,
    MessageType,
    OutboundMessage,
    TelegramService,
    get_delivery_stats,
    telegram_service,
)

__all__ = [
    "DeliveryStatus",
    "MessageType",
    "OutboundMessage",
    "TelegramService",
    "get_delivery_stats",
    "telegram_service",
]
