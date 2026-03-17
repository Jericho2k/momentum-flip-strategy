"""
MACD/ADX Bybit Bridge
======================
Executes the validated MACD/ADX flip strategy on Bybit linear perpetuals.
Production params: MACD(8,21,9) hist>=0.1 ADX(14)>=25 | 2x leverage | no SL

Run alongside openclaw_agent.py in a second terminal for oversight.

Setup:
    pip install -r requirements.txt
    cp .env.example .env  # fill in your keys
    python bot/macd_adx_bridge.py
"""

import os
import sys
import time
import logging
from datetime import datetime, timezone
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

try:
    from pybit.unified_trading import HTTP
    PYBIT_AVAILABLE = True
except ImportError:
    PYBIT_AVAILABLE = False
    print("pybit not installed — running in SIMULATION mode")

from backtest.macd_adx_strategy import StrategyParams, generate_signals, Bar
from backtest.backtester import fetch_bybit_ohlcv

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("macd_adx_bridge")

# ── Config ─────────────────────────────────────────────────────────────────────
SYMBOL        = "SOLUSDT"
CATEGORY      = "linear"
LOOP_INTERVAL = 300          # check every 5 minutes
CANDLES       = 120          # enough for warmup + signal
TESTNET       = True         # set False only when ready for real money
TRADE_LOG     = Path("trade_log_macd.jsonl")

API_KEY    = os.getenv("BYBIT_API_KEY", "")
API_SECRET = os.getenv("BYBIT_API_SECRET", "")

# Production params — validated via backtester
PARAMS = StrategyParams(
    macd_fast     = 8,
    macd_slow     = 21,
    macd_signal   = 9,
    min_hist_pips = 0.1,
    adx_period    = 14,
    adx_level     = 25.0,
    session_start = None,
    session_end   = None,
    morning_stop  = False,
    sl_pips       = 0.0,
    leverage      = 2,
)

# ── Bybit client ───────────────────────────────────────────────────────────────

def get_client():
    if not PYBIT_AVAILABLE or not API_KEY:
        return None
    return HTTP(
        testnet=False,
        demo=True,
        api_key=API_KEY,
        api_secret=API_SECRET,
    )


def set_leverage(client):
    try:
        client.set_leverage(
            category=CATEGORY, symbol=SYMBOL,
            buyLeverage=str(PARAMS.leverage),
            sellLeverage=str(PARAMS.leverage),
        )
        log.info(f"Leverage set to {PARAMS.leverage}x")
    except Exception as e:
        log.warning(f"Leverage already set or error: {e}")


def get_candles(client) -> list[Bar]:
    if client is None:
        return fetch_bybit_ohlcv(SYMBOL, "60", days=10)
    try:
        resp = client.get_kline(
            category=CATEGORY, symbol=SYMBOL,
            interval="60", limit=CANDLES
        )
        raw = list(reversed(resp["result"]["list"]))
        return [
            Bar(
                timestamp = int(c[0]) // 1000,
                open      = float(c[1]),
                high      = float(c[2]),
                low       = float(c[3]),
                close     = float(c[4]),
                volume    = float(c[5]),
            )
            for c in raw
        ]
    except Exception as e:
        log.error(f"Candle fetch failed: {e}")
        return []


def get_position(client) -> dict:
    """Returns current open position or None."""
    if client is None:
        return {}
    try:
        resp = client.get_positions(category=CATEGORY, symbol=SYMBOL)
        positions = [p for p in resp["result"]["list"] if float(p["size"]) > 0]
        return positions[0] if positions else {}
    except Exception as e:
        log.error(f"Position fetch failed: {e}")
        return {}


def get_balance(client) -> float:
    if client is None:
        return 500.0
    try:
        resp = client.get_wallet_balance(accountType="UNIFIED")
        return float(resp["result"]["list"][0]["totalWalletBalance"])
    except Exception as e:
        log.error(f"Balance fetch failed: {e}")
        return 0.0


def calc_qty(balance: float, price: float) -> float:
    """Size position at 10% of balance notional at given leverage."""
    notional = balance * 0.10 * PARAMS.leverage
    qty = round(notional / price, 1)
    return max(1.0, qty)


