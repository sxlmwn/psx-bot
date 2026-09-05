"""Alerts and Telegram package for VeteranDesk."""

from veterandesk.alerts.telegram import (
    MessageType,
    OutboundMessage,
    TelegramService,
)

__all__ = ["MessageType", "OutboundMessage", "TelegramService"]
