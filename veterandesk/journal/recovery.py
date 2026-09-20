"""
Trade Recovery Module for Post-Mortem Backfill.

This module provides functions to reconstruct DemoTrade objects from database rows
and ledger entries, used by both the PostMortemEngine startup recovery and dry-run scripts.
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from veterandesk.execution.paper_broker import DemoTrade, TradeStatus, ExitReason
from veterandesk.strategy.models import SignalAction


def build_recovery_trades(
    client: Any,
    trade_ids: Optional[List[str]] = None,
    ledger_by_trade: Optional[Dict[str, List[Dict[str, Any]]]] = None
) -> List[DemoTrade]:
    """
    Reconstruct DemoTrade objects from DB rows and ledger entries.
    Used by both the engine startup recovery and dry-run scripts.
    
    Args:
        client: Supabase client
        trade_ids: Optional list of trade_ids to reconstruct. If None, fetches all CLOSED trades.
        ledger_by_trade: Optional pre-fetched ledger entries grouped by trade_id.
                      If None, fetches ledger entries with in_("trade_id", ids) if trade_ids provided,
                      otherwise unbounded select (WARNING: avoid in production).
    
    Returns:
        List of reconstructed DemoTrade objects
    """
    # Fetch trades
    if trade_ids:
        trades_res = client.table("demo_trades").select("*").in_("trade_id", trade_ids).execute()
    else:
        # WARNING: Unbounded select - only use when trade_ids is not provided
        trades_res = client.table("demo_trades").select("*").eq("status", "CLOSED").limit(100).execute()
    
    # Fetch ledger entries if not provided
    if ledger_by_trade is None:
        if trade_ids:
            ledger_res = client.table("demo_ledger").select("*").in_("trade_id", trade_ids).execute()
        else:
            # WARNING: Unbounded select - avoid in production
            ledger_res = client.table("demo_ledger").select("*").execute()
        
        ledger_by_trade = {}
        for entry in (ledger_res.data or []):
            entry_trade_id = entry.get("trade_id")
            if entry_trade_id:
                if entry_trade_id not in ledger_by_trade:
                    ledger_by_trade[entry_trade_id] = []
                ledger_by_trade[entry_trade_id].append(entry)
    
    reconstructed_trades = []
    for trade_row in (trades_res.data or []):
        trade_id = trade_row.get("trade_id")
        
        # Parse action
        action_str = trade_row.get("action", "BUY").upper()
        action = SignalAction.BUY if action_str == "BUY" else SignalAction.SELL
        
        # Parse exit reason
        exit_reason_str = trade_row.get("exit_reason")
        exit_reason = ExitReason(exit_reason_str) if exit_reason_str else None
        
        # Parse timestamps
        opened_at_raw = trade_row.get("opened_at")
        opened_at = datetime.fromisoformat(opened_at_raw) if opened_at_raw else datetime.now(timezone.utc)
        
        # Use actual stored values where available
        entry_price = float(trade_row.get("entry_price", 0.0))
        exit_price = float(trade_row.get("exit_price")) if trade_row.get("exit_price") else None
        fees_paid = float(trade_row.get("fees_paid", 0.0))
        
        # Try to reconstruct actual filled prices and per-side fees from ledger
        filled_entry_price = entry_price  # fallback
        filled_exit_price = exit_price  # fallback
        entry_fees = fees_paid / 2  # fallback approximation
        exit_fees = fees_paid / 2  # fallback approximation
        approximated_fields = ["filled_entry_price", "filled_exit_price", "entry_fees", "exit_fees"]
        
        if trade_id in ledger_by_trade:
            entries = ledger_by_trade[trade_id]
            # Parse BUY entry to get filled_entry_price
            for entry in entries:
                desc = entry.get("description", "")
                if "BUY" in desc and "@" in desc:
                    try:
                        # Parse "BUY 1136 OGDC @ 316.42 (Slip: 0.20%)"
                        parts = desc.split("@")
                        if len(parts) >= 2:
                            price_str = parts[1].split("(")[0].strip()
                            filled_entry_price = float(price_str)
                            if "filled_entry_price" in approximated_fields:
                                approximated_fields.remove("filled_entry_price")
                    except (ValueError, IndexError):
                        pass
                # Get entry fees from COMMISSION_EXPENSE + TAX_EXPENSE
                if entry.get("account_name") == "COMMISSION_EXPENSE" and "BUY" in desc:
                    entry_fees = entry.get("debit", 0.0)
                    if "entry_fees" in approximated_fields:
                        approximated_fields.remove("entry_fees")
                if entry.get("account_name") == "TAX_EXPENSE" and "BUY" in desc:
                    entry_fees += entry.get("debit", 0.0)
            
            # Parse EXIT entry to get filled_exit_price
            for entry in entries:
                desc = entry.get("description", "")
                if "EXIT" in desc and "@" in desc:
                    try:
                        # Parse "EXIT 1136 OGDC @ 320.19 (TARGET_HIT)"
                        parts = desc.split("@")
                        if len(parts) >= 2:
                            price_str = parts[1].split("(")[0].strip()
                            filled_exit_price = float(price_str)
                            if "filled_exit_price" in approximated_fields:
                                approximated_fields.remove("filled_exit_price")
                    except (ValueError, IndexError):
                        pass
                # Get exit fees from COMMISSION_EXPENSE + TAX_EXPENSE
                if entry.get("account_name") == "COMMISSION_EXPENSE" and "EXIT" in desc:
                    exit_fees = entry.get("debit", 0.0)
                    if "exit_fees" in approximated_fields:
                        approximated_fields.remove("exit_fees")
                if entry.get("account_name") == "TAX_EXPENSE" and "EXIT" in desc:
                    exit_fees += entry.get("debit", 0.0)
        
        trade = DemoTrade(
            trade_id=trade_id,
            signal_id=trade_row.get("signal_id", f"SIG_{trade_id}"),
            ticker=trade_row.get("ticker", "UNKNOWN"),
            action=action,
            shares=int(trade_row.get("shares", 0)),
            entry_price=entry_price,
            stop_loss=float(trade_row.get("stop_loss", 0.0)),
            target_price=float(trade_row.get("target_price", 0.0)),
            slippage_pct=float(trade_row.get("slippage_pct", 0.002)),
            filled_entry_price=filled_entry_price,
            exit_price=exit_price,
            filled_exit_price=filled_exit_price,
            exit_reason=exit_reason,
            gross_pnl=float(trade_row.get("gross_pnl", 0.0)),
            entry_fees=entry_fees,
            exit_fees=exit_fees,
            net_pnl=float(trade_row.get("net_pnl", 0.0)),
            opened_at=opened_at,
            status=TradeStatus.CLOSED,
            # Preserve data_quality_flag if it exists
            data_quality_flag=trade_row.get("data_quality_flag", "VALID"),
            approximated_fields=approximated_fields if approximated_fields else None,
        )
        
        reconstructed_trades.append(trade)
    
    return reconstructed_trades
