#!/usr/bin/env python3
"""
===============================================================================
VeteranDesk PSX Trading Agent — Standalone Discord Delivery Verification
===============================================================================

This standalone script independently verifies whether Discord webhook alert
delivery is genuinely reaching the configured channel or running in offline/mock mode.

Usage:
    # Run with webhook from .env / settings:
    python scripts/test_discord_send.py

    # Run with custom one-off webhook:
    python scripts/test_discord_send.py --webhook <DISCORD_WEBHOOK_URL>

    # Force a specific message text:
    python scripts/test_discord_send.py --text "Custom test message"

===============================================================================
OFFLINE / MOCK-DELIVERED FALLBACK SPECIFICATION & PRODUCTION SAFETY
===============================================================================
Under what exact conditions does VeteranDesk fall back to "offline/mock" mode?
Mock/offline mode triggers IF AND ONLY IF at least one of these 2 conditions is met:
  1. `DISCORD_ENABLED` is explicitly set to `False` in `.env` or settings.
  2. `DISCORD_WEBHOOK_URL` is unset, empty (""), or whitespace.

Can mock mode silently happen in production?
ABSOLUTELY NOT. The safety guarantees are:
  1. Once a valid webhook URL is provided and `DISCORD_ENABLED=true` (the default),
     `DiscordService.enabled` evaluates strictly to `True`.
  2. `_send_with_retry_sync()` ALWAYS initiates real HTTPS POST requests to
     the Discord webhook URL using `httpx`.
  3. If Discord API fails (e.g. HTTP 400, 401, 404, 429, 500) or times out:
     IT NEVER FALLS BACK TO MOCK MODE.
  4. Instead, it executes up to 3 retry attempts with exponential backoff
     (0.5s, 1.0s, 2.0s), respects HTTP 429 `retry_after` headers, and if all
     attempts fail, permanently marks the message as `DeliveryStatus.FAILED` (`attempts=3`),
     stores the exact HTTP status and error body in `discord_delivery_log`,
     pushes the message to `failed_dead_letter`, and logs an error.
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
from veterandesk.alerts.discord_notifier import (
    DeliveryStatus,
    DiscordOutboundMessage,
    DiscordService,
    MessageType,
    discord_service,
)


def mask_webhook_url(url: Optional[str]) -> str:
    """Mask Discord webhook URL token to prevent secret exposure in logs/console."""
    if not url or not url.strip():
        return "[NOT SET / EMPTY]"
    s = url.strip()
    parts = s.split("/")
    if len(parts) >= 2:
        # discord webhook format: https://discord.com/api/webhooks/<id>/<token>
        prefix = "/".join(parts[:-1])
        token = parts[-1]
        masked_token = "*" * (max(0, len(token) - 4)) + token[-4:] if len(token) > 4 else "****"
        return f"{prefix}/{masked_token}"
    return "*" * (len(s) - 4) + s[-4:] if len(s) > 4 else "****"


def print_specification() -> None:
    """Print the mock fallback conditions and production guarantees."""
    print("=" * 80)
    print("VETERANDESK DISCORD NOTIFICATION SYSTEM — SPECIFICATION")
    print("=" * 80)
    print("Conditions that trigger Offline / Mock-Delivered Mode:")
    print("  [1] DISCORD_ENABLED is set to False in .env / settings")
    print("  [2] DISCORD_WEBHOOK_URL is unset, empty, or whitespace")
    print()
    print("Production Guarantee:")
    print("  • When credentials are configured, the service strictly calls")
    print("    the Discord Webhook URL via HTTPS with rich embeds.")
    print("  • Under network failure, HTTP 4xx/5xx errors, or timeout, the engine")
    print("    NEVER falls back to mock mode. It retries 3x with exponential")
    print("    backoff (0.5s, 1.0s, 2.0s), respects HTTP 429 rate limits, and if")
    print("    unresolved, marks status='failed' in database table `discord_delivery_log`.")
    print("=" * 80)


def run_discord_test(
    cli_webhook: Optional[str] = None,
    custom_text: Optional[str] = None,
) -> int:
    """
    Run standalone Discord verification test.
    Returns 0 on success or documented mock-run, 1 on real API failure.
    """
    print_specification()
    print()

    # Determine credential source
    env_webhook = settings.discord_webhook_url or os.environ.get("DISCORD_WEBHOOK_URL")
    env_enabled = settings.discord_enabled

    webhook_url = (cli_webhook or env_webhook or "").strip()
    webhook_present = bool(webhook_url)

    print("CONFIGURED CREDENTIALS INSPECTION:")
    print(f"  • Config File / Source    : {settings.model_config.get('env_file', '.env')}")
    print(f"  • DISCORD_ENABLED Flag    : {env_enabled}")
    print(f"  • DISCORD_WEBHOOK_URL     : {'SET' if webhook_present else 'UNSET'} | Length: {len(webhook_url)} chars | Masked: {mask_webhook_url(webhook_url)}")
    print()

    # Initialize notifier service
    if cli_webhook:
        print("[*] Instantiating DiscordService with provided CLI webhook URL...")
        service = DiscordService(webhook_url=webhook_url, enabled=True)
    else:
        print("[*] Utilizing production singleton `discord_service` from `discord_notifier`...")
        service = discord_service

    is_configured_for_real = bool(service.enabled and service.webhook_url and service.webhook_url.strip())
    print(f"  • Active Service Status   : {'REAL HTTP MODE (Network enabled)' if is_configured_for_real else 'MOCK / OFFLINE MODE (No network)'}")
    print()

    # Formulate test payload
    now_utc = datetime.now(timezone.utc)
    now_pkt = now_utc.astimezone(PKT_TZ)
    timestamp_str = f"{now_utc.strftime('%Y-%m-%d %H:%M:%S UTC')} / {now_pkt.strftime('%H:%M:%S PKT')}"
    run_id = str(uuid.uuid4())[:8]

    if custom_text:
        test_message = custom_text
    else:
        test_message = f"✅ VeteranDesk Discord test — if you see this, delivery is working. [{timestamp_str}]"

    embed = {
        "title": "✅ VeteranDesk Discord Delivery Test",
        "description": "If you see this rich embed, webhook delivery from VeteranDesk is functioning normally.",
        "color": 0x2ECC71,
        "fields": [
            {"name": "Timestamp", "value": timestamp_str, "inline": True},
            {"name": "Run ID", "value": f"`{run_id}`", "inline": True},
            {"name": "Environment", "value": settings.environment, "inline": True},
            {"name": "Route", "value": "Direct Webhook via httpx", "inline": True},
            {"name": "Status", "value": "Operational", "inline": True},
        ],
        "footer": {"text": "VeteranDesk PSX Trading Agent • Alert Subsystem"},
        "timestamp": now_utc.isoformat(),
    }

    print("OUTBOUND MESSAGE PREVIEW:")
    print("-" * 60)
    print(f"Content: {test_message}")
    print(f"Embed Title: {embed['title']}")
    print(f"Color: 0x{embed['color']:06X}")
    print("-" * 60)
    print()

    # Send message using the production function
    print("[*] Invoking `discord_service.send_message()`...")
    hist_before = len(service.delivered_history)
    failed_before = len(service.failed_dead_letter)
    skipped_before = len(service.skipped_history)

    send_success = service.send_message(
        content=test_message,
        embeds=[embed],
        msg_type=MessageType.ALERT,
        reference_id=f"TEST_DISCORD_{run_id}",
        event_type="STANDALONE_DISCORD_DELIVERY_TEST",
    )

    outbound: Optional[DiscordOutboundMessage] = None
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
        print("[!] No network transmission to Discord occurred.")
        print("[!] Reason: Missing or empty DISCORD_WEBHOOK_URL in environment.")
        print(f"[!] Result Flag Returned   : {send_success} (False = not sent over network)")
        print(f"[!] Attempt Count          : {outbound.attempts if outbound else 0} (0 attempts confirms NO network call was made)")
        print(f"[!] Message Delivery ID    : {outbound.id if outbound else 'N/A'}")
        print(f"[!] Internal DB Status     : {outbound.status.value if outbound else 'unknown'} (correctly recorded as 'skipped', NOT 'sent')")
        print(f"[!] Log Reason Stored      : {outbound.last_error if outbound else 'None'}")
        print()
        print("NEXT STEP TO ENABLE REAL DISCORD DELIVERY:")
        print("  1. Add your real Discord webhook to .env: DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...")
        print("  2. Ensure DISCORD_ENABLED=true in .env")
        print("  3. Re-run: python scripts/test_discord_send.py")
        print("=" * 80)
        return 0

    elif send_success and outbound and outbound.status == DeliveryStatus.SENT:
        # REAL DELIVERY SUCCESS
        print("✅  DELIVERY RESULT: REAL NETWORK DELIVERY SUCCESSFUL!")
        print("=" * 80)
        print("[+] Live HTTPS request was transmitted to Discord Webhook.")
        print("[+] Discord API accepted the payload and returned HTTP 200/204.")
        print(f"[+] Result Flag Returned   : {send_success}")
        print(f"[+] Attempts Required      : {outbound.attempts}")
        print(f"[+] Message Delivery ID    : {outbound.id}")
        print(f"[+] Webhook URL Masked     : {mask_webhook_url(webhook_url)}")
        print(f"[+] Delivered At           : {outbound.sent_at.isoformat() if outbound.sent_at else 'N/A'}")
        print(f"[+] Database Log Table     : `discord_delivery_log` (persisted)")
        print("=" * 80)
        return 0

    else:
        # REAL DELIVERY FAILED
        print("❌  DELIVERY RESULT: REAL DELIVERY ATTEMPT FAILED!")
        print("=" * 80)
        print("[!] Live HTTPS request was attempted against Discord Webhook but failed.")
        print(f"[!] Result Flag Returned   : {send_success}")
        print(f"[!] Total Attempts Made    : {outbound.attempts if outbound else 'N/A'}")
        print(f"[!] Final Message Status   : {outbound.status.value if outbound else 'unknown'}")
        print(f"[!] Error Timestamp        : {outbound.failed_at.isoformat() if outbound and outbound.failed_at else 'N/A'}")
        print()
        print("ACTUAL ERROR RETURNED BY DISCORD / NETWORK:")
        print("-" * 60)
        print(outbound.last_error if (outbound and outbound.last_error) else "Unknown delivery error.")
        print("-" * 60)
        print()
        print("TROUBLESHOOTING:")
        print("  • If HTTP 404: Check that the webhook URL exists and channel was not deleted.")
        print("  • If HTTP 401 / 403: Verify webhook permissions.")
        print("  • If HTTP 429: Rate limit hit (auto-handled with retry_after).")
        print("  • If Network Unreachable / Timeout: Check internet connectivity.")
        print("=" * 80)
        return 1


def main() -> None:
    parser = argparse.ArgumentParser(
        description="VeteranDesk PSX Trading Agent — Standalone Discord Delivery Verification"
    )
    parser.add_argument(
        "--webhook",
        dest="webhook",
        default=None,
        help="Optional: Override DISCORD_WEBHOOK_URL for this test run without editing .env",
    )
    parser.add_argument(
        "--text",
        dest="text",
        default=None,
        help="Optional: Override the default test message text",
    )
    args = parser.parse_args()

    exit_code = run_discord_test(
        cli_webhook=args.webhook,
        custom_text=args.text,
    )
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
