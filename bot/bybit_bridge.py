"""
Bybit Bridge — SOL/USDT Linear Perpetual Futures
Connects the OpenClaw SOL skill to Bybit via pybit.

Setup:
    pip install pybit anthropic requests

Get your Bybit API key:
    Bybit → Account → API Management → Create New Key
    Enable: "Contract" (read + trade), IP whitelist recommended.

    Start on TESTNET: https://testnet.bybit.com
    Testnet base URL: https://api-testnet.bybit.com
"""

import os
import time
import logging
from datetime import datetime, timezone
from typing import Optional
from dotenv import load_dotenv
load_dotenv()

try:
    from pybit.unified_trading import HTTP
    PYBIT_AVAILABLE = True
except ImportError:
    PYBIT_AVAILABLE = False
    print("⚠ pybit not installed. pip install pybit — running in SIMULATION mode.")

import requests
from sol_skill import run_skill
from pathlib import Path

TRADE_LOG = Path("trade_log.jsonl")

def log_decision(decision: dict):
    with open(TRADE_LOG, "a") as f:
        f.write(json.dumps(decision) + "\n")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("bybit_bridge")

# ── Config ─────────────────────────────────────────────────────────────────────
SYMBOL         = "SOLUSDT"
CATEGORY       = "linear"          # linear = USDT-margined perpetual
LEVERAGE       = 3
CANDLE_LIMIT   = 60               # 60 H1 candles
LOOP_INTERVAL  = 300              # 5 minutes

# Keys loaded from .env
API_KEY        = os.getenv("BYBIT_API_KEY", "")
API_SECRET     = os.getenv("BYBIT_API_SECRET", "")
TESTNET        = True             # ← set False only when ready for real trading

# News
NEWS_API_KEY   = os.getenv("NEWS_API_KEY", "")
NEWS_QUERY     = "Solana SOL crypto blockchain"


# ── Bybit Client ───────────────────────────────────────────────────────────────

def get_client() -> Optional[object]:
    if not PYBIT_AVAILABLE:
        return None
    return HTTP(
        testnet=TESTNET,
        api_key=API_KEY,
        api_secret=API_SECRET,
    )


def set_leverage(client, symbol: str, leverage: int):
    try:
        client.set_leverage(
            category=CATEGORY,
            symbol=symbol,
            buyLeverage=str(leverage),
            sellLeverage=str(leverage),
        )
        log.info(f"Leverage set to {leverage}×")
    except Exception as e:
        log.warning(f"Leverage set skipped (may already be set): {e}")


# ── Market Data ────────────────────────────────────────────────────────────────

def get_candles(client, symbol: str, limit: int) -> Optional[dict]:
    if client is None:
        return _sim_candles(limit)
    try:
        resp = client.get_kline(
            category=CATEGORY,
            symbol=symbol,
            interval="60",      # H1
            limit=limit,
        )
        raw = resp["result"]["list"]
        # Bybit returns newest first — reverse to chronological
        raw = list(reversed(raw))
        closes = [float(c[4]) for c in raw]
        highs  = [float(c[2]) for c in raw]
        lows   = [float(c[3]) for c in raw]
        return {"closes": closes, "highs": highs, "lows": lows, "current_price": closes[-1]}
    except Exception as e:
        log.error(f"Candle fetch failed: {e}")
        return None


def get_account(client) -> dict:
    if client is None:
        return {"balance": 500.0, "equity": 500.0, "open_positions": []}
    try:
        wallet = client.get_wallet_balance(accountType="UNIFIED")
        info   = wallet["result"]["list"][0]
        balance = float(info["totalWalletBalance"])
        equity  = float(info["totalEquity"])

        pos_resp  = client.get_positions(category=CATEGORY, symbol=SYMBOL)
        positions = [p for p in pos_resp["result"]["list"] if float(p["size"]) > 0]
        return {"balance": balance, "equity": equity, "open_positions": positions}
    except Exception as e:
        log.error(f"Account fetch failed: {e}")
        return {"balance": 0, "equity": 0, "open_positions": []}


# ── Order Execution ────────────────────────────────────────────────────────────

def place_order(client, signal: str, position: dict, current_price: float) -> bool:
    if client is None:
        log.info(f"[SIM] {signal} {position['qty']} SOL @ ~{current_price:.3f} "
                 f"| SL={position['sl']} TP={position['tp']} | {LEVERAGE}× leverage "
                 f"| margin≈${position.get('margin_used',0):.2f} risk=${position.get('risk_usdt',0):.2f}")
        return True

    side = "Buy" if signal == "BUY" else "Sell"
    try:
        resp = client.place_order(
            category=CATEGORY,
            symbol=SYMBOL,
            side=side,
            orderType="Market",
            qty=str(position["qty"]),
            stopLoss=str(position["sl"]),
            takeProfit=str(position["tp"]),
            slTriggerBy="MarkPrice",
            tpTriggerBy="MarkPrice",
            reduceOnly=False,
            timeInForce="IOC",
        )
        if resp["retCode"] == 0:
            log.info(f"✅ Order placed: {side} {position['qty']} SOL | orderId={resp['result']['orderId']}")
            return True
        else:
            log.error(f"Order rejected: {resp['retCode']} — {resp['retMsg']}")
            return False
    except Exception as e:
        log.error(f"Order exception: {e}")
        return False


