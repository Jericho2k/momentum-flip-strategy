"""
Backtester + Parameter Optimizer
==================================
- Backtester: simulates trades on historical bars, computes full stats
- DataFetcher: downloads H1 OHLCV from Bybit (free, no auth needed)
- Optimizer: grid searches MACD/ADX params, ranks by Sharpe ratio
"""

import json
import math
import time
import logging
import itertools
from dataclasses import dataclass, asdict
from typing import Optional
from datetime import datetime, timezone

import requests

try:
    from backtest.macd_adx_strategy import Bar, Signal, StrategyParams, generate_signals
except ModuleNotFoundError:
    from macd_adx_strategy import Bar, Signal, StrategyParams, generate_signals

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("backtester")


# ── Data fetcher (Bybit public API — no key needed) ────────────────────────────

def fetch_bybit_ohlcv(
    symbol: str = "SOLUSDT",
    interval: str = "60",
    days: int = 365,
) -> list[Bar]:
    """
    Fetch up to `days` days of H1 OHLCV from Bybit public API.
    Returns list of Bar objects, oldest first.
    No API key required.
    """
    base_url = "https://api.bybit.com/v5/market/kline"
    limit    = 200          # Bybit max per request
    bars     = []
    end_ts   = int(time.time() * 1000)
    target   = days * 24   # total hourly bars wanted

    log.info(f"Fetching {days}d of {symbol} H{interval} data from Bybit...")

    while len(bars) < target:
        params = {
            "category": "linear",
            "symbol":   symbol,
            "interval": interval,
            "limit":    limit,
            "end":      end_ts,
        }
        try:
            r = requests.get(base_url, params=params, timeout=15)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            log.error(f"Fetch error: {e}")
            break

        raw = data.get("result", {}).get("list", [])
        if not raw:
            break

        # Bybit returns newest first: [ts, open, high, low, close, volume, turnover]
        batch = []
        for c in raw:
            batch.append(Bar(
                timestamp = int(c[0]) // 1000,
                open      = float(c[1]),
                high      = float(c[2]),
                low       = float(c[3]),
                close     = float(c[4]),
                volume    = float(c[5]),
            ))

        batch.sort(key=lambda b: b.timestamp)
        bars = batch + bars

        oldest_ts   = int(raw[-1][0])
        end_ts      = oldest_ts - 1
        log.info(f"  Fetched {len(bars)} bars so far...")

        if len(raw) < limit:
            break
        time.sleep(0.3)

    bars.sort(key=lambda b: b.timestamp)
    log.info(f"Total bars fetched: {len(bars)}")
    return bars


def fetch_coingecko_ohlcv(
    coin_id: str = "solana",
    days: int = 365,
    vs_currency: str = "usd",
) -> list[Bar]:
    """
    Fetch OHLCV data from CoinGecko free API.
    Returns H1 bars for up to 90 days, or H4 bars for up to 365 days,
    or daily bars for max history (2020–present).

    No API key required for free tier.
    Rate limit: 30 calls/minute — we only need 1 call.

    Args:
        coin_id:     CoinGecko coin ID (solana, bitcoin, ethereum)
        days:        Number of days of history (up to 'max' for full history)
        vs_currency: Quote currency (usd, eur, btc)
    """
    import time as _time

    # CoinGecko auto-selects granularity based on days:
    # 1 day       → 5-minute intervals
    # 2–90 days   → hourly intervals
    # 91–365 days → daily intervals
    # 'max'       → daily intervals (from coin inception)

    log.info(f"Fetching {coin_id} OHLCV from CoinGecko (days={days})...")

    url = f"https://api.coingecko.com/api/v3/coins/{coin_id}/ohlc"
    params = {
        "vs_currency": vs_currency,
        "days":        str(days),
    }

    try:
        r = requests.get(url, params=params, timeout=30)
        r.raise_for_status()
        raw = r.json()
    except Exception as e:
        log.error(f"CoinGecko fetch failed: {e}")
        return []

    if not raw or not isinstance(raw, list):
        log.error(f"CoinGecko returned unexpected data: {raw}")
        return []

    # CoinGecko OHLC format: [timestamp_ms, open, high, low, close]
    bars = []
    for c in raw:
        try:
            bars.append(Bar(
                timestamp = int(c[0]) // 1000,
                open      = float(c[1]),
                high      = float(c[2]),
                low       = float(c[3]),
                close     = float(c[4]),
                volume    = 0.0,  # CoinGecko OHLC endpoint doesn't include volume
            ))
        except (IndexError, ValueError):
            continue

    bars.sort(key=lambda b: b.timestamp)
    log.info(f"CoinGecko: {len(bars)} bars fetched")
    if bars:
        log.info(f"From: {datetime.fromtimestamp(bars[0].timestamp, tz=timezone.utc).strftime('%Y-%m-%d')}")
        log.info(f"To:   {datetime.fromtimestamp(bars[-1].timestamp, tz=timezone.utc).strftime('%Y-%m-%d')}")
        log.info(f"Price range: ${min(b.low for b in bars):.2f} – ${max(b.high for b in bars):.2f}")

    return bars


