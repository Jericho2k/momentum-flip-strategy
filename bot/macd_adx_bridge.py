"""
Multi-Pair Crypto Execution Bridge — Bybit Demo
=================================================
Runs BTC/USDT, SOL/USDT, BNB/USDT simultaneously on Bybit perpetuals.
Each pair has independent params, signals, and position management.
Checks signals every 15 minutes, sends Telegram notifications.

Validated params (backtested 2024-2026, 3× leverage):
  BTC/USDT  MACD(10,26,9) hist≥0.08% ADX≥25  Sharpe 2.197  DD -11%
  SOL/USDT  MACD(8,21,5)  hist≥0.08% ADX≥20  Sharpe 1.570  DD -55%
  BNB/USDT  MACD(10,26,3) hist≥0.01% ADX≥30  Sharpe 2.271  DD -80%

Shared filters (all pairs):
  Kill hours: 16, 17, 18 UTC
  Skip Monday entries
  Min hold bars: 16
  Regime filter: daily ADX ≥ 20

Setup:
  Add to .env:
    BYBIT_API_KEY=your_demo_api_key
    BYBIT_API_SECRET=your_demo_api_secret
    TELEGRAM_BOT_TOKEN=your_bot_token
    TELEGRAM_CHAT_ID=your_chat_id

Run:
  python bot/macd_adx_bridge.py
"""

import os
import sys
import time
import math
import json
import hmac
import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import dataclass

sys.path.append(str(Path(__file__).parent.parent))
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("crypto_bridge")

# ── Config ─────────────────────────────────────────────────────────────────────
API_KEY    = os.getenv("BYBIT_API_KEY", "")
API_SECRET = os.getenv("BYBIT_API_SECRET", "")
BASE_URL   = "https://api-demo.bybit.com"
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
PROXY            = os.getenv("PROXY", "")

LOOP_INTERVAL  = 900    # 15 minutes
CANDLES_NEEDED = 300    # enough for all MACD warmup periods
LEVERAGE       = 3
RISK_PCT       = 0.01   # 1% per trade
TRADE_LOG      = Path("trade_log_crypto.jsonl")


# ── Pair configurations ────────────────────────────────────────────────────────
@dataclass
class PairConfig:
    symbol:       str
    display:      str
    macd_fast:    int
    macd_slow:    int
    macd_signal:  int
    min_hist_pct: float
    adx_level:    float
    min_hold_bars: int = 16
    kill_hours:   tuple = (16, 17, 18)
    skip_monday:  bool = True
    regime_adx:   float = 20.0

PAIRS = [
    PairConfig("BTCUSDT", "BTC/USDT", 10, 26, 9, 0.0008, 25.0),
    PairConfig("SOLUSDT", "SOL/USDT", 8,  21, 5, 0.0008, 20.0),
    PairConfig("BNBUSDT", "BNB/USDT", 10, 26, 3, 0.0001, 30.0),
]

# Per-pair runtime state
state = {
    p.symbol: {
        "signal":    None,   # current position direction
        "entry_bar": 0,      # bar index when position was entered
        "bar_count": 0,      # total bars processed
    }
    for p in PAIRS
}


# ── Telegram ───────────────────────────────────────────────────────────────────
def notify(msg: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        proxies = {"http://": PROXY, "https://": PROXY} if PROXY else None
        with httpx.Client(proxies=proxies, timeout=5) as client:
            client.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"}
            )
    except Exception:
        pass


# ── Bybit REST client ──────────────────────────────────────────────────────────
def _sign(params: dict) -> dict:
    ts = str(int(time.time() * 1000))
    recv_window = "5000"
    query = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    raw = f"{ts}{API_KEY}{recv_window}{query}"
    sig = hmac.new(API_SECRET.encode(), raw.encode(), hashlib.sha256).hexdigest()
    return {"X-BAPI-API-KEY": API_KEY, "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-RECV-WINDOW": recv_window, "X-BAPI-SIGN": sig}


