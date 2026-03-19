"""
Dukascopy M15 Forex Data Downloader
=====================================
Downloads free M15 OHLCV data from Dukascopy (Swiss bank).
No account or API key required.
Data goes back to 2003 for major pairs.

Usage:
    python backtest/fetch_dukascopy.py --pair EURUSD --start 2020-01-01
    python backtest/fetch_dukascopy.py --pair GBPUSD --start 2020-01-01
    python backtest/fetch_dukascopy.py --all --start 2022-01-01

Output:
    backtest/data/EURUSD_M15.json  (readable by forex dashboard)
    backtest/data/GBPUSD_M15.json
    etc.
"""

import struct
import lzma
import json
import time
import logging
import argparse
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("dukascopy")

OUTPUT_DIR = Path(__file__).parent / "data"
OUTPUT_DIR.mkdir(exist_ok=True)

PAIRS = {
    "EURUSD": "EUR/USD",
    "GBPUSD": "GBP/USD",
    "USDJPY": "USD/JPY",
    "AUDUSD": "AUD/USD",
    "USDCHF": "USD/CHF",
}

# Dukascopy instrument names
DUKA_SYMBOLS = {
    "EURUSD": "EURUSD",
    "GBPUSD": "GBPUSD",
    "USDJPY": "USDJPY",
    "AUDUSD": "AUDUSD",
    "USDCHF": "USDCHF",
}

# Point sizes (for price conversion)
POINT_SIZE = {
    "EURUSD": 1e-5,
    "GBPUSD": 1e-5,
    "USDJPY": 1e-3,
    "AUDUSD": 1e-5,
    "USDCHF": 1e-5,
}


def fetch_dukascopy_hour(symbol: str, dt: datetime) -> list[dict]:
    """
    Fetch one hour of M1 tick data from Dukascopy and aggregate to M15.
    Dukascopy provides data in hourly .bi5 (LZMA-compressed binary) files.
    """
    url = (
        f"https://datafeed.dukascopy.com/datafeed/{DUKA_SYMBOLS[symbol]}/"
        f"{dt.year}/{dt.month-1:02d}/{dt.day:02d}/{dt.hour:02d}h_ticks.bi5"
    )

    try:
        r = requests.get(url, timeout=15)
        if r.status_code == 404:
            return []
        r.raise_for_status()
        if len(r.content) == 0:
            return []
    except Exception as e:
        log.debug(f"Fetch error {url}: {e}")
        return []

    # Decompress LZMA
    try:
        raw = lzma.decompress(r.content)
    except Exception:
        return []

    # Binary format: 5 x int32 per tick
    # [timestamp_ms_offset, ask*point, bid*point, ask_vol, bid_vol]
    point = POINT_SIZE[symbol]
    ticks = []
    record_size = 20
    for i in range(0, len(raw) - record_size + 1, record_size):
        chunk = raw[i:i + record_size]
        if len(chunk) < record_size:
            break
        ts_offset, ask_raw, bid_raw, ask_vol, bid_vol = struct.unpack('>IIIff', chunk)
        ts_ms = int(dt.timestamp() * 1000) + ts_offset
        mid   = (ask_raw + bid_raw) / 2 * point
        ticks.append({'ts': ts_ms, 'price': mid, 'vol': ask_vol + bid_vol})

    if not ticks:
        return []

    # Aggregate ticks to M15 bars
    bars = []
    bar_start = (ticks[0]['ts'] // (15 * 60 * 1000)) * (15 * 60 * 1000)
    bar_ticks = []

    for tick in ticks:
        bar_idx = (tick['ts'] // (15 * 60 * 1000)) * (15 * 60 * 1000)
        if bar_idx != bar_start and bar_ticks:
            prices = [t['price'] for t in bar_ticks]
            bars.append({
                't': bar_start // 1000,
                'o': bar_ticks[0]['price'],
                'h': max(prices),
                'l': min(prices),
                'c': bar_ticks[-1]['price'],
                'v': sum(t['vol'] for t in bar_ticks),
            })
            bar_start  = bar_idx
            bar_ticks  = []
        bar_ticks.append(tick)

    if bar_ticks:
        prices = [t['price'] for t in bar_ticks]
        bars.append({
            't': bar_start // 1000,
            'o': bar_ticks[0]['price'],
            'h': max(prices),
            'l': min(prices),
            'c': bar_ticks[-1]['price'],
            'v': sum(t['vol'] for t in bar_ticks),
        })

    return bars


def download_pair(symbol: str, start_date: str, end_date: str = None) -> list[dict]:
    """Download M15 bars for a forex pair between start and end dates."""
    start_dt = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end_dt   = datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=timezone.utc) \
               if end_date else datetime.now(timezone.utc)

    all_bars = []
    current  = start_dt
    total_hours = int((end_dt - start_dt).total_seconds() / 3600)
    done = 0

    log.info(f"Downloading {symbol} M15 from {start_date} to {end_dt.strftime('%Y-%m-%d')} (~{total_hours} hours)")

    while current < end_dt:
        bars = fetch_dukascopy_hour(symbol, current)
        all_bars.extend(bars)
        current += timedelta(hours=1)
        done += 1

        if done % 100 == 0:
            pct = done / total_hours * 100
            log.info(f"  {symbol}: {done}/{total_hours} hours ({pct:.1f}%) — {len(all_bars)} bars so far")

        time.sleep(0.05)  # be polite to Dukascopy

    all_bars.sort(key=lambda b: b['t'])
    log.info(f"Downloaded {len(all_bars)} M15 bars for {symbol}")
    return all_bars


def save_pair(symbol: str, bars: list[dict]):
    """Save bars to JSON file in backtest/data/."""
    path = OUTPUT_DIR / f"{symbol}_M15.json"
    with open(path, 'w') as f:
        json.dump({
            'symbol':    symbol,
            'interval':  'M15',
            'generated': datetime.now(timezone.utc).isoformat(),
            'bars':      bars,
        }, f)
    log.info(f"Saved {len(bars)} bars to {path}")
    return path


def main():
    parser = argparse.ArgumentParser(description='Download Dukascopy M15 forex data')
    parser.add_argument('--pair',  type=str, help='Pair to download e.g. EURUSD')
    parser.add_argument('--all',   action='store_true', help='Download all 5 pairs')
    parser.add_argument('--start', type=str, default='2022-01-01', help='Start date YYYY-MM-DD')
    parser.add_argument('--end',   type=str, default=None, help='End date YYYY-MM-DD')
    args = parser.parse_args()

    pairs = list(PAIRS.keys()) if args.all else [args.pair.upper()] if args.pair else []

    if not pairs:
        print("Specify --pair EURUSD or --all")
        return

    for symbol in pairs:
        if symbol not in PAIRS:
            log.error(f"Unknown pair {symbol}. Available: {list(PAIRS.keys())}")
            continue
        bars = download_pair(symbol, args.start, args.end)
        if bars:
            save_pair(symbol, bars)
        else:
            log.error(f"No data downloaded for {symbol}")


if __name__ == "__main__":
    main()