def fetch_coingecko_full_history(
    coin_id: str = "solana",
    vs_currency: str = "usd",
) -> list[Bar]:
    """
    Fetch ~2 years of daily OHLC from CoinGecko free tier.
    Uses the /ohlc endpoint with days=max (free, no key needed).
    """
    log.info(f"Fetching {coin_id} OHLC from CoinGecko free tier...")

    url = f"https://api.coingecko.com/api/v3/coins/{coin_id}/ohlc"
    params = {
        "vs_currency": vs_currency,
        "days":        "max",
    }

    try:
        r = requests.get(
            url,
            params=params,
            headers={"accept": "application/json"},
            timeout=30
        )
        r.raise_for_status()
        raw = r.json()
    except Exception as e:
        log.error(f"CoinGecko fetch failed: {e}")
        return []

    if not raw or not isinstance(raw, list):
        log.error(f"Unexpected response: {raw}")
        return []

    bars = []
    for c in raw:
        try:
            bars.append(Bar(
                timestamp = int(c[0]) // 1000,
                open      = float(c[1]),
                high      = float(c[2]),
                low       = float(c[3]),
                close     = float(c[4]),
                volume    = 0.0,
            ))
        except (IndexError, ValueError):
            continue

    bars.sort(key=lambda b: b.timestamp)
    log.info(f"Fetched {len(bars)} bars")
    if bars:
        log.info(f"From: {datetime.fromtimestamp(bars[0].timestamp, tz=timezone.utc).strftime('%Y-%m-%d')}")
        log.info(f"To:   {datetime.fromtimestamp(bars[-1].timestamp, tz=timezone.utc).strftime('%Y-%m-%d')}")
        log.info(f"Price range: ${min(b.low for b in bars):.2f} – ${max(b.high for b in bars):.2f}")

    return bars


# ── Trade simulation ───────────────────────────────────────────────────────────

@dataclass
class Trade:
    entry_ts:    int
    exit_ts:     int
    direction:   str      # 'LONG' or 'SHORT'
    entry_price: float
    exit_price:  float
    pnl_pct:     float    # % PnL on the position (leveraged)
    exit_reason: str
    bars_held:   int


def simulate_trades(bars: list[Bar], signals: list[Signal], params: StrategyParams) -> list[Trade]:
    """
    Turn signals into closed trades.
    PnL is calculated per-position (not relative to full account).
    """
    trades = []
    open_trade = None   # (signal, bar_index)

    sig_map = {s.bar_index: s for s in signals}

    for i, bar in enumerate(bars):
        # Check stop-loss on open trade
        if open_trade is not None and params.sl_pips > 0:
            sig = open_trade
            if sig.action == 'LONG'  and bar.low  <= sig.price - params.sl_pips:
                exit_p = sig.price - params.sl_pips
                pnl    = (exit_p - sig.price) / sig.price * 100 * params.leverage
                trades.append(Trade(sig.timestamp, bar.timestamp, 'LONG',
                                    sig.price, exit_p, round(pnl, 4), 'stop_loss',
                                    i - signals.index(sig)))
                open_trade = None
                continue
            if sig.action == 'SHORT' and bar.high >= sig.price + params.sl_pips:
                exit_p = sig.price + params.sl_pips
                pnl    = (sig.price - exit_p) / sig.price * 100 * params.leverage
                trades.append(Trade(sig.timestamp, bar.timestamp, 'SHORT',
                                    sig.price, exit_p, round(pnl, 4), 'stop_loss',
                                    i - signals.index(sig)))
                open_trade = None
                continue

        if i not in sig_map:
            continue

        new_sig = sig_map[i]

        # Close existing trade on flip or close signal
        if open_trade is not None:
            prev = open_trade
            exit_p = new_sig.price
            if prev.action == 'LONG':
                pnl = (exit_p - prev.price) / prev.price * 100 * params.leverage
                direction = 'LONG'
            else:
                pnl = (prev.price - exit_p) / prev.price * 100 * params.leverage
                direction = 'SHORT'
            bars_held = i - next((j for j, s in enumerate(signals) if s.bar_index == prev.bar_index), i)
            trades.append(Trade(prev.timestamp, new_sig.timestamp, direction,
                                prev.price, exit_p, round(pnl, 4), new_sig.reason,
                                max(1, bars_held)))
            open_trade = None

        if new_sig.action in ('LONG', 'SHORT'):
            open_trade = new_sig

    return trades


