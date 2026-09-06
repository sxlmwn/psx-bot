#!/usr/bin/env python3
"""
===============================================================================
VeteranDesk PSX Trading Agent — Standalone Telegram Delivery Verification
===============================================================================

This standalone script independently verifies whether Telegram alert delivery
is genuinely reaching the configured chat or running in offline/mock mode.

Usage:
    # Run with credentials from .env / settings:
    python scripts/test_telegram_send.py

    # Run with custom one-off credentials:
    python scripts/test_telegram_send.py --token <BOT_TOKEN> --chat-id <CHAT_ID>

    # Force a specific message text:
    python scripts/test_telegram_send.py --text "Custom test message"

===============================================================================
OFFLINE / MOCK-DELIVERED FALLBACK SPECIFICATION & PRODUCTION SAFETY
===============================================================================
Under what exact conditions does VeteranDesk fall back to "offline/mock" mode?
Mock/offline mode triggers IF AND ONLY IF at least one of these 3 conditions is met:
  1. `TELEGRAM_ENABLED` is explicitly set to `False` in `.env` or settings.
  2. `TELEGRAM_BOT_TOKEN` is unset, empty (""), or whitespace.
  3. `TELEGRAM_CHAT_ID` is unset, empty (""), or whitespace.

Can mock mode silently happen in production?
ABSOLUTELY NOT. The safety guarantees are:
  1. Once valid credentials are provided and `TELEGRAM_ENABLED=true` (the default),
     `TelegramService.enabled` evaluates strictly to `True`.
  2. `_send_with_retry_sync()` ALWAYS initiates real HTTPS POST requests to
     `https://api.telegram.org/bot<TOKEN>/sendMessage` using `httpx`.
  3. If Telegram API fails (e.g. HTTP 400, 401, 403, 429, 500) or times out:
     IT NEVER FALLS BACK TO MOCK MODE.
  4. Instead, it executes up to 3 retry attempts with exponential backoff
     (0.5s, 1.0s, 2.0s), logs warnings per attempt, and if all attempts fail,
     permanently marks the message as `DeliveryStatus.FAILED` (`attempts=3`),
     stores the exact HTTP status and error body in `telegram_delivery_log`,
     pushes the message to `failed_dead_letter`, and fires an alert.
===============================================================================
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Ensure project root is on sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from veterandesk.config import PKT_TZ, settings
from veterandesk.alerts.telegram_notifier import (
    DeliveryStatus,
    MessageType,
    OutboundMessage,
    TelegramService,
    telegram_service,
)


def mask_secret(secret: Optional[str], show_last: int = 4) -> str:
    """Mask sensitive tokens so they are never printed in logs or terminal."""
    if not secret:
        return "[NOT SET / EMPTY]"
    s = str(secret).strip()
    if len(s) <= show_last:
        return "*" * len(s)
    return "*" * (len(s) - show_last) + s[-show_last:]


def print_specification() -> None:
    """Print the mock fallback conditions and production guarantees."""
    print("=" * 80)
    print("VETERANDESK TELEGRAM NOTIFICATION SYSTEM — SPECIFICATION")
    print("=" * 80)
    print("Conditions that trigger Offline / Mock-Delivered Mode:")
    print("  [1] TELEGRAM_ENABLED is set to False in .env / settings")
    print("  [2] TELEGRAM_BOT_TOKEN is unset, empty, or whitespace")
    print("  [3] TELEGRAM_CHAT_ID is unset, empty, or whitespace")
    print()
    print("Production Guarantee:")
    print("  • When credentials are configured, the service strictly calls")
    print("    https://api.telegram.org/bot<TOKEN>/sendMessage via HTTPS.")
    print("  • Under network failure, HTTP 4xx/5xx errors, or timeout, the engine")
    print("    NEVER falls back to mock mode. It retries 3x with exponential")
    print("    backoff (0.5s, 1.0s, 2.0s), and if unresolved, marks status='failed'")
    print("    in database table `telegram_delivery_log` with the exact error message.")
    print("=" * 80)


def run_telegram_test(
    cli_token: Optional[str] = None,
    cli_chat_id: Optional[str] = None,
    custom_text: Optional[str] = None,
) -> int:
    """
    Run standalone Telegram verification test.
    Returns 0 on success or documented mock-run, 1 on real API failure.
    """
    print_specification()
    print()

    # Determine credential source
    env_token = settings.telegram_bot_token or os.environ.get("TELEGRAM_BOT_TOKEN")
    env_chat_id = settings.telegram_chat_id or os.environ.get("TELEGRAM_CHAT_ID")
    env_enabled = settings.telegram_enabled

    token = (cli_token or env_token or "").strip()
    chat_id = (cli_chat_id or env_chat_id or "").strip()

    token_present = bool(token)
    chat_id_present = bool(chat_id)

    print("CONFIGURED CREDENTIALS INSPECTION:")
    print(f"  • Config File / Source   : {settings.model_config.get('env_file', '.env')}")
    print(f"  • TELEGRAM_ENABLED Flag  : {env_enabled}")
    print(f"  • TELEGRAM_BOT_TOKEN     : {'SET' if token_present else 'UNSET'} | Length: {len(token)} chars | Masked: {mask_secret(token)}")
    print(f"  • TELEGRAM_CHAT_ID       : {'SET' if chat_id_present else 'UNSET'} | Length: {len(chat_id)} chars | Masked: {mask_secret(chat_id)}")
    print()

    # Initialize notifier service
    # If custom CLI flags provided, instantiate TelegramService with them;
    # otherwise use the app-wide singleton instance directly.
    if cli_token or cli_chat_id:
        print("[*] Instantiating TelegramService with provided CLI credentials...")
        service = TelegramService(bot_token=token, chat_id=chat_id, enabled=True)
    else:
        print("[*] Utilizing production singleton `telegram_service` from `telegram_notifier`...")
        service = telegram_service

    is_configured_for_real = bool(service.enabled and service.bot_token and service.chat_id)
    print(f"  • Active Service Status  : {'REAL HTTP MODE (Network enabled)' if is_configured_for_real else 'MOCK / OFFLINE MODE (No network)'}")
    print()

    # Formulate test payload
    now_utc = datetime.now(timezone.utc)
    now_pkt = now_utc.astimezone(PKT_TZ)
    timestamp_str = f"{now_utc.strftime('%Y-%m-%d %H:%M:%S UTC')} / {now_pkt.strftime('%H:%M:%S PKT')}"
    run_id = str(uuid.uuid4())[:8]

    if custom_text:
        test_message = custom_text
    else:
        test_message = (
            f"✅ *VeteranDesk Telegram test* — if you see this, delivery is working.\n\n"
            f"• *Timestamp*: `{timestamp_str}`\n"
            f"• *Run ID*: `{run_id}`\n"
            f"• *Environment*: `{settings.environment}`\n"
            f"• *Delivery Route*: `Direct Telegram Bot API via httpx`\n"
            f"• *Status*: Operational"
        )

    print("OUTBOUND MESSAGE PREVIEW:")
    print("-" * 60)
    print(test_message)
    print("-" * 60)
    print()

    # Send message using the exact production function used throughout VeteranDesk
    print("[*] Invoking `telegram_service.send_message()`...")
    hist_before = len(service.delivered_history)
    failed_before = len(service.failed_dead_letter)
    skipped_before = len(service.skipped_history)

    send_success = service.send_message(
        text=test_message,
        msg_type=MessageType.ALERT,
        reference_id=f"TEST_SEND_{run_id}",
        event_type="STANDALONE_DELIVERY_TEST",
    )

    # Locate the created OutboundMessage object
    outbound: Optional[OutboundMessage] = None
    if len(service.skipped_history) > skipped_before:
        outbound = service.skipped_history[-1]
    elif len(service.delivered_history) > hist_before:
        outbound = service.delivered_history[-1]
    elif len(service.failed_dead_letter) > failed_before:
        outbound = service.failed_dead_letter[-1]
    elif service.outbound_queue:
        outbound = service.outbound_queue[-1]

    # Evaluate Result
    print()
    print("=" * 80)

    if not is_configured_for_real or (outbound and (outbound.status == DeliveryStatus.SKIPPED or outbound.attempts == 0)):
        # MOCK / OFFLINE MODE
        print("⚠️  DELIVERY RESULT: MOCK / OFFLINE MODE (DELIVERY SKIPPED)")
        print("=" * 80)
        print("[!] No network transmission to api.telegram.org occurred.")
        print("[!] Reason: Missing or empty TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID in environment.")
        print(f"[!] Result Flag Returned  : {send_success} (False = not sent over network)")
        print(f"[!] Attempt Count         : {outbound.attempts if outbound else 0} (0 attempts confirms NO network call was made)")
        print(f"[!] Message Delivery ID   : {outbound.id if outbound else 'N/A'}")
        print(f"[!] Internal DB Status    : {outbound.status.value if outbound else 'unknown'} (correctly recorded as 'skipped', NOT 'sent')")
        print(f"[!] Log Reason Stored     : {outbound.last_error if outbound else 'None'}")
        print()
        print("NEXT STEP TO ENABLE REAL TELEGRAM DELIVERY:")
        print("  1. Add your real bot token to .env: TELEGRAM_BOT_TOKEN=123456:ABC-DEF...")
        print("  2. Add your real chat ID to .env:   TELEGRAM_CHAT_ID=987654321")
        print("  3. Ensure TELEGRAM_ENABLED=true in .env")
        print("  4. Re-run: python scripts/test_telegram_send.py")
        print("=" * 80)
        return 0

    elif send_success and outbound and outbound.status == DeliveryStatus.SENT:
        # REAL DELIVERY SUCCESS
        print("✅  DELIVERY RESULT: REAL NETWORK DELIVERY SUCCESSFUL!")
        print("=" * 80)
        print("[+] Live HTTPS request was transmitted to Telegram Bot API.")
        print("[+] Telegram API accepted the payload and returned HTTP 200 OK.")
        print(f"[+] Result Flag Returned  : {send_success}")
        print(f"[+] Attempts Required     : {outbound.attempts}")
        print(f"[+] Message Delivery ID   : {outbound.id}")
        print(f"[+] Target Chat ID        : {mask_secret(chat_id)}")
        print(f"[+] Delivered At          : {outbound.sent_at.isoformat() if outbound.sent_at else 'N/A'}")
        print(f"[+] Database Log Table    : `telegram_delivery_log` (persisted)")
        print("=" * 80)
        return 0

    else:
        # REAL DELIVERY FAILED
        print("❌  DELIVERY RESULT: REAL DELIVERY ATTEMPT FAILED!")
        print("=" * 80)
        print("[!] Live HTTPS request was attempted against Telegram Bot API but failed.")
        print(f"[!] Result Flag Returned  : {send_success}")
        print(f"[!] Total Attempts Made   : {outbound.attempts if outbound else 'N/A'}")
        print(f"[!] Final Message Status  : {outbound.status.value if outbound else 'unknown'}")
        print(f"[!] Error Timestamp       : {outbound.failed_at.isoformat() if outbound and outbound.failed_at else 'N/A'}")
        print()
        print("ACTUAL ERROR RETURNED BY TELEGRAM / NETWORK:")
        print("-" * 60)
        print(outbound.last_error if (outbound and outbound.last_error) else "Unknown delivery error.")
        print("-" * 60)
        print()
        print("TROUBLESHOOTING:")
        print("  • If HTTP 401 Unauthorized: Check that TELEGRAM_BOT_TOKEN is correct and valid.")
        print("  • If HTTP 400 Bad Request: Check that TELEGRAM_CHAT_ID exists and user has started the bot (/start).")
        print("  • If Network Unreachable / Timeout: Check local internet connectivity and firewall rules.")
        print("=" * 80)
        return 1


def main() -> None:
    parser = argparse.ArgumentParser(
        description="VeteranDesk PSX Trading Agent — Standalone Telegram Delivery Verification"
    )
    parser.add_argument(
        "--token",
        dest="token",
        default=None,
        help="Optional: Override TELEGRAM_BOT_TOKEN for this test run without editing .env",
    )
    parser.add_argument(
        "--chat-id",
        dest="chat_id",
        default=None,
        help="Optional: Override TELEGRAM_CHAT_ID for this test run without editing .env",
    )
    parser.add_argument(
        "--text",
        dest="text",
        default=None,
        help="Optional: Override the default test message text",
    )
    args = parser.parse_args()

    exit_code = run_telegram_test(
        cli_token=args.token,
        cli_chat_id=args.chat_id,
        custom_text=args.text,
    )
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