def bybit_get(endpoint: str, params: dict = None) -> dict:
    params = params or {}
    try:
        r = httpx.get(f"{BASE_URL}{endpoint}", params=params,
                      headers=_sign(params), timeout=15)
        return r.json()
    except Exception as e:
        log.error(f"GET {endpoint}: {e}")
        return {}


def bybit_post(endpoint: str, body: dict) -> dict:
    ts = str(int(time.time() * 1000))
    recv_window = "5000"
    payload = json.dumps(body)
    raw = f"{ts}{API_KEY}{recv_window}{payload}"
    sig = hmac.new(API_SECRET.encode(), raw.encode(), hashlib.sha256).hexdigest()
    headers = {
        "X-BAPI-API-KEY": API_KEY, "X-BAPI-TIMESTAMP": ts,
        "X-BAPI-RECV-WINDOW": recv_window, "X-BAPI-SIGN": sig,
        "Content-Type": "application/json"
    }
    try:
        r = httpx.post(f"{BASE_URL}{endpoint}", content=payload,
                       headers=headers, timeout=15)
        return r.json()
    except Exception as e:
        log.error(f"POST {endpoint}: {e}")
        return {}


# ── Market data ────────────────────────────────────────────────────────────────
def get_candles(symbol: str, interval: str = "15", count: int = 300) -> list[dict]:
    resp = bybit_get("/v5/market/kline", {
        "category": "linear", "symbol": symbol,
        "interval": interval, "limit": str(count)
    })
    bars = []
    for c in reversed(resp.get("result", {}).get("list", [])):
        bars.append({
            "t": int(c[0]) // 1000,
            "o": float(c[1]), "h": float(c[2]),
            "l": float(c[3]), "c": float(c[4]),
        })
    return bars


def get_account_balance() -> float:
    resp = bybit_get("/v5/account/wallet-balance", {"accountType": "UNIFIED"})
    try:
        coins = resp["result"]["list"][0]["coin"]
        usdt  = next((c for c in coins if c["coin"] == "USDT"), {})
        return float(usdt.get("walletBalance", 0))
    except Exception:
        return 0.0


def get_position(symbol: str) -> dict | None:
    resp = bybit_get("/v5/position/list", {
        "category": "linear", "symbol": symbol
    })
    for pos in resp.get("result", {}).get("list", []):
        size = float(pos.get("size", 0))
        if size > 0:
            return {
                "side": pos["side"],   # "Buy" or "Sell"
                "size": size,
                "entry_price": float(pos.get("avgPrice", 0)),
            }
    return None


def set_leverage(symbol: str, leverage: int):
    bybit_post("/v5/position/set-leverage", {
        "category": "linear", "symbol": symbol,
        "buyLeverage": str(leverage), "sellLeverage": str(leverage)
    })


# ── Indicators ─────────────────────────────────────────────────────────────────
def ema(prices: list[float], period: int) -> list[float]:
    k   = 2.0 / (period + 1)
    out = [float("nan")] * (period - 1)
    out.append(sum(prices[:period]) / period)
    for p in prices[period:]:
        out.append(p * k + out[-1] * (1 - k))
    return out


def macd_hist(closes: list[float], fast: int, slow: int, signal: int) -> list[float]:
    fe = ema(closes, fast)
    se = ema(closes, slow)
    macd = [(f-s) if not (math.isnan(f) or math.isnan(s)) else float("nan")
            for f, s in zip(fe, se)]
    valid = [v for v in macd if not math.isnan(v)]
    vs    = ema(valid, signal) if len(valid) >= signal else []
    sig   = [float("nan")] * len(macd)
    vi    = next((i for i, v in enumerate(macd) if not math.isnan(v)), len(macd))
    for i, v in enumerate(vs):
        if vi + i < len(sig):
            sig[vi + i] = v
    return [(m-s) if not (math.isnan(m) or math.isnan(s)) else float("nan")
            for m, s in zip(macd, sig)]