# ── Performance statistics ─────────────────────────────────────────────────────

@dataclass
class BacktestResult:
    params:          dict
    total_trades:    int
    win_rate:        float
    total_pnl_pct:   float
    avg_win_pct:     float
    avg_loss_pct:    float
    profit_factor:   float
    max_drawdown_pct: float
    sharpe:          float
    trades:          list   # list of Trade dicts, omitted in optimizer output
    flat_months:     int    # months with near-zero PnL (like uncle's note)


def compute_stats(trades: list[Trade], params: StrategyParams, include_trades: bool = True) -> BacktestResult:
    if not trades:
        return BacktestResult(asdict(params), 0, 0, 0, 0, 0, 0, 0, 0, [], 0)

    pnls     = [t.pnl_pct for t in trades]
    wins     = [p for p in pnls if p > 0]
    losses   = [p for p in pnls if p <= 0]

    win_rate     = len(wins) / len(pnls) * 100
    total_pnl    = sum(pnls)
    avg_win      = sum(wins)  / len(wins)  if wins   else 0
    avg_loss     = sum(losses)/ len(losses)if losses else 0
    gross_profit = sum(wins)
    gross_loss   = abs(sum(losses))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')

    # Max drawdown (on cumulative PnL curve)
    cum = 0.0; peak = 0.0; max_dd = 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)

    # Sharpe (annualised, assuming H1 bars → ~8760 trades/year theoretical)
    if len(pnls) > 1:
        mean_r = sum(pnls) / len(pnls)
        std_r  = math.sqrt(sum((p - mean_r)**2 for p in pnls) / (len(pnls) - 1))
        sharpe = (mean_r / std_r * math.sqrt(len(pnls))) if std_r > 0 else 0
    else:
        sharpe = 0

    # Count flat months (like uncle's note — months where |pnl| < 2%)
    monthly: dict[str, float] = {}
    for t in trades:
        dt  = datetime.fromtimestamp(t.exit_ts, tz=timezone.utc)
        key = f"{dt.year}-{dt.month:02d}"
        monthly[key] = monthly.get(key, 0) + t.pnl_pct
    flat_months = sum(1 for v in monthly.values() if abs(v) < 2.0)

    return BacktestResult(
        params          = asdict(params),
        total_trades    = len(trades),
        win_rate        = round(win_rate, 2),
        total_pnl_pct   = round(total_pnl, 2),
        avg_win_pct     = round(avg_win, 4),
        avg_loss_pct    = round(avg_loss, 4),
        profit_factor   = round(profit_factor, 3),
        max_drawdown_pct= round(max_dd, 2),
        sharpe          = round(sharpe, 3),
        trades          = [asdict(t) for t in trades] if include_trades else [],
        flat_months     = flat_months,
    )


def run_backtest(bars: list[Bar], params: StrategyParams, include_trades: bool = True) -> BacktestResult:
    signals = generate_signals(bars, params)
    trades  = simulate_trades(bars, signals, params)
    return compute_stats(trades, params, include_trades)


# ── Parameter optimizer ────────────────────────────────────────────────────────

PARAM_GRID = {
    "macd_fast":     [8, 10, 12],
    "macd_slow":     [21, 26, 28],
    "macd_signal":   [3, 5, 9],
    "min_hist_pips": [2.0, 3.3, 5.0],
    "adx_period":    [14],
    "adx_level":     [35.0, 40.0, 45.0],
}


def run_optimizer(
    bars: list[Bar],
    grid: dict = None,
    sort_by: str = "sharpe",
    top_n: int = 10,
    min_trades: int = 30,
) -> list[BacktestResult]:
    """
    Grid search over parameter combinations.
    Returns top_n results sorted by sort_by metric.
    Skips combos with fewer than min_trades trades.
    """
    grid = grid or PARAM_GRID
    keys = list(grid.keys())
    vals = list(grid.values())
    combos = list(itertools.product(*vals))

    log.info(f"Optimizer: {len(combos)} parameter combinations to test on {len(bars)} bars")

    results = []
    for i, combo in enumerate(combos):
        p = dict(zip(keys, combo))
        if p["macd_fast"] >= p["macd_slow"]:
            continue   # invalid: fast must be < slow

        params = StrategyParams(**{k: v for k, v in p.items() if hasattr(StrategyParams, k) or k in StrategyParams.__dataclass_fields__})
        # Only set fields that exist on StrategyParams
        params = StrategyParams(
            macd_fast     = p.get("macd_fast",     10),
            macd_slow     = p.get("macd_slow",     28),
            macd_signal   = p.get("macd_signal",    3),
            min_hist_pips = p.get("min_hist_pips", 3.3),
            adx_period    = p.get("adx_period",    14),
            adx_level     = p.get("adx_level",     45.0),
        )

        result = run_backtest(bars, params, include_trades=False)

        if result.total_trades < min_trades:
            continue

        results.append(result)

        if (i + 1) % 50 == 0:
            log.info(f"  {i+1}/{len(combos)} done | best so far: {max(r.sharpe for r in results):.3f} Sharpe")

    results.sort(key=lambda r: getattr(r, sort_by), reverse=True)
    log.info(f"Optimization complete. Top result: {results[0].total_pnl_pct:.1f}% PnL, Sharpe={results[0].sharpe:.3f}")
    return results[:top_n]


