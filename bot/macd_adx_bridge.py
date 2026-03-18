"""
MACD/ADX Bybit Bridge — raw HTTP implementation
Bypasses pybit entirely to avoid demo trading auth issues.
Uses Bybit V5 API directly with HMAC-SHA256 signing.
"""

import os
import sys
import hmac
import hashlib
import json
import time
import logging
from datetime import datetime, timezone
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

import httpx

from backtest.macd_adx_strategy import StrategyParams, generate_signals, Bar

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("macd_adx_bridge")

# ── Config ─────────────────────────────────────────────────────────────────────
SYMBOL        = "SOLUSDT"
CATEGORY      = "linear"
LOOP_INTERVAL = 300
CANDLES       = 120
TRADE_LOG     = Path("trade_log_macd.jsonl")

API_KEY    = os.getenv("BYBIT_API_KEY", "")
API_SECRET = os.getenv("BYBIT_API_SECRET", "")
BASE_URL   = "https://api-testnet.bybit.com"  # demo trading

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

# ── Raw Bybit HTTP client ──────────────────────────────────────────────────────

def _sign(payload: str) -> tuple[str, str]:
    ts  = str(int(time.time() * 1000))
    recv_window = "5000"
    param_str = ts + API_KEY + recv_window + payload
    sig = hmac.new(
        bytes(API_SECRET, "utf-8"),
        bytes(param_str, "utf-8"),
        hashlib.sha256
    ).hexdigest()
    return ts, sig

def _headers(ts: str, sig: str) -> dict:
    return {
        "X-BAPI-API-KEY":     API_KEY,
        "X-BAPI-TIMESTAMP":   ts,
        "X-BAPI-SIGN":        sig,
        "X-BAPI-RECV-WINDOW": "5000",
        "Content-Type":       "application/json",
    }

def bybit_get(endpoint: str, params: dict) -> dict:
    query = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    ts, sig = _sign(query)
    try:
        r = httpx.get(
            f"{BASE_URL}{endpoint}",
            params=params,
            headers=_headers(ts, sig),
            timeout=10
        )
        data = r.json()
        if data.get("retCode") != 0:
            log.error(f"{endpoint}: {data.get('retMsg')}")
        return data
    except Exception as e:
        log.error(f"GET {endpoint} failed: {e}")
        return {}

def bybit_post(endpoint: str, body: dict) -> dict:
    body_str = json.dumps(body, separators=(',', ':'))
    ts, sig  = _sign(body_str)
    try:
        r = httpx.post(
            f"{BASE_URL}{endpoint}",
            content=body_str,
            headers=_headers(ts, sig),
            timeout=10
        )
        data = r.json()
        if data.get("retCode") != 0:
            log.warning(f"{endpoint}: {data.get('retMsg')}")
        return data
    except Exception as e:
        log.error(f"POST {endpoint} failed: {e}")
        return {}

# ── API calls ──────────────────────────────────────────────────────────────────

def set_leverage():
    resp = bybit_post("/v5/position/set-leverage", {
        "category":     CATEGORY,
        "symbol":       SYMBOL,
        "buyLeverage":  str(PARAMS.leverage),
        "sellLeverage": str(PARAMS.leverage),
    })
    if resp.get("retCode") == 0:
        log.info(f"Leverage set to {PARAMS.leverage}x")
    else:
        log.warning(f"Leverage: {resp.get('retMsg', 'unknown')}")


def get_balance() -> float:
    resp = bybit_get("/v5/account/wallet-balance", {"accountType": "UNIFIED"})
    try:
        return float(resp["result"]["list"][0]["totalWalletBalance"])
    except:
        log.error(f"Balance fetch failed: {resp.get('retMsg', resp)}")
        return 0.0


def get_position() -> dict:
    resp = bybit_get("/v5/position/list", {"category": CATEGORY, "symbol": SYMBOL})
    try:
        positions = [p for p in resp["result"]["list"] if float(p["size"]) > 0]
        return positions[0] if positions else {}
    except:
        log.error(f"Position fetch failed: {resp.get('retMsg', resp)}")
        return {}


def get_candles() -> list[Bar]:
    resp = httpx.get(
        f"{BASE_URL}/v5/market/kline",
        params={"category": CATEGORY, "symbol": SYMBOL, "interval": "60", "limit": CANDLES},
        timeout=10
    ).json()
    try:
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


def calc_qty(balance: float, price: float) -> float:
    notional = balance * 0.10 * PARAMS.leverage
    qty = round(notional / price, 1)
    return max(1.0, qty)


