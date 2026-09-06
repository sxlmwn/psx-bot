"""Alerts, Telegram, and Discord notification package for VeteranDesk."""

from veterandesk.alerts.discord import (
    DiscordOutboundMessage,
    DiscordService,
    discord_service,
    get_discord_delivery_stats,
)
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
    "DiscordOutboundMessage",
    "DiscordService",
    "MessageType",
    "OutboundMessage",
    "TelegramService",
    "discord_service",
    "get_delivery_stats",
    "get_discord_delivery_stats",
    "telegram_service",
]