def manage_trailing(client, position: dict, current_price: float):
    """Activate trailing stop once price reaches trigger level."""
    if client is None or not position.get("qty"):
        return

    try:
        pos_resp  = client.get_positions(category=CATEGORY, symbol=SYMBOL)
        positions = [p for p in pos_resp["result"]["list"] if float(p["size"]) > 0]
        if not positions:
            return

        pos  = positions[0]
        side = pos["side"]           # "Buy" or "Sell"
        trail_act  = position.get("trailing_activation", 0)
        trail_dist = position.get("trailing_distance", 0)

        if not trail_act or not trail_dist:
            return

        activated = (
            (side == "Buy"  and current_price >= trail_act) or
            (side == "Sell" and current_price <= trail_act)
        )
        if not activated:
            return

        # Bybit supports native trailing stop via set_trading_stop
        resp = client.set_trading_stop(
            category=CATEGORY,
            symbol=SYMBOL,
            trailingStop=str(round(trail_dist, 3)),
            activePrice=str(round(trail_act, 3)),
            positionIdx=0,
        )
        if resp["retCode"] == 0:
            log.info(f"🔄 Trailing stop activated | dist={trail_dist:.3f} | activation={trail_act:.3f}")
        else:
            log.warning(f"Trailing stop set failed: {resp['retMsg']}")
    except Exception as e:
        log.error(f"Trailing stop error: {e}")


# ── News ───────────────────────────────────────────────────────────────────────

def fetch_headlines(n: int = 8) -> list[str]:
    if NEWS_API_KEY == "YOUR_NEWSAPI_KEY":
        log.warning("No NewsAPI key — using placeholder headlines")
        return [
            "Solana ecosystem TVL reaches new record as DeFi activity surges",
            "Bitcoin holds above key support, altcoins show strength",
        ]
    try:
        url = (f"https://newsapi.org/v2/everything?q={requests.utils.quote(NEWS_QUERY)}"
               f"&sortBy=publishedAt&pageSize={n}&apiKey={NEWS_API_KEY}")
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        return [a["title"] for a in r.json().get("articles", []) if a.get("title")]
    except Exception as e:
        log.error(f"News fetch error: {e}")
        return []


# ── Simulation ─────────────────────────────────────────────────────────────────

def _sim_candles(n: int) -> dict:
    import random
    random.seed(int(time.time()) // 300)
    base   = 138.0
    closes = [base]
    for _ in range(n - 1):
        closes.append(closes[-1] + random.uniform(-3, 3))
    highs = [c + random.uniform(0.5, 2) for c in closes]
    lows  = [c - random.uniform(0.5, 2) for c in closes]
    return {"closes": closes, "highs": highs, "lows": lows, "current_price": closes[-1]}


# ── Main Loop ──────────────────────────────────────────────────────────────────

last_position = {}

def run():
    global last_position
    log.info("🚀 Bybit SOL/USDT bridge starting...")

    client = get_client()
    if client:
        set_leverage(client, SYMBOL, LEVERAGE)
        mode = "TESTNET" if TESTNET else "LIVE"
        log.info(f"Connected to Bybit ({mode})")
    else:
        log.info("Simulation mode — no real orders")

    while True:
        try:
            log.info(f"── {datetime.now(timezone.utc).strftime('%H:%M UTC')} ──────────")

            market   = get_candles(client, SYMBOL, CANDLE_LIMIT)
            if not market:
                time.sleep(60)
                continue

            headlines = fetch_headlines()
            account   = get_account(client)
            log.info(f"Balance: ${account['balance']:.2f} | Positions: {len(account['open_positions'])}")

            decision = run_skill(market, headlines, account)
            last_position = decision.get("position", {})
            log_decision(decision)

            sig = decision["fused"]["signal"]
            log.info(f"Signal: {sig} | strength={decision['fused']['strength']:.2f} | score={decision['fused']['score']:.3f}")
            log.info(f"Sentiment: {decision['sentiment']['summary']}")

            if decision["execute"]:
                place_order(client, sig, decision["position"], market["current_price"])

            if account["open_positions"] and last_position:
                manage_trailing(client, last_position, market["current_price"])

        except KeyboardInterrupt:
            log.info("Shutting down.")
            break
        except Exception as e:
            log.exception(f"Loop error: {e}")

        time.sleep(LOOP_INTERVAL)


if __name__ == "__main__":
    run()
