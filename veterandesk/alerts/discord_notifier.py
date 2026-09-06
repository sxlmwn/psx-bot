"""
Discord Notifier Re-export Module.
Allows importing DiscordService, discord_service, and get_discord_delivery_stats directly from discord_notifier.
"""

from veterandesk.alerts.discord import (
    DiscordOutboundMessage,
    DiscordService,
    discord_service,
    get_discord_delivery_stats,
)
from veterandesk.alerts.telegram import DeliveryStatus, MessageType

__all__ = [
    "DeliveryStatus",
    "DiscordOutboundMessage",
    "DiscordService",
    "MessageType",
    "discord_service",
    "get_discord_delivery_stats",
]
