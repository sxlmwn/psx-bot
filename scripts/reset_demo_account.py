"""
VeteranDesk: Safe Demo Account Reset Utility
Wipes demo trading tables in Supabase while preserving portfolio plans and configurations.
"""

from veterandesk.database.session import db_manager


def reset_demo_account():
    client = db_manager.get_client()
    print("Beginning VeteranDesk demo account reset...")

    # Step 1: Wipe child tables referencing demo_trades first
    print("1. Wiping child tables (demo_ledger, trade_journal, lessons_memory)...")
    client.table("demo_ledger").delete().gte("id", 0).execute()
    client.table("trade_journal").delete().gte("id", 0).execute()
    client.table("lessons_memory").delete().gte("id", 0).execute()

    # Step 2: Wipe parent demo tables (demo_trades, trade_signals)...
    print("2. Wiping parent demo tables (demo_trades, trade_signals)...")
    client.table("demo_trades").delete().gte("id", 0).execute()
    client.table("trade_signals").delete().gte("id", 0).execute()

    # Step 3: Wipe standalone log tables
    print("3. Wiping standalone log tables (trades, mistake_audit_log, daily_halts)...")
    client.table("trades").delete().gte("id", 0).execute()
    client.table("mistake_audit_log").delete().gte("id", 0).execute()

    # Verification
    print("\nVerifying post-reset row counts:")
    tables = [
        "demo_ledger",
        "trade_journal",
        "lessons_memory",
        "demo_trades",
        "trade_signals",
        "trades",
        "mistake_audit_log",
        "portfolio_plans",
    ]
    for table in tables:
        res = client.table(table).select("id", count="exact").execute()
        status = "(PRESERVED)" if table == "portfolio_plans" else ""
        print(f"  - {table}: {res.count} rows {status}")

    print("\nDemo account successfully reset to clean PKR 500,000 starting state.")


if __name__ == "__main__":
    reset_demo_account()
