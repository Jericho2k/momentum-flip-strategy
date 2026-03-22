"""
Multi-Pair Forex Execution Bridge
===================================
Runs 4 forex pairs simultaneously on OANDA practice account.
Each pair has independent params, session rules, and position management.
Checks signals every 15 minutes, sends Telegram notifications.

Pairs:
  EUR/USD  MACD(8,28,3)   ADX≥20  kill_hours=16,17,18  skip_monday=True
  GBP/USD  MACD(10,21,5)  ADX≥25  kill_hours=16,17,18  skip_monday=True
  USD/JPY  MACD(8,21,5)   ADX≥20  kill_hours=16,17,18  skip_monday=True
  USD/CHF  MACD(12,26,5)  ADX≥20  kill_hours=none      skip_monday=False

Setup:
  1. pip install oandapyV20 python-dotenv httpx
  2. Create free OANDA practice account at oanda.com
  3. My Services → Manage API Access → Generate token
  4. Add to .env:
       OANDA_API_KEY=your_token
       OANDA_ACCOUNT_ID=your_account_id
       TELEGRAM_BOT_TOKEN=your_bot_token
       TELEGRAM_CHAT_ID=your_chat_id

Run:
  python bot/forex_bridge.py
"""

import os
import sys
import math
import time
import json
import hmac
import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

