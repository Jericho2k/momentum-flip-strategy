"""
OpenClaw Skill: SOL/USDT Hybrid Futures Strategy
Bybit Linear Perpetuals · H1 timeframe · 2–5× leverage
Combines EMA/RSI/ATR technicals with Claude news sentiment.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Optional
import anthropic

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("sol_skill")

# ── Config ─────────────────────────────────────────────────────────────────────
SYMBOL          = "SOLUSDT"
TIMEFRAME       = "60"          # Bybit interval string for H1
LEVERAGE        = 3             # 3× — conservative within 2–5× range
RISK_PER_TRADE  = 0.01          # 1% account risk per trade

ATR_SL_MULT     = 1.5
ATR_TP_MULT     = 2.5
TRAIL_ACTIVATION = 0.60         # activate trailing at 60% of TP
TRAIL_DISTANCE   = 0.40         # trail at 40% of initial SL

TECH_WEIGHT      = 0.60
SENT_WEIGHT      = 0.40         # slightly more sentiment weight vs gold (crypto is news-driven)
SIGNAL_THRESHOLD = 0.22

# SOL-specific: min order qty and contract size
MIN_QTY          = 1.0          # minimum 1 SOL contract on Bybit
QTY_STEP         = 1.0


# ── Technical Indicators ───────────────────────────────────────────────────────

def ema(prices: list[float], period: int) -> list[float]:
    k = 2 / (period + 1)
    out = [prices[0]]
    for p in prices[1:]:
        out.append(p * k + out[-1] * (1 - k))
    return out


def rsi(prices: list[float], period: int = 14) -> float:
    deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
    gains  = [max(d, 0) for d in deltas[-period:]]
    losses = [abs(min(d, 0)) for d in deltas[-period:]]
    ag = sum(gains) / period
    al = sum(losses) / period
    if al == 0:
        return 100.0
    return 100 - (100 / (1 + ag / al))


def atr(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> float:
    trs = [
        max(highs[i] - lows[i],
            abs(highs[i] - closes[i-1]),
            abs(lows[i]  - closes[i-1]))
        for i in range(1, len(closes))
    ]
    return sum(trs[-period:]) / period


def compute_technical_signal(closes: list[float], highs: list[float], lows: list[float]) -> dict:
    fast = ema(closes, 9)
    slow = ema(closes, 21)
    rsi_val = rsi(closes)
    atr_val = atr(highs, lows, closes)

    # Volume proxy: price range expansion signals momentum
    recent_range = highs[-1] - lows[-1]
    avg_range    = sum(h - l for h, l in zip(highs[-10:], lows[-10:])) / 10
    momentum_ok  = recent_range > avg_range * 0.9

    cross_up   = fast[-2] <= slow[-2] and fast[-1] > slow[-1]
    cross_down = fast[-2] >= slow[-2] and fast[-1] < slow[-1]
    trend_up   = fast[-1] > slow[-1]
    trend_down = fast[-1] < slow[-1]

    signal   = "FLAT"
    strength = 0.0

    if cross_up or (trend_up and 40 < rsi_val < 68 and momentum_ok):
        signal   = "BUY"
        strength = 0.88 if cross_up else 0.55
        if rsi_val < 35:
            strength = min(strength + 0.10, 1.0)

    elif cross_down or (trend_down and 32 < rsi_val < 60 and momentum_ok):
        signal   = "SELL"
        strength = 0.88 if cross_down else 0.55
        if rsi_val > 65:
            strength = min(strength + 0.10, 1.0)

    return {
        "signal":    signal,
        "strength":  round(strength, 3),
        "atr_value": round(atr_val, 4),
        "details": {
            "ema_fast":    round(fast[-1], 3),
            "ema_slow":    round(slow[-1], 3),
            "rsi":         round(rsi_val, 2),
            "atr":         round(atr_val, 4),
            "momentum_ok": momentum_ok,
            "cross_up":    cross_up,
            "cross_down":  cross_down,
        }
    }


# ── Sentiment via Claude ───────────────────────────────────────────────────────

def fetch_sentiment(headlines: list[str], current_price: float) -> dict:
    client = anthropic.Anthropic()

    prompt = f"""You are a professional Solana (SOL) futures trader analysing short-term (4–8 hour) price impact.
Current SOL/USDT price: ${current_price:.3f}

SOL typically RISES on: Solana ecosystem news, ETF speculation, broader crypto risk-on moves,
BTC strength, DeFi/NFT volume spikes, positive developer activity, institutional buying.
SOL typically FALLS on: network outages, SEC actions against crypto, BTC dumps, risk-off macro,
exchange hacks, regulatory crackdowns, whale selling.

Headlines:
{chr(10).join(f'- {h}' for h in headlines)}

