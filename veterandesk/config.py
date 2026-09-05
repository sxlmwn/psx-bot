"""Configuration module for VeteranDesk using Pydantic Settings."""

from __future__ import annotations

import json
from datetime import time, timezone, timedelta
from typing import Any, List, Optional
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# PSX Timezone is Pakistan Standard Time (PKT = UTC+5)
PKT_TZ = timezone(timedelta(hours=5))


class FeeStructure(BaseSettings):
    """PSX Broker & Regulatory fee structure (Versioned)."""
    version: str = "PSX_STANDARD_v1"
    broker_commission_pct: float = 0.0015  # 0.15% of trade value
    secp_turnover_pct: float = 0.00003     # 0.003%
    nccpl_charges_pct: float = 0.00002     # 0.002%
    cgt_withholding_pct: float = 0.1500    # 15% on net capital gains (if positive)
    min_slippage_pct: float = 0.0010       # 0.10%
    max_slippage_pct: float = 0.0030       # 0.30%
    default_slippage_pct: float = 0.0020   # 0.20%


class Settings(BaseSettings):
    """System-wide application settings."""
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

    # Core
    environment: str = "development"
    session_id: str = "dev_session_1"
    app_name: str = "VeteranDesk"
    app_version: str = "1.0.0"

    # Database (Supabase PostgreSQL / SQLite fallback)
    database_url: str = Field(default="sqlite+aiosqlite:///./veterandesk.db", alias="DATABASE_URL")
    supabase_url: Optional[str] = Field(default=None, alias="SUPABASE_URL")
    supabase_anon_key: Optional[str] = Field(default=None, alias="SUPABASE_ANON_KEY")
    supabase_service_role_key: Optional[str] = Field(default=None, alias="SUPABASE_SERVICE_ROLE_KEY")
    supabase_key: Optional[str] = Field(default=None, alias="SUPABASE_KEY")

    # Scraper & DPS Market Data
    dps_base_url: str = "https://dps.psx.com.pk"
    scrape_interval_seconds: int = 30
    latency_alert_threshold_seconds: int = 90
    max_consecutive_poll_failures_alert: int = 2
    max_consecutive_poll_failures_halt: int = 5
    user_agents: List[str] = [
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15"
    ]
    watchlist: List[str] = [
        "OGDC", "PPL", "ENGRO", "LUCK", "HUBC", 
        "MCB", "UBL", "HBL", "EFERT", "FFC", 
        "TRG", "SYS", "DGKC", "MLCF", "PSO"
    ]

    # Risk Engine Hard Rules
    starting_balance_pkr: float = 500000.0
    max_risk_per_trade_pct: float = 1.00  # Cannot exceed 1.00%
    max_daily_loss_pct: float = 2.00      # Cannot exceed 2.00%
    max_intraday_trades_per_day: int = 3
    max_adv_percentage: float = 5.00       # Max 5% of 20-day ADV
    min_reward_risk_ratio: float = 1.50    # Minimum R:R ratio

    # Trading Time Cutoffs (PKT)
    market_open_pkt: time = time(9, 15, 0)
    entry_cutoff_pkt: time = time(15, 0, 0)
    force_close_pkt: time = time(15, 20, 0)
    market_close_pkt: time = time(15, 30, 0)

    # Strategy Parameters (ORB v1.0)
    orb_range_minutes: int = 15
    orb_volume_multiplier: float = 1.50
    orb_target_range_multiplier: float = 1.50  # 1.5x - 2.0x range height

    # Graduation Criteria
    graduation_min_trades: int = 30
    graduation_max_drawdown_pct: float = 10.00
    graduation_clean_recent_trades: int = 20

    # Reasoning / Post-Mortem (Claude API)
    anthropic_api_key: Optional[str] = Field(default=None, alias="ANTHROPIC_API_KEY")
    anthropic_model: str = "claude-3-7-sonnet-20250219"
    groq_api_key: Optional[str] = Field(default=None, alias="GROQ_API_KEY")
    use_mock_llm_if_no_key: bool = True

    # Telegram
    telegram_bot_token: Optional[str] = Field(default=None, alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: Optional[str] = Field(default=None, alias="TELEGRAM_CHAT_ID")
    telegram_enabled: bool = False

    # Health Heartbeat
    heartbeat_interval_seconds: int = 60
    max_missed_heartbeat_seconds: int = 120

    @field_validator("max_risk_per_trade_pct")
    @classmethod
    def validate_max_risk(cls, v: float) -> float:
        if v > 1.00:
            raise ValueError("Per-trade risk cannot exceed 1.00% under any circumstance.")
        if v <= 0:
            raise ValueError("Per-trade risk must be strictly positive.")
        return v

    @field_validator("max_daily_loss_pct")
    @classmethod
    def validate_max_daily_loss(cls, v: float) -> float:
        if v > 5.00:
            raise ValueError("Max daily loss cannot exceed 5.00%.")
        return v

    @field_validator("watchlist", mode="before")
    @classmethod
    def parse_watchlist(cls, v: Any) -> List[str]:
        if isinstance(v, str):
            try:
                parsed = json.loads(v)
                if isinstance(parsed, list):
                    return [str(x).upper() for x in parsed]
            except Exception:
                return [str(x).strip().upper() for x in v.split(",") if str(x).strip()]
        if isinstance(v, list):
            return [str(x).upper() for x in v]
        return []


# Global singleton instances
settings = Settings()
fee_structure = FeeStructure()