# ── CLI entry ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    mode = sys.argv[1] if len(sys.argv) > 1 else "backtest"

    if mode == "coingecko":
        log.info("Fetching full SOL history from CoinGecko (daily bars, 2020–present)...")
        bars = fetch_coingecko_full_history("solana")
        if not bars:
            log.error("No data fetched")
            sys.exit(1)
        log.info(f"Running backtest on {len(bars)} daily bars...")
        params = StrategyParams()
        result = run_backtest(bars, params)
        print(f"\n=== COINGECKO FULL HISTORY BACKTEST ===")
        print(f"Period:      {datetime.fromtimestamp(bars[0].timestamp, tz=timezone.utc).strftime('%Y-%m-%d')} → {datetime.fromtimestamp(bars[-1].timestamp, tz=timezone.utc).strftime('%Y-%m-%d')}")
        print(f"Bars:        {len(bars)} daily")
        print(f"Trades:      {result.total_trades}")
        print(f"Win rate:    {result.win_rate}%")
        print(f"Total PnL:   {result.total_pnl_pct}%")
        print(f"Prof factor: {result.profit_factor}")
        print(f"Max DD:      {result.max_drawdown_pct}%")
        print(f"Sharpe:      {result.sharpe}")
        print(f"Flat months: {result.flat_months}")

    elif mode == "optimize_cg":
        log.info("Fetching CoinGecko data for optimization...")
        bars = fetch_coingecko_full_history("solana")
        if not bars:
            sys.exit(1)
        log.info("Running optimizer on full history...")
        top = run_optimizer(bars, top_n=10)
        print("\n=== TOP 10 PARAMS — FULL COINGECKO HISTORY ===")
        for i, r in enumerate(top):
            p = r.params
            print(
                f"{i+1:2d}. MACD({p['macd_fast']},{p['macd_slow']},{p['macd_signal']}) "
                f"hist≥{p['min_hist_pips']} ADX({p['adx_period']},≥{p['adx_level']}) | "
                f"PnL={r.total_pnl_pct:.1f}% WR={r.win_rate:.1f}% "
                f"Sharpe={r.sharpe:.3f} DD={r.max_drawdown_pct:.1f}% "
                f"Trades={r.total_trades} FlatMonths={r.flat_months}"
            )

    elif mode == "optimize":
        log.info("Fetching Bybit data for optimization...")
        bars = fetch_bybit_ohlcv("SOLUSDT", "60", days=365)
        top  = run_optimizer(bars, top_n=10)
        print("\n=== TOP 10 PARAMETER SETS ===")
        for i, r in enumerate(top):
            p = r.params
            print(
                f"{i+1:2d}. MACD({p['macd_fast']},{p['macd_slow']},{p['macd_signal']}) "
                f"hist≥{p['min_hist_pips']} ADX({p['adx_period']},≥{p['adx_level']}) | "
                f"PnL={r.total_pnl_pct:.1f}% WR={r.win_rate:.1f}% "
                f"Sharpe={r.sharpe:.3f} DD={r.max_drawdown_pct:.1f}% "
                f"Trades={r.total_trades} FlatMonths={r.flat_months}"
            )

    else:
        log.info("Fetching Bybit data...")
        bars   = fetch_bybit_ohlcv("SOLUSDT", "60", days=365)
        params = StrategyParams()
        result = run_backtest(bars, params)
        print(f"\n=== BACKTEST RESULT ===")
        print(f"Params:      MACD({params.macd_fast},{params.macd_slow},{params.macd_signal}) ADX({params.adx_period},≥{params.adx_level})")
        print(f"Trades:      {result.total_trades}")
        print(f"Win rate:    {result.win_rate}%")
        print(f"Total PnL:   {result.total_pnl_pct}%")
        print(f"Prof factor: {result.profit_factor}")
        print(f"Max DD:      {result.max_drawdown_pct}%")
        print(f"Sharpe:      {result.sharpe}")
        print(f"Flat months: {result.flat_months}")