sys.path.append(str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("forex_bridge")

# ── Config ─────────────────────────────────────────────────────────────────────
OANDA_API_KEY    = os.getenv("OANDA_API_KEY", "")
OANDA_ACCOUNT_ID = os.getenv("OANDA_ACCOUNT_ID", "")
OANDA_BASE_URL   = "https://api-fxpractice.oanda.com"  # change to api-fxtrade for live
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
PROXY            = os.getenv("PROXY", "")

LOOP_INTERVAL  = 900   # 15 minutes in seconds
TRADE_LOG      = Path("trade_log_forex.jsonl")
CANDLES_NEEDED = 120   # enough for all MACD warmup periods
RISK_PCT       = 0.01  # 1% account risk per trade
LEVERAGE       = 10    # adjust to your preference

# ── Pair configurations ────────────────────────────────────────────────────────
@dataclass
class PairConfig:
    instrument:   str
    display:      str
    macd_fast:    int
    macd_slow:    int
    macd_signal:  int
    min_hist_pct: float
    adx_level:    float
    kill_hours:   tuple
    skip_monday:  bool
    min_hold_bars: int = 16
    units:        int  = 1000   # OANDA unit size (1000 = micro lot)

PAIRS = [
    PairConfig("EUR_USD", "EUR/USD", 8,  28, 3, 0.0001, 20.0, (16,17,18), True),
    PairConfig("GBP_USD", "GBP/USD", 10, 21, 5, 0.0001, 25.0, (16,17,18), True),
    PairConfig("USD_JPY", "USD/JPY", 8,  21, 5, 0.0001, 20.0, (16,17,18), True),
    PairConfig("USD_CHF", "USD/CHF", 12, 26, 5, 0.0001, 20.0, (),          False),
]

# ── State tracking per pair ────────────────────────────────────────────────────
pair_state = {
    p.instrument: {
        "current_signal": None,
        "entry_bar":      0,
        "bar_count":      0,
    }
    for p in PAIRS
}


# ── Telegram ───────────────────────────────────────────────────────────────────

def notify(message: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        proxies = {"http://": PROXY, "https://": PROXY} if PROXY else None
        with httpx.Client(proxies=proxies, timeout=5) as client:
            client.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
            )
    except Exception:
        pass


# ── OANDA REST client ──────────────────────────────────────────────────────────

def oanda_get(endpoint: str, params: dict = None) -> dict:
    headers = {
        "Authorization": f"Bearer {OANDA_API_KEY}",
        "Content-Type":  "application/json",
    }
    try:
        r = httpx.get(
            f"{OANDA_BASE_URL}{endpoint}",
            params=params,
            headers=headers,
            timeout=15
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.error(f"OANDA GET {endpoint}: {e}")
        return {}


def oanda_post(endpoint: str, body: dict) -> dict:
    headers = {
        "Authorization": f"Bearer {OANDA_API_KEY}",
        "Content-Type":  "application/json",
    }
    try:
        r = httpx.post(
            f"{OANDA_BASE_URL}{endpoint}",
            json=body,
            headers=headers,
            timeout=15
        )
        return r.json()
    except Exception as e:
        log.error(f"OANDA POST {endpoint}: {e}")
        return {}


def oanda_put(endpoint: str, body: dict) -> dict:
    headers = {
        "Authorization": f"Bearer {OANDA_API_KEY}",
        "Content-Type":  "application/json",
    }
    try:
        r = httpx.put(
            f"{OANDA_BASE_URL}{endpoint}",
            json=body,
            headers=headers,
            timeout=15
        )
        return r.json()
    except Exception as e:
        log.error(f"OANDA PUT {endpoint}: {e}")
        return {}


# ── Account info ───────────────────────────────────────────────────────────────

def get_account() -> dict:
    resp = oanda_get(f"/v3/accounts/{OANDA_ACCOUNT_ID}/summary")
    acc  = resp.get("account", {})
    return {
        "balance": float(acc.get("balance", 0)),
        "nav":     float(acc.get("NAV", 0)),
        "pl":      float(acc.get("pl", 0)),
    }


def get_open_positions() -> dict:
    """Returns dict of instrument → position dict for open positions."""
    resp = oanda_get(f"/v3/accounts/{OANDA_ACCOUNT_ID}/openPositions")
    positions = {}
    for pos in resp.get("positions", []):
        instr = pos["instrument"]
        long_units  = float(pos["long"]["units"])
        short_units = float(pos["short"]["units"])
        if long_units > 0:
            positions[instr] = {"side": "LONG",  "units": long_units}
        elif short_units < 0:
            positions[instr] = {"side": "SHORT", "units": abs(short_units)}
    return positions


# ── Market data ────────────────────────────────────────────────────────────────

def get_candles(instrument: str, count: int = 120) -> list[dict]:
    """Fetch M15 candles from OANDA. Returns list of {t, o, h, l, c}."""
    resp = oanda_get(
        f"/v3/instruments/{instrument}/candles",
        params={
            "granularity": "M15",
            "count":       count,
            "price":       "M",  # midpoint
        }
    )
    candles = []
    for c in resp.get("candles", []):
        if c.get("complete", False):
            mid = c["mid"]
            candles.append({
                "t": int(datetime.fromisoformat(
                    c["time"].replace("Z", "+00:00")
                ).timestamp()),
                "o": float(mid["o"]),
                "h": float(mid["h"]),
                "l": float(mid["l"]),
                "c": float(mid["c"]),
            })
    return candles


# ── Indicators ─────────────────────────────────────────────────────────────────

def ema_series(prices: list[float], period: int) -> list[float]:
    k   = 2.0 / (period + 1)
    out = [float("nan")] * (period - 1)
    out.append(sum(prices[:period]) / period)
    for p in prices[period:]:
        out.append(p * k + out[-1] * (1 - k))
    return out


def macd_histogram(closes: list[float], fast: int, slow: int, signal: int) -> list[float]:
    fe   = ema_series(closes, fast)
    se   = ema_series(closes, slow)
    macd = [
        (f - s) if not (math.isnan(f) or math.isnan(s)) else float("nan")
        for f, s in zip(fe, se)
    ]
    valid_start = next((i for i, v in enumerate(macd) if not math.isnan(v)), len(macd))
    valid_macd  = [v for v in macd if not math.isnan(v)]
    sig_ema     = ema_series(valid_macd, signal) if len(valid_macd) >= signal else []
    sig_line    = [float("nan")] * len(macd)
    for i, idx in enumerate(range(valid_start, len(macd))):
        sig_line[idx] = sig_ema[i] if i < len(sig_ema) else float("nan")
    return [
        (m - s) if not (math.isnan(m) or math.isnan(s)) else float("nan")
        for m, s in zip(macd, sig_line)
    ]


def adx_series(highs: list[float], lows: list[float], closes: list[float], period: int) -> list[float]:
    n      = len(closes)
    out    = [float("nan")] * n
    trs, pdms, ndms = [], [], []
    for i in range(1, n):
        tr  = max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
        pdm = max(highs[i]-highs[i-1], 0) if (highs[i]-highs[i-1]) > (lows[i-1]-lows[i]) else 0
        ndm = max(lows[i-1]-lows[i], 0)   if (lows[i-1]-lows[i]) > (highs[i]-highs[i-1]) else 0
        trs.append(tr); pdms.append(pdm); ndms.append(ndm)

    def ws(arr, p):
        o = [float("nan")] * (p-1)
        o.append(sum(arr[:p]))
        for v in arr[p:]:
            o.append(o[-1] - o[-1]/p + v)
        return o

    atr_s = ws(trs, period); pdm_s = ws(pdms, period); ndm_s = ws(ndms, period)
    dx    = []
    for a, pm, nm in zip(atr_s, pdm_s, ndm_s):
        if math.isnan(a) or a == 0: dx.append(float("nan")); continue
        pdi = 100*pm/a; ndi = 100*nm/a
        dx.append(100*abs(pdi-ndi)/(pdi+ndi) if (pdi+ndi) else 0)

    valid = [(i,v) for i,v in enumerate(dx) if not math.isnan(v)]
    if len(valid) < period: return out
    adx_val = sum(v for _,v in valid[:period]) / period
    si = valid[period-1][0]
    if si+1 < n: out[si+1] = adx_val
    for i,v in valid[period:]:
        adx_val = (adx_val*(period-1)+v)/period
        if i+2 < n: out[i+2] = adx_val
    return out


# ── Signal generation ──────────────────────────────────────────────────────────

def get_signal(candles: list[dict], cfg: PairConfig, state: dict) -> str:
    """
    Returns 'LONG', 'SHORT', 'CLOSE', or 'HOLD'.
    Applies all filters: session, kill hours, skip monday, min hold, ADX, histogram.
    """
    if len(candles) < cfg.macd_slow + cfg.macd_signal + 10:
        return "HOLD"

    closes = [c["c"] for c in candles]
    highs  = [c["h"] for c in candles]
    lows   = [c["l"] for c in candles]

    hist = macd_histogram(closes, cfg.macd_fast, cfg.macd_slow, cfg.macd_signal)
    adx  = adx_series(highs, lows, closes, 14)

    # Use last two complete bars
    h_now  = hist[-1]; h_prev = hist[-2]
    adx_now = adx[-1]
    price   = closes[-1]

    if math.isnan(h_now) or math.isnan(h_prev) or math.isnan(adx_now):
        return "HOLD"

    last_bar = candles[-1]
    dt   = datetime.fromtimestamp(last_bar["t"], tz=timezone.utc)
    hour = dt.hour
    dow  = dt.weekday()  # 0=Monday

    # Kill hours — close and don't trade
    if cfg.kill_hours and hour in cfg.kill_hours:
        return "CLOSE"

    # Skip Monday new entries (hold existing)
    if cfg.skip_monday and dow == 0:
        return "HOLD"

    # ADX filter
    if adx_now < cfg.adx_level:
        return "HOLD"

    # Histogram filter
    if abs(h_now) < price * cfg.min_hist_pct:
        return "HOLD"

    # MACD crossover
    cross_up   = h_prev <= 0 and h_now > 0
    cross_down = h_prev >= 0 and h_now < 0

    current = state["current_signal"]
    bars_held = state["bar_count"] - state["entry_bar"]

    if cross_up and current != "LONG":
        if current is not None and bars_held < cfg.min_hold_bars:
            return "HOLD"  # too soon to flip
        return "LONG"

    if cross_down and current != "SHORT":
        if current is not None and bars_held < cfg.min_hold_bars:
            return "HOLD"
        return "SHORT"

    return "HOLD"


# ── Order execution ────────────────────────────────────────────────────────────

def calc_units(balance: float, price: float, instrument: str) -> int:
    """Calculate position size based on 1% risk and configured leverage."""
    notional = balance * RISK_PCT * LEVERAGE
    units    = int(notional / price)
    # OANDA minimum is 1 unit, round to nearest 100 for cleanliness
    return max(100, round(units / 100) * 100)


def close_position(instrument: str, side: str) -> bool:
    """Close an existing position."""
    body = {
        "longUnits":  "ALL" if side == "LONG"  else "NONE",
        "shortUnits": "ALL" if side == "SHORT" else "NONE",
    }
    resp = oanda_put(
        f"/v3/accounts/{OANDA_ACCOUNT_ID}/positions/{instrument}/close",
        body
    )
    if "relatedTransactionIDs" in resp:
        log.info(f"Closed {side} {instrument}")
        return True
    log.error(f"Close failed for {instrument}: {resp}")
    return False


def open_order(instrument: str, side: str, units: int) -> bool:
    """Open a market order."""
    signed_units = str(units) if side == "LONG" else str(-units)
    body = {
        "order": {
            "type":        "MARKET",
            "instrument":  instrument,
            "units":       signed_units,
            "timeInForce": "FOK",
        }
    }
    resp = oanda_post(f"/v3/accounts/{OANDA_ACCOUNT_ID}/orders", body)
    if resp.get("orderFillTransaction"):
        fill = resp["orderFillTransaction"]
        log.info(f"✅ {side} {units} {instrument} @ {fill.get('price', '?')}")
        return True
    log.error(f"Order failed {instrument}: {resp}")
    return False


# ── Trade log ──────────────────────────────────────────────────────────────────

def log_decision(instrument: str, signal: str, price: float, balance: float, action: str):
    entry = {
        "timestamp":  datetime.now(timezone.utc).isoformat(),
        "instrument": instrument,
        "signal":     signal,
        "price":      price,
        "balance":    balance,
        "action":     action,
    }
    with open(TRADE_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")


# ── Main loop ──────────────────────────────────────────────────────────────────

def run():
    log.info("🚀 Forex Bridge starting — 4 pairs: EUR/USD, GBP/USD, USD/JPY, USD/CHF")
    log.info(f"Account: {OANDA_ACCOUNT_ID} | Base: {OANDA_BASE_URL}")

    if not OANDA_API_KEY:
        log.error("OANDA_API_KEY not set in .env")
        log.error("1. Sign up at oanda.com → My Services → API Access")
        log.error("2. Add OANDA_API_KEY and OANDA_ACCOUNT_ID to .env")
        return

    # Verify connection
    acc = get_account()
    if not acc["balance"]:
        log.error("Could not connect to OANDA. Check your API key and account ID.")
        return

    log.info(f"Connected ✅ | Balance: ${acc['balance']:.2f} | NAV: ${acc['nav']:.2f}")
    notify(f"🚀 *Forex Bridge started*\n"
           f"Pairs: EUR/USD · GBP/USD · USD/JPY · USD/CHF\n"
           f"Balance: ${acc['balance']:.2f} | {LEVERAGE}× leverage")

    tick = 0

    while True:
        try:
            tick += 1
            now = datetime.now(timezone.utc)
            log.info(f"── Tick #{tick} @ {now.strftime('%H:%M UTC')} ──────────────")

            acc       = get_account()
            balance   = acc["balance"]
            positions = get_open_positions()

            log.info(f"Balance: ${balance:.2f} | Open positions: {list(positions.keys()) or 'none'}")

            for cfg in PAIRS:
                try:
                    candles = get_candles(cfg.instrument, CANDLES_NEEDED)
                    if not candles:
                        log.warning(f"{cfg.display}: no candles")
                        continue

                    price  = candles[-1]["c"]
                    state  = pair_state[cfg.instrument]
                    state["bar_count"] += 1

                    signal = get_signal(candles, cfg, state)
                    pos    = positions.get(cfg.instrument)

                    log.info(f"{cfg.display} @ {price:.5f} | signal={signal} | "
                             f"position={pos['side'] if pos else 'none'}")

                    action = "hold"

                    # CLOSE signal — exit any open position
                    if signal == "CLOSE":
                        if pos:
                            if close_position(cfg.instrument, pos["side"]):
                                notify(f"⚪ *{cfg.display} closed*\n"
                                       f"Reason: kill hour\n"
                                       f"Price: {price:.5f}")
                                state["current_signal"] = None
                                action = "closed"

                    # LONG signal
                    elif signal == "LONG":
                        if pos and pos["side"] == "SHORT":
                            close_position(cfg.instrument, "SHORT")
                            time.sleep(1)
                        if not pos or pos["side"] == "SHORT":
                            units = calc_units(balance, price, cfg.instrument)
                            if open_order(cfg.instrument, "LONG", units):
                                state["current_signal"] = "LONG"
                                state["entry_bar"] = state["bar_count"]
                                notify(f"🟢 *{cfg.display} LONG*\n"
                                       f"Price: {price:.5f} | Units: {units}\n"
                                       f"MACD({cfg.macd_fast},{cfg.macd_slow},{cfg.macd_signal})")
                                action = "opened_long"

                    # SHORT signal
                    elif signal == "SHORT":
                        if pos and pos["side"] == "LONG":
                            close_position(cfg.instrument, "LONG")
                            time.sleep(1)
                        if not pos or pos["side"] == "LONG":
                            units = calc_units(balance, price, cfg.instrument)
                            if open_order(cfg.instrument, "SHORT", units):
                                state["current_signal"] = "SHORT"
                                state["entry_bar"] = state["bar_count"]
                                notify(f"🔴 *{cfg.display} SHORT*\n"
                                       f"Price: {price:.5f} | Units: {units}\n"
                                       f"MACD({cfg.macd_fast},{cfg.macd_slow},{cfg.macd_signal})")
                                action = "opened_short"

                    log_decision(cfg.instrument, signal, price, balance, action)
                    time.sleep(1)  # small delay between pairs

                except Exception as e:
                    log.exception(f"{cfg.display} error: {e}")

        except KeyboardInterrupt:
            log.info("Shutting down.")
            notify("⛔ *Forex Bridge stopped*")
            break
        except Exception as e:
            log.exception(f"Loop error: {e}")

        log.info(f"Sleeping {LOOP_INTERVAL//60} minutes...")
        time.sleep(LOOP_INTERVAL)


if __name__ == "__main__":
    run()
