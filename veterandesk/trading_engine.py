"""
VeteranDesk Autonomous Quantitative Trading Engine.

Integrates:
1. PSX DPS Market Data Scraper (intraday timeseries & live quotes)
2. 1-Minute Candle Builder
3. Opening Range Breakout (ORB v1.0) Deterministic Strategy
4. Risk & Discipline Engine (1% max risk, 2% daily loss, 3 trades/day, 15:00 cutoff)
5. Paper Broker with Double-Entry Ledger Bookkeeping
6. Telegram & Discord Alert Notifications
7. Prominent, structured cycle & check logging for production monitoring
"""

from __future__ import annotations

import os
import threading
import time
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Set

from veterandesk.alerts.discord import discord_service
from veterandesk.alerts.telegram import telegram_service
from veterandesk.config import PKT_TZ, settings
from veterandesk.execution.ledger import DoubleEntryLedger
from veterandesk.execution.paper_broker import DemoTrade, ExitReason, PaperBroker
from veterandesk.logging import get_logger
from veterandesk.market_data.scraper import PSXDpsScraper
from veterandesk.risk.engine import RiskEngine, check_daily_halt_from_db
from veterandesk.strategy.models import SignalStatus, TradeSignal
from veterandesk.strategy.orb import compute_orb_signal

logger = get_logger("veterandesk.trading_engine")


def is_psx_market_open(now_pkt: Optional[datetime] = None) -> tuple[bool, str]:
    """
    Check if PSX equity market is currently open.
    PSX trading hours: Monday - Friday, 09:15 to 15:30 PKT.
    """
    now = now_pkt or datetime.now(PKT_TZ)
    # Weekday check (0 = Monday, 4 = Friday, 5 = Saturday, 6 = Sunday)
    if now.weekday() >= 5:
        day_name = now.strftime("%A")
        return False, f"Weekend ({day_name}) - PSX is closed on Saturdays and Sundays"

    t = now.time()
    if t < settings.market_open_pkt:
        return (
            False,
            f"Pre-market (Current: {t.strftime('%H:%M:%S')} PKT, Opens at {settings.market_open_pkt.strftime('%H:%M:%S')} PKT)",
        )
    if t >= settings.market_close_pkt:
        return (
            False,
            f"Post-market (Current: {t.strftime('%H:%M:%S')} PKT, Closed at {settings.market_close_pkt.strftime('%H:%M:%S')} PKT)",
        )

    return True, f"Market OPEN ({t.strftime('%H:%M:%S')} PKT)"