def adx_val(highs: list[float], lows: list[float],
            closes: list[float], period: int) -> float:
    n = len(closes)
    if n < period * 3:
        return float("nan")
    trs, pdms, ndms = [], [], []
    for i in range(1, n):
        tr  = max(highs[i]-lows[i],
                  abs(highs[i]-closes[i-1]),
                  abs(lows[i]-closes[i-1]))
        pdm = max(highs[i]-highs[i-1], 0) \
              if (highs[i]-highs[i-1]) > (lows[i-1]-lows[i]) else 0
        ndm = max(lows[i-1]-lows[i], 0) \
              if (lows[i-1]-lows[i]) > (highs[i]-highs[i-1]) else 0
        trs.append(tr); pdms.append(pdm); ndms.append(ndm)

    def ws(arr):
        o = [sum(arr[:period])]
        for v in arr[period:]:
            o.append(o[-1] - o[-1]/period + v)
        return o

    atr_s = ws(trs); pdm_s = ws(pdms); ndm_s = ws(ndms)
    dx_vals = []
    for a, pm, nm in zip(atr_s, pdm_s, ndm_s):
        if a == 0:
            continue
        pdi = 100*pm/a; ndi = 100*nm/a
        dx_vals.append(100*abs(pdi-ndi)/(pdi+ndi) if (pdi+ndi) else 0)

    if len(dx_vals) < period:
        return float("nan")
    adx = sum(dx_vals[:period]) / period
    for v in dx_vals[period:]:
        adx = (adx * (period-1) + v) / period
    return adx


# ── Regime filter ──────────────────────────────────────────────────────────────
def regime_ok(symbol: str, cfg: PairConfig) -> bool:
    daily = get_candles(symbol, interval="D", count=60)
    if len(daily) < 30:
        return True
    h = [c["h"] for c in daily]
    l = [c["l"] for c in daily]
    c = [c["c"] for c in daily]
    adx = adx_val(h, l, c, 14)
    if math.isnan(adx):
        return True
    ok = adx >= cfg.regime_adx
    log.info(f"  {symbol} daily ADX={adx:.1f} regime={'OK' if ok else 'BLOCKED'}")
    return ok


# ── Signal logic ───────────────────────────────────────────────────────────────
def get_signal(bars: list[dict], cfg: PairConfig, s: dict) -> str:
    """Returns LONG, SHORT, CLOSE, or HOLD."""
    if len(bars) < cfg.macd_slow + cfg.macd_signal + 20:
        return "HOLD"

    closes = [b["c"] for b in bars]
    highs  = [b["h"] for b in bars]
    lows   = [b["l"] for b in bars]

    hist   = macd_hist(closes, cfg.macd_fast, cfg.macd_slow, cfg.macd_signal)
    adx    = adx_val(highs, lows, closes, 14)
    h_now  = hist[-1]
    h_prev = hist[-2]
    price  = closes[-1]

    if math.isnan(h_now) or math.isnan(h_prev) or math.isnan(adx):
        return "HOLD"

    dt   = datetime.fromtimestamp(bars[-1]["t"], tz=timezone.utc)
    hour = dt.hour
    dow  = dt.weekday()  # 0=Monday

    # Kill hours
    if hour in cfg.kill_hours:
        return "CLOSE"

    # Skip Monday new entries
    if cfg.skip_monday and dow == 0:
        return "HOLD"

    # ADX filter
    if adx < cfg.adx_level:
        return "HOLD"

    # Histogram filter
    if abs(h_now) < price * cfg.min_hist_pct:
        return "HOLD"

    # Crossover
    cross_up   = h_prev <= 0 and h_now > 0
    cross_down = h_prev >= 0 and h_now < 0

    current   = s["signal"]
    bars_held = s["bar_count"] - s["entry_bar"]

    if cross_up and current != "LONG":
        if current is not None and bars_held < cfg.min_hold_bars:
            return "HOLD"
        return "LONG"

    if cross_down and current != "SHORT":
        if current is not None and bars_held < cfg.min_hold_bars:
            return "HOLD"
        return "SHORT"

    return "HOLD"


