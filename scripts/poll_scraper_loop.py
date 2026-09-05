#!/usr/bin/env python3
"""
Sustained Reliability Poller for PSX DPS Scraper.
Runs 10 consecutive poll cycles against real tickers (OGDC, HBL, HUBC)
with 30s intervals. Logs round-trip HTTP latency, parsed quote data,
and confirms zero failures or data gaps.
"""

import os
import sys
from pathlib import Path

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import time
from datetime import datetime, timezone
from veterandesk.market_data.scraper import PSXDpsScraper

def run_sustained_polling(tickers=("OGDC", "HBL", "HUBC"), cycles=10, interval_seconds=30):
    scraper = PSXDpsScraper()
    print("=" * 80, flush=True)
    print("STARTING SUSTAINED SCRAPER RELIABILITY TEST", flush=True)
    print(f"Target Tickers: {', '.join(tickers)} | Cycles: {cycles} | Interval: {interval_seconds}s", flush=True)
    print(f"Start Time: {datetime.now(timezone.utc).isoformat()}", flush=True)
    print("=" * 80, flush=True)

    total_polls = 0
    successful_polls = 0
    failed_polls = 0
    latencies = {t: [] for t in tickers}

    for cycle in range(1, cycles + 1):
        cycle_ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        print(f"\n--- CYCLE {cycle}/{cycles} [{cycle_ts}] ---", flush=True)

        for ticker in tickers:
            total_polls += 1
            t_start = time.perf_counter()
            quote = scraper.fetch_ticker_quote(ticker)
            latency_ms = (time.perf_counter() - t_start) * 1000.0

            if quote is not None:
                successful_polls += 1
                latencies[ticker].append(latency_ms)
                price = quote.get("price")
                vol = quote.get("volume")
                status = quote.get("data_status")
                gaps = scraper.gap_detector.consecutive_failures
                print(
                    f"  [{ticker}] SUCCESS | Price: PKR {price:,.2f} | Volume: {vol:,} | "
                    f"Latency: {latency_ms:.1f}ms | Status: {status} | Consecutive Gaps: {gaps}",
                    flush=True
                )
            else:
                failed_polls += 1
                gaps = scraper.gap_detector.consecutive_failures
                print(
                    f"  [{ticker}] FAILED  | Latency: {latency_ms:.1f}ms | Consecutive Gaps: {gaps}",
                    flush=True
                )

        if cycle < cycles:
            print(f"Waiting {interval_seconds}s before next cycle...", flush=True)
            time.sleep(interval_seconds)

    print("\n" + "=" * 80, flush=True)
    print("SUSTAINED RELIABILITY SUMMARY", flush=True)
    print("=" * 80, flush=True)
    print(f"Total Cycles: {cycles}", flush=True)
    print(f"Total Ticker Polls: {total_polls}", flush=True)
    print(f"Successful Polls: {successful_polls}/{total_polls} ({successful_polls/total_polls*100:.1f}%)", flush=True)
    print(f"Failed Polls: {failed_polls}", flush=True)
    print(f"Final Consecutive Failures (Gaps): {scraper.gap_detector.consecutive_failures}", flush=True)
    for ticker, lats in latencies.items():
        if lats:
            avg_l = sum(lats) / len(lats)
            min_l = min(lats)
            max_l = max(lats)
            print(f"  {ticker} Latency -> Avg: {avg_l:.1f}ms | Min: {min_l:.1f}ms | Max: {max_l:.1f}ms", flush=True)
    print("=" * 80, flush=True)

    if failed_polls == 0 and scraper.gap_detector.consecutive_failures == 0:
        print("VERDICT: SUSTAINED RELIABILITY CONFIRMED (0 failures, 0 gaps)", flush=True)
        return 0
    else:
        print("VERDICT: FAILURES DETECTED", flush=True)
        return 1

if __name__ == "__main__":
    sys.exit(run_sustained_polling())