class TradingEngine:
    """
    Core background trading loop runner for VeteranDesk.
    Coordinates scraping, candle construction, ORB breakout detection,
    risk evaluation, and paper execution on every poll cycle.
    """

    def __init__(
        self,
        ledger: Optional[DoubleEntryLedger] = None,
        broker: Optional[PaperBroker] = None,
        risk_engine: Optional[RiskEngine] = None,
        scraper: Optional[PSXDpsScraper] = None,
    ) -> None:
        self.ledger = ledger or DoubleEntryLedger(starting_balance_pkr=settings.starting_balance_pkr)
        self.broker = broker or PaperBroker(ledger=self.ledger)
        self.risk_engine = risk_engine or RiskEngine(
            max_risk_per_trade_pct=settings.max_risk_per_trade_pct,
            max_daily_loss_pct=settings.max_daily_loss_pct,
            max_intraday_trades=settings.max_intraday_trades_per_day,
            entry_cutoff_pkt=settings.entry_cutoff_pkt,
            force_close_pkt=settings.force_close_pkt,
            max_adv_pct=settings.max_adv_percentage,
        )
        self.scraper = scraper or PSXDpsScraper()
        self.tickers_traded_today: Set[str] = set()
        self.current_session_date: Optional[date] = None
        self.cycle_count: int = 0
        self._is_running: bool = False
        self._thread: Optional[threading.Thread] = None

        # Attempt to recover open trades from Supabase on startup
        recovered = self.broker.load_open_trades_from_db()
        if recovered > 0:
            for t in self.broker.open_trades.values():
                self.tickers_traded_today.add(t.ticker)

    def _reset_session_if_new_day(self, today: date) -> None:
        if self.current_session_date != today:
            logger.info("new_trading_session_initialized", date=str(today), previous=str(self.current_session_date))
            self.current_session_date = today
            self.tickers_traded_today.clear()
            # Reload any open trades from database
            self.broker.load_open_trades_from_db()
            for t in self.broker.open_trades.values():
                self.tickers_traded_today.add(t.ticker)

    def _is_already_halted_today(self) -> bool:
        """Check if trading is already halted for the current PKT date."""
        if self.current_session_date is None:
            return False
        return check_daily_halt_from_db(self.current_session_date)

    def check_open_positions_for_exits(self, now_pkt: datetime) -> List[DemoTrade]:
        """
        Scan all open positions against live market price for:
        1. 15:20 PKT mandatory time cutoff -> TIME_STOP_1520
        2. Stop-loss breach -> STOP_HIT
        3. Target reached -> TARGET_HIT
        """
        closed_this_cycle: List[DemoTrade] = []
        open_list = list(self.broker.open_trades.values())
        if not open_list:
            return closed_this_cycle

        for trade in open_list:
            ticker = trade.ticker
            quote = self.scraper.fetch_ticker_quote(ticker)
            if not quote:
                logger.warning("position_monitor_fetch_failed", ticker=ticker, trade_id=trade.trade_id)
                continue

            current_price = float(quote["price"])
            exit_reason = PaperBroker.evaluate_exit_condition(
                trade=trade,
                scraped_price=current_price,
                current_time_pkt=now_pkt.time(),
            )

            if exit_reason is not None:
                logger.info(
                    "position_exit_triggered",
                    trade_id=trade.trade_id,
                    ticker=ticker,
                    reason=exit_reason.value,
                    current_price=current_price,
                    stop_loss=trade.stop_loss,
                    target=trade.target_price,
                )
                try:
                    closed_trade = self.broker.execute_exit(
                        trade_id=trade.trade_id,
                        scraped_price=current_price,
                        exit_reason=exit_reason,
                    )
                    closed_this_cycle.append(closed_trade)
                except Exception as ex:
                    logger.error("position_exit_execution_error", trade_id=trade.trade_id, error=str(ex))
            else:
                logger.info(
                    "position_holding",
                    trade_id=trade.trade_id,
                    ticker=ticker,
                    entry=trade.filled_entry_price,
                    current=current_price,
                    stop_loss=trade.stop_loss,
                    target=trade.target_price,
                    unrealized_pnl_pct=round(((current_price - trade.filled_entry_price) / trade.filled_entry_price) * 100.0, 2),
                )

        return closed_this_cycle

    def run_trading_cycle(self, force_scan: bool = False) -> Dict[str, Any]:
        """
        Execute a single complete trading cycle:
        1. Check market hours (or force_scan)
        2. Evaluate exits for open positions
        3. Scrape watchlist tickers from DPS & build 1m candles
        4. Evaluate ORB breakout strategy
        5. Pass signals through Risk Engine
        6. Execute paper buy orders & dispatch alerts
        7. Log detailed diagnostics
        """
        self.cycle_count += 1
        start_time = time.perf_counter()
        now_pkt = datetime.now(PKT_TZ)
        today = now_pkt.date()
        self._reset_session_if_new_day(today)

        is_open, market_status_msg = is_psx_market_open(now_pkt)
        should_scan = is_open or force_scan or os.environ.get("FORCE_MARKET_SCAN", "").strip().lower() in ("1", "true", "yes")

        logger.info(
            "trading_cycle_started",
            cycle=self.cycle_count,
            market_open=is_open,
            market_status=market_status_msg,
            cash_balance=round(self.ledger.cash_balance, 2),
            open_positions=len(self.broker.open_trades),
            timestamp=now_pkt.strftime("%Y-%m-%d %H:%M:%S PKT"),
        )

        # Step 1: Manage Exits on Open Positions
        closed_trades = self.check_open_positions_for_exits(now_pkt)

        # Step 2: If Market is Closed and Not Forcing Scan, Idle Gracefully
        if not should_scan:
            duration = time.perf_counter() - start_time
            logger.info(
                "trading_cycle_idle_market_closed",
                cycle=self.cycle_count,
                reason=market_status_msg,
                duration_sec=round(duration, 2),
            )
            return {
                "cycle": self.cycle_count,
                "status": "MARKET_CLOSED",
                "market_status_msg": market_status_msg,
                "closed_trades": [t.trade_id for t in closed_trades],
                "duration_sec": round(duration, 2),
            }

        # Step 3: Scan Watchlist Tickers
        watchlist = settings.watchlist
        signals_evaluated = 0
        signals_fired = 0
        trades_executed = 0

        for ticker in watchlist:
            try:
                quote, candles = self.scraper.fetch_intraday_data(ticker)
            except Exception as ex:
                logger.warning("scraper_fetch_exception", ticker=ticker, error=str(ex))
                continue

            if not quote or not candles:
                logger.warning(
                    "scraper_no_data",
                    ticker=ticker,
                    has_quote=bool(quote),
                    candles_count=len(candles) if candles else 0,
                )
                continue

            latest_price = float(quote["price"])
            latest_volume = int(quote["volume"])
            candle_count = len(candles)

            logger.info(
                "scraper_tick_received",
                ticker=ticker,
                price=latest_price,
                volume=latest_volume,
                candles_count=candle_count,
                data_status=quote.get("data_status", "ok"),
            )

            # Check if this ticker was already traded today (1 ORB trade per ticker per day)
            if ticker in self.tickers_traded_today:
                logger.info(
                    "orb_check_skipped_already_traded",
                    ticker=ticker,
                    reason="Max 1 trade per ticker per day already executed",
                )
                continue

            # Check if past entry cutoff (15:00 PKT)
            if now_pkt.time() >= settings.entry_cutoff_pkt:
                logger.info(
                    "orb_check_skipped_past_cutoff",
                    ticker=ticker,
                    current_time=now_pkt.strftime("%H:%M:%S"),
                    cutoff="15:00:00 PKT",
                )
                continue

            # Check if enough candles exist for opening range (15-min range + 1 breakout candle)
            if candle_count < settings.orb_range_minutes + 1:
                logger.info(
                    "orb_check_waiting_for_opening_range",
                    ticker=ticker,
                    candles_available=candle_count,
                    candles_needed=settings.orb_range_minutes + 1,
                    range_minutes=settings.orb_range_minutes,
                )
                continue

            # Compute ORB Strategy
            signals_evaluated += 1
            signal = compute_orb_signal(
                ticker=ticker,
                candles_1m=candles,
                range_minutes=settings.orb_range_minutes,
                volume_multiplier=settings.orb_volume_multiplier,
                target_multiplier=settings.orb_target_range_multiplier,
                session_id=settings.session_id,
            )

            # Compute stats for clear logging
            range_candles = candles[: settings.orb_range_minutes]
            range_high = max(float(c["high"]) for c in range_candles)
            range_low = min(float(c["low"]) for c in range_candles)
            avg_range_vol = sum(float(c["volume"]) for c in range_candles) / len(range_candles)
            latest_close = float(candles[-1]["close"])
            latest_candle_vol = float(candles[-1]["volume"])
            vol_required = avg_range_vol * settings.orb_volume_multiplier

            if signal is None:
                logger.info(
                    "orb_check_no_signal",
                    ticker=ticker,
                    range_high=round(range_high, 2),
                    range_low=round(range_low, 2),
                    latest_close=round(latest_close, 2),
                    latest_vol=int(latest_candle_vol),
                    avg_range_vol=int(avg_range_vol),
                    vol_required=int(vol_required),
                    status="NO_BREAKOUT",
                )
                continue

            # ORB Breakout Signal Generated!
            signals_fired += 1
            logger.info(
                "orb_signal_fired",
                signal_id=signal.signal_id,
                ticker=ticker,
                action=signal.action.value,
                entry=signal.entry_price,
                stop=signal.stop_loss,
                target=signal.target_price,
                rr=signal.reward_risk_ratio,
                confidence=signal.confidence_pct,
            )

            # Step 4: Risk & Discipline Engine Evaluation
            open_pos = [{"ticker": t.ticker, "shares": t.shares} for t in self.broker.open_trades.values()]
            realized_loss = abs(min(0.0, self.ledger.realized_pnl))
            trades_today = len(self.broker.closed_trades) + len(self.broker.open_trades)
            is_already_halted = self._is_already_halted_today()

            assessment = self.risk_engine.evaluate_signal(
                signal=signal,
                account_balance=self.ledger.cash_balance,
                current_day_realized_loss=realized_loss,
                trades_executed_today=trades_today,
                current_time_pkt=now_pkt.time(),
                twenty_day_adv=5000000.0,
                open_positions=open_pos,
                is_already_halted=is_already_halted,
            )

            if not assessment.is_approved:
                logger.warning(
                    "risk_engine_rejected_signal",
                    ticker=ticker,
                    signal_id=signal.signal_id,
                    rejection_reasons=assessment.rejection_reasons,
                )
                continue

            logger.info(
                "risk_engine_approved_signal",
                ticker=ticker,
                signal_id=signal.signal_id,
                approved_shares=assessment.approved_shares,
                risk_pct=assessment.risk_pct_used,
            )

            # Step 5: Execute Paper Buy Order
            signal.position_size = assessment.approved_shares
            signal.status = SignalStatus.APPROVED

            try:
                trade = self.broker.execute_buy(
                    signal=signal,
                    shares=assessment.approved_shares,
                    scraped_price=signal.entry_price,
                )
                self.tickers_traded_today.add(ticker)
                trades_executed += 1

                logger.info(
                    "trade_executed_successfully",
                    trade_id=trade.trade_id,
                    ticker=trade.ticker,
                    shares=trade.shares,
                    fill_price=trade.filled_entry_price,
                    stop_loss=trade.stop_loss,
                    target=trade.target_price,
                    cash_remaining=self.ledger.cash_balance,
                )

                # Dispatch Real-Time Alerts
                try:
                    telegram_service.send_signal_alert(
                        signal=signal,
                        shares=assessment.approved_shares,
                        reason_lines=f"ORB breakout approved by Risk Engine.\nRisk allocated: {assessment.risk_pct_used:.2f}% equity.",
                    )
                except Exception as ex:
                    logger.warning("telegram_alert_dispatch_failed", error=str(ex))

                try:
                    discord_service.send_signal_alert(
                        signal=signal,
                        shares=assessment.approved_shares,
                        reason_lines=f"ORB breakout approved by Risk Engine.\nRisk allocated: {assessment.risk_pct_used:.2f}% equity.",
                    )
                except Exception as ex:
                    logger.warning("discord_alert_dispatch_failed", error=str(ex))

            except Exception as ex:
                logger.error("trade_execution_failed", ticker=ticker, error=str(ex))

        duration = time.perf_counter() - start_time
        logger.info(
            "trading_cycle_completed",
            cycle=self.cycle_count,
            duration_sec=round(duration, 2),
            tickers_scanned=len(watchlist),
            signals_evaluated=signals_evaluated,
            signals_fired=signals_fired,
            trades_executed=trades_executed,
            open_positions=len(self.broker.open_trades),
            cash_balance=round(self.ledger.cash_balance, 2),
        )

        return {
            "cycle": self.cycle_count,
            "status": "COMPLETED",
            "duration_sec": round(duration, 2),
            "tickers_scanned": len(watchlist),
            "signals_evaluated": signals_evaluated,
            "signals_fired": signals_fired,
            "trades_executed": trades_executed,
            "open_positions": len(self.broker.open_trades),
            "closed_trades": [t.trade_id for t in closed_trades],
        }

    def start_background_loop(self, interval_seconds: int = 30) -> threading.Thread:
        """
        Start the trading loop in a background daemon thread.
        Runs every interval_seconds (default 30s).
        """
        if self._is_running:
            logger.warning("trading_loop_already_running")
            return self._thread  # type: ignore

        self._is_running = True

        def _loop() -> None:
            logger.info("trading_engine_background_thread_started", interval_seconds=interval_seconds)
            while self._is_running:
                try:
                    self.run_trading_cycle()
                except Exception as ex:
                    logger.error("trading_cycle_uncaught_error", error=str(ex))

                # Sleep interval
                time.sleep(interval_seconds)

        self._thread = threading.Thread(target=_loop, name="VeteranDesk-TradingEngine", daemon=True)
        self._thread.start()
        return self._thread

    def stop_background_loop(self) -> None:
        """Stop the background trading loop."""
        self._is_running = False
        logger.info("trading_engine_background_thread_stopping")