# ── Order execution ────────────────────────────────────────────────────────────
def calc_qty(balance: float, price: float, symbol: str) -> float:
    """Calculate position size. Returns quantity in base currency."""
    notional = balance * RISK_PCT * LEVERAGE
    qty      = notional / price
    # Round to Bybit's minimum qty increments
    if "BTC" in symbol:
        return round(qty, 3)
    elif "BNB" in symbol:
        return round(qty, 2)
    else:
        return round(qty, 1)


def place_order(symbol: str, side: str, qty: float) -> bool:
    """Place a market order. side = 'Buy' or 'Sell'."""
    resp = bybit_post("/v5/order/create", {
        "category":    "linear",
        "symbol":      symbol,
        "side":        side,
        "orderType":   "Market",
        "qty":         str(qty),
        "timeInForce": "IOC",
    })
    ret = resp.get("retCode", -1)
    if ret == 0:
        log.info(f"✅ {side} {qty} {symbol}")
        return True
    log.error(f"Order failed {symbol}: {resp.get('retMsg')} (code {ret})")
    return False


def close_position(symbol: str, pos: dict) -> bool:
    """Close an existing position."""
    side = "Sell" if pos["side"] == "Buy" else "Buy"
    resp = bybit_post("/v5/order/create", {
        "category":    "linear",
        "symbol":      symbol,
        "side":        side,
        "orderType":   "Market",
        "qty":         str(pos["size"]),
        "timeInForce": "IOC",
        "reduceOnly":  True,
    })
    ret = resp.get("retCode", -1)
    if ret == 0:
        log.info(f"✅ Closed {pos['side']} {symbol}")
        return True
    log.error(f"Close failed {symbol}: {resp.get('retMsg')}")
    return False