Respond ONLY with valid JSON (no markdown, no extra text):
{{
  "signal": "BUY" | "SELL" | "FLAT",
  "strength": <float 0.0–1.0>,
  "summary": "<one concise sentence>"
}}"""

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}]
    )

    raw = response.content[0].text.strip()
    parsed = json.loads(raw)
    return {
        "signal":   parsed["signal"].upper(),
        "strength": float(parsed["strength"]),
        "summary":  parsed["summary"],
    }


# ── Signal Fusion ──────────────────────────────────────────────────────────────

DIR = {"BUY": 1, "FLAT": 0, "SELL": -1}

def fuse_signals(tech: dict, sentiment: dict) -> dict:
    t = DIR[tech["signal"]]      * tech["strength"]      * TECH_WEIGHT
    s = DIR[sentiment["signal"]] * sentiment["strength"]  * SENT_WEIGHT
    score = t + s

    if score > SIGNAL_THRESHOLD:
        sig, strength = "BUY",  min(score / (TECH_WEIGHT + SENT_WEIGHT), 1.0)
    elif score < -SIGNAL_THRESHOLD:
        sig, strength = "SELL", min(abs(score) / (TECH_WEIGHT + SENT_WEIGHT), 1.0)
    else:
        sig, strength = "FLAT", 0.0

    return {
        "signal":   sig,
        "strength": round(strength, 3),
        "score":    round(score, 4),
        "tech_contribution": round(t, 4),
        "sent_contribution": round(s, 4),
    }


# ── Position Sizing ────────────────────────────────────────────────────────────

def calculate_position(balance: float, atr_val: float, signal: str, price: float) -> dict:
    if signal == "FLAT":
        return {"qty": 0, "sl": 0, "tp": 0, "trailing_activation": 0, "trailing_distance": 0}

    direction = 1 if signal == "BUY" else -1
    sl_dist   = atr_val * ATR_SL_MULT
    tp_dist   = atr_val * ATR_TP_MULT

    # Bybit linear perpetual: PnL = qty × (exit - entry), in USDT
    risk_usdt  = balance * RISK_PER_TRADE
    # With leverage: actual position value = qty × price / leverage
    # Max loss = qty × sl_dist → qty = risk_usdt / sl_dist
    raw_qty    = risk_usdt / sl_dist
    # Round to step and apply min
    qty        = max(MIN_QTY, round(raw_qty / QTY_STEP) * QTY_STEP)
    # Cap at 5× risk amount worth of SOL
    max_qty    = (balance * LEVERAGE * 0.20) / price   # max 20% notional of leveraged balance
    qty        = round(min(qty, max_qty) / QTY_STEP) * QTY_STEP
    qty        = max(MIN_QTY, qty)

    sl_price   = round(price - direction * sl_dist, 3)
    tp_price   = round(price + direction * tp_dist, 3)
    trail_act  = round(price + direction * tp_dist * TRAIL_ACTIVATION, 3)
    trail_dist = round(sl_dist * TRAIL_DISTANCE, 3)

    notional   = qty * price
    margin     = notional / LEVERAGE

    return {
        "qty":                 qty,
        "sl":                  sl_price,
        "tp":                  tp_price,
        "trailing_activation": trail_act,
        "trailing_distance":   trail_dist,
        "notional_usdt":       round(notional, 2),
        "margin_used":         round(margin, 2),
        "risk_usdt":           round(risk_usdt, 2),
        "leverage":            LEVERAGE,
    }


# ── Main Entry ─────────────────────────────────────────────────────────────────

def run_skill(market_data: dict, headlines: list[str], account: dict) -> dict:
    """
    Args:
        market_data: { closes, highs, lows, current_price }
        headlines:   list of recent SOL/crypto news headlines
        account:     { balance, equity, open_positions: [] }
    Returns:
        Full decision dict — consumed by the Bybit bridge.
    """
    log.info(f"SOL skill | price={market_data['current_price']:.3f} | {len(headlines)} headlines")

    tech      = compute_technical_signal(market_data["closes"], market_data["highs"], market_data["lows"])
    log.info(f"Tech: {tech['signal']} @ {tech['strength']} | {tech['details']}")

    if headlines:
        sentiment = fetch_sentiment(headlines, market_data["current_price"])
    else:
        sentiment = {"signal": "FLAT", "strength": 0.0, "summary": "No headlines available"}
    log.info(f"Sent: {sentiment['signal']} @ {sentiment['strength']} | {sentiment['summary']}")

    fused = fuse_signals(tech, sentiment)
    log.info(f"Fused: {fused['signal']} @ {fused['strength']} (score={fused['score']})")

    has_position = len(account.get("open_positions", [])) > 0
    position = (
        {"qty": 0, "note": "already_in_position"}
        if has_position
        else calculate_position(account["balance"], tech["atr_value"], fused["signal"], market_data["current_price"])
    )

    return {
        "timestamp":  datetime.now(timezone.utc).isoformat(),
        "symbol":     SYMBOL,
        "timeframe":  TIMEFRAME,
        "leverage":   LEVERAGE,
        "technical":  tech,
        "sentiment":  sentiment,
        "fused":      fused,
        "position":   position,
        "execute":    fused["signal"] != "FLAT" and not has_position,
    }


# ── Local test ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import random
    random.seed(7)

    base = 135.0
    closes = [base + random.uniform(-4, 4) for _ in range(50)]
    for i in range(40, 50):
        closes[i] += (i - 40) * 0.35   # gentle uptrend
    highs  = [c + random.uniform(0.5, 2.5) for c in closes]
    lows   = [c - random.uniform(0.5, 2.5) for c in closes]

    result = run_skill(
        market_data={"closes": closes, "highs": highs, "lows": lows, "current_price": closes[-1]},
        headlines=[
            "Solana ETF filing gains traction with SEC, analysts bullish",
            "BTC breaks $90k resistance, altcoins follow",
            "Solana DeFi TVL hits new all-time high as ecosystem expands",
        ],
        account={"balance": 500.0, "equity": 500.0, "open_positions": []}
    )
    print(json.dumps(result, indent=2))