def place_order(client, side: str, qty: float, price: float) -> bool:
    if client is None:
        log.info(f"[SIM] {side} {qty} SOL @ ~${price:.3f} | {PARAMS.leverage}x leverage")
        return True
    try:
        resp = client.place_order(
            category    = CATEGORY,
            symbol      = SYMBOL,
            side        = side,
            orderType   = "Market",
            qty         = str(qty),
            reduceOnly  = False,
            timeInForce = "IOC",
        )
        if resp["retCode"] == 0:
            log.info(f"✅ {side} {qty} SOL @ ~${price:.3f} | orderId={resp['result']['orderId']}")
            return True
        else:
            log.error(f"Order failed: {resp['retCode']} — {resp['retMsg']}")
            return False
    except Exception as e:
        log.error(f"Order exception: {e}")
        return False


def close_position(client, position: dict, price: float) -> bool:
    """Close existing position by placing a reduce-only opposite order."""
    if not position:
        return True
    side = "Sell" if position["side"] == "Buy" else "Buy"
    qty  = float(position["size"])
    if client is None:
        log.info(f"[SIM] Close {position['side']} {qty} SOL @ ~${price:.3f}")
        return True
    try:
        resp = client.place_order(
            category    = CATEGORY,
            symbol      = SYMBOL,
            side        = side,
            orderType   = "Market",
            qty         = str(qty),
            reduceOnly  = True,
            timeInForce = "IOC",
        )
        return resp["retCode"] == 0
    except Exception as e:
        log.error(f"Close position failed: {e}")
        return False


def log_decision(data: dict):
    import json
    with open(TRADE_LOG, "a") as f:
        f.write(json.dumps(data) + "\n")


# ── Main loop ──────────────────────────────────────────────────────────────────

def run():
    log.info("MACD/ADX Bridge starting...")
    log.info(f"Params: MACD({PARAMS.macd_fast},{PARAMS.macd_slow},{PARAMS.macd_signal}) "
             f"hist>={PARAMS.min_hist_pips} ADX>={PARAMS.adx_level} | {PARAMS.leverage}x")

    client = get_client()
    if client:
        set_leverage(client)
        mode = "TESTNET" if TESTNET else "LIVE"
        log.info(f"Connected to Bybit ({mode})")
    else:
        log.info("No API keys — simulation mode")

    current_signal = None  # track what signal we last acted on

    while True:
        try:
            log.info(f"── {datetime.now(timezone.utc).strftime('%H:%M UTC')} ──")

            bars = get_candles(client)
            if len(bars) < PARAMS.macd_slow + PARAMS.macd_signal + PARAMS.adx_period + 10:
                log.warning("Not enough bars yet")
                time.sleep(60)
                continue

            signals    = generate_signals(bars, PARAMS)
            position   = get_position(client)
            balance    = get_balance(client)
            price      = bars[-1].close
            last_sig   = signals[-1] if signals else None

            log.info(f"SOL @ ${price:.3f} | balance=${balance:.2f} | "
                     f"position={'yes' if position else 'none'} | "
                     f"last_signal={last_sig.action if last_sig else 'none'}")

            if last_sig is None:
                log.info("No signal generated — holding")
                time.sleep(LOOP_INTERVAL)
                continue

            sig_action = last_sig.action  # 'LONG', 'SHORT', 'CLOSE'
            qty        = calc_qty(balance, price)

            decision = {
                "timestamp":   datetime.now(timezone.utc).isoformat(),
                "price":       price,
                "signal":      sig_action,
                "adx":         round(last_sig.adx_value, 2),
                "histogram":   round(last_sig.hist_value, 4),
                "position":    position.get("side", "none"),
                "qty":         qty,
                "balance":     balance,
            }

            # Only act if signal changed since last loop
            if sig_action != current_signal:
                if sig_action == "LONG":
                    if position and position.get("side") == "Sell":
                        log.info("Flipping SHORT -> LONG")
                        close_position(client, position, price)
                    log.info(f"Opening LONG {qty} SOL")
                    if place_order(client, "Buy", qty, price):
                        current_signal = "LONG"

                elif sig_action == "SHORT":
                    if position and position.get("side") == "Buy":
                        log.info("Flipping LONG -> SHORT")
                        close_position(client, position, price)
                    log.info(f"Opening SHORT {qty} SOL")
                    if place_order(client, "Sell", qty, price):
                        current_signal = "SHORT"

                elif sig_action == "CLOSE":
                    if position:
                        log.info("Closing position on CLOSE signal")
                        close_position(client, position, price)
                        current_signal = None
            else:
                log.info(f"Signal unchanged ({sig_action}) — no action")

            log_decision(decision)

        except KeyboardInterrupt:
            log.info("Shutting down.")
            break
        except Exception as e:
            log.exception(f"Loop error: {e}")

        time.sleep(LOOP_INTERVAL)


if __name__ == "__main__":
    run()