# ── Trade log ──────────────────────────────────────────────────────────────────
def log_trade(symbol: str, signal: str, price: float,
              balance: float, action: str):
    entry = {
        "ts":      datetime.now(timezone.utc).isoformat(),
        "symbol":  symbol,
        "signal":  signal,
        "price":   price,
        "balance": balance,
        "action":  action,
    }
    with open(TRADE_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")


# ── Main loop ──────────────────────────────────────────────────────────────────
def run():
    log.info("🚀 Crypto Bridge starting")
    log.info(f"Pairs: {', '.join(p.display for p in PAIRS)}")
    log.info(f"Leverage: {LEVERAGE}×  |  Risk per trade: {RISK_PCT*100}%")

    if not API_KEY:
        log.error("BYBIT_API_KEY not set in .env")
        return

    # Set leverage for all pairs
    for cfg in PAIRS:
        set_leverage(cfg.symbol, LEVERAGE)
        log.info(f"Set {LEVERAGE}× leverage on {cfg.display}")
        time.sleep(0.5)

    balance = get_account_balance()
    log.info(f"Balance: ${balance:.2f} USDT")
    notify(f"🚀 *Crypto Bridge started*\n"
           f"Pairs: BTC · SOL · BNB\n"
           f"Leverage: {LEVERAGE}× | Balance: ${balance:.2f}")

    tick = 0

    while True:
        # Check for pause signal from Telegram
        if Path(".bridge_paused").exists():
            log.info("⏸ Bridge paused via Telegram — skipping tick")
            time.sleep(LOOP_INTERVAL)
            continue

        try:
            tick += 1
            now = datetime.now(timezone.utc)
            log.info(f"\n── Tick #{tick} @ {now.strftime('%Y-%m-%d %H:%M UTC')} ──")

            balance = get_account_balance()
            log.info(f"Balance: ${balance:.2f} USDT")

            for cfg in PAIRS:
                try:
                    s = state[cfg.symbol]
                    s["bar_count"] += 1

                    bars = get_candles(cfg.symbol, count=CANDLES_NEEDED)
                    if not bars:
                        log.warning(f"{cfg.display}: no candles")
                        continue

                    price  = bars[-1]["c"]
                    signal = get_signal(bars, cfg, s)
                    pos    = get_position(cfg.symbol)
                    pos_side = pos["side"] if pos else "none"

                    log.info(f"{cfg.display} @ ${price:,.2f} | "
                             f"signal={signal} | pos={pos_side}")

                    action = "hold"

                    # CLOSE — kill hour triggered
                    if signal == "CLOSE":
                        if pos:
                            if close_position(cfg.symbol, pos):
                                pnl_est = ""
                                if pos["entry_price"]:
                                    mult = 1 if pos["side"]=="Buy" else -1
                                    pct  = mult*(price-pos["entry_price"])/pos["entry_price"]*100*LEVERAGE
                                    pnl_est = f"\nEst. PnL: {pct:+.2f}%"
                                notify(f"⚪ *{cfg.display} closed*\n"
                                       f"Reason: kill hour {now.hour}:00 UTC\n"
                                       f"Price: ${price:,.2f}{pnl_est}")
                                s["signal"] = None
                                action = "closed"

                    # LONG signal
                    elif signal == "LONG":
                        # Check regime filter
                        if not regime_ok(cfg.symbol, cfg):
                            notify(f"🚫 *{cfg.display}* LONG signal blocked by regime filter")
                            action = "regime_blocked"
                        else:
                            # Close existing short first
                            if pos and pos["side"] == "Sell":
                                close_position(cfg.symbol, pos)
                                time.sleep(1)
                                pos = None

                            if not pos:
                                qty = calc_qty(balance, price, cfg.symbol)
                                if place_order(cfg.symbol, "Buy", qty):
                                    s["signal"]    = "LONG"
                                    s["entry_bar"] = s["bar_count"]
                                    notify(f"🟢 *{cfg.display} LONG*\n"
                                           f"Price: ${price:,.2f} | Qty: {qty}\n"
                                           f"MACD({cfg.macd_fast},{cfg.macd_slow},"
                                           f"{cfg.macd_signal}) ADX≥{cfg.adx_level:.0f}")
                                    action = "opened_long"

                    # SHORT signal
                    elif signal == "SHORT":
                        if not regime_ok(cfg.symbol, cfg):
                            notify(f"🚫 *{cfg.display}* SHORT signal blocked by regime filter")
                            action = "regime_blocked"
                        else:
                            # Close existing long first
                            if pos and pos["side"] == "Buy":
                                close_position(cfg.symbol, pos)
                                time.sleep(1)
                                pos = None

                            if not pos:
                                qty = calc_qty(balance, price, cfg.symbol)
                                if place_order(cfg.symbol, "Sell", qty):
                                    s["signal"]    = "SHORT"
                                    s["entry_bar"] = s["bar_count"]
                                    notify(f"🔴 *{cfg.display} SHORT*\n"
                                           f"Price: ${price:,.2f} | Qty: {qty}\n"
                                           f"MACD({cfg.macd_fast},{cfg.macd_slow},"
                                           f"{cfg.macd_signal}) ADX≥{cfg.adx_level:.0f}")
                                    action = "opened_short"

                    log_trade(cfg.symbol, signal, price, balance, action)
                    time.sleep(1)

                except Exception as e:
                    log.exception(f"{cfg.display} error: {e}")

        except KeyboardInterrupt:
            log.info("Shutting down gracefully.")
            notify("⛔ *Crypto Bridge stopped*")
            break
        except Exception as e:
            log.exception(f"Loop error: {e}")

        log.info(f"Sleeping {LOOP_INTERVAL//60} min until next tick...")
        time.sleep(LOOP_INTERVAL)


if __name__ == "__main__":
    run()