def place_order(side: str, qty: float, reduce_only: bool = False) -> bool:
    body = {
        "category":    CATEGORY,
        "symbol":      SYMBOL,
        "side":        side,
        "orderType":   "Market",
        "qty":         str(qty),
        "reduceOnly":  reduce_only,
        "timeInForce": "IOC",
    }
    resp = bybit_post("/v5/order/create", body)
    if resp.get("retCode") == 0:
        log.info(f"✅ {side} {qty} SOL | orderId={resp['result']['orderId']}")
        return True
    else:
        log.error(f"Order failed: {resp.get('retCode')} — {resp.get('retMsg')}")
        return False


def close_position(position: dict) -> bool:
    if not position:
        return True
    side = "Sell" if position["side"] == "Buy" else "Buy"
    qty  = float(position["size"])
    return place_order(side, qty, reduce_only=True)


def notify_telegram(message: str):
    token   = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return
    try:
        httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message, "parse_mode": "Markdown"},
            timeout=5
        )
    except:
        pass


def log_decision(data: dict):
    with open(TRADE_LOG, "a") as f:
        f.write(json.dumps(data) + "\n")


# ── Main loop ──────────────────────────────────────────────────────────────────

def run():
    log.info("MACD/ADX Bridge starting (raw HTTP mode)")
    log.info(f"Params: MACD({PARAMS.macd_fast},{PARAMS.macd_slow},{PARAMS.macd_signal}) "
             f"hist>={PARAMS.min_hist_pips} ADX>={PARAMS.adx_level} | {PARAMS.leverage}x")
    log.info(f"Endpoint: {BASE_URL}")

    if not API_KEY or not API_SECRET:
        log.error("BYBIT_API_KEY or BYBIT_API_SECRET not set in .env")
        return

    set_leverage()
    notify_telegram("MACD/ADX bot started\nMACD(8,21,9) ADX>=25 | 2x | Demo Trading")

    current_signal = None

    while True:
        try:
            log.info(f"── {datetime.now(timezone.utc).strftime('%H:%M UTC')} ──")

            bars = get_candles()
            if len(bars) < 60:
                log.warning(f"Only {len(bars)} bars — waiting")
                time.sleep(60)
                continue

            signals  = generate_signals(bars, PARAMS)
            position = get_position()
            balance  = get_balance()
            price    = bars[-1].close
            last_sig = signals[-1] if signals else None

            log.info(
                f"SOL @ ${price:.3f} | balance=${balance:.2f} | "
                f"position={'yes ('+position['side']+')' if position else 'none'} | "
                f"signal={last_sig.action if last_sig else 'none'}"
            )

            decision = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "price":     price,
                "signal":    last_sig.action if last_sig else "none",
                "adx":       round(last_sig.adx_value, 2) if last_sig else 0,
                "histogram": round(last_sig.hist_value, 4) if last_sig else 0,
                "position":  position.get("side", "none"),
                "balance":   balance,
            }
            log_decision(decision)

            if last_sig is None:
                log.info("No signal — holding")
                time.sleep(LOOP_INTERVAL)
                continue

            sig_action = last_sig.action

            if sig_action != current_signal:
                if sig_action == "LONG":
                    if position and position.get("side") == "Sell":
                        log.info("Flipping SHORT -> LONG")
                        close_position(position)
                    qty = calc_qty(balance, price)
                    log.info(f"Opening LONG {qty} SOL")
                    if place_order("Buy", qty):
                        current_signal = "LONG"
                        notify_telegram(
                            f"LONG opened\n"
                            f"${price:.3f} | {qty} SOL | 2x\n"
                            f"ADX={last_sig.adx_value:.1f} hist={last_sig.hist_value:.3f}"
                        )

                elif sig_action == "SHORT":
                    if position and position.get("side") == "Buy":
                        log.info("Flipping LONG -> SHORT")
                        close_position(position)
                    qty = calc_qty(balance, price)
                    log.info(f"Opening SHORT {qty} SOL")
                    if place_order("Sell", qty):
                        current_signal = "SHORT"
                        notify_telegram(
                            f"SHORT opened\n"
                            f"${price:.3f} | {qty} SOL | 2x\n"
                            f"ADX={last_sig.adx_value:.1f} hist={last_sig.hist_value:.3f}"
                        )

                elif sig_action == "CLOSE":
                    if position:
                        log.info("Closing on CLOSE signal")
                        close_position(position)
                        notify_telegram(f"Position closed @ ${price:.3f}")
                        current_signal = None
            else:
                log.info(f"Signal unchanged ({sig_action}) — no action")

        except KeyboardInterrupt:
            log.info("Shutting down.")
            break
        except Exception as e:
            log.exception(f"Loop error: {e}")

        time.sleep(LOOP_INTERVAL)


if __name__ == "__main__":
    run()
