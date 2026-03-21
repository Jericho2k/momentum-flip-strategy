"""
OpenClaw v2 — Full Trading Control Agent
==========================================
Telegram bot with complete visibility and control over the crypto bridge.

Commands:
  /status     — bridge health, uptime, current positions
  /positions  — all open positions with unrealized PnL
  /trades     — recent trade history with stats
  /pnl        — P&L breakdown: today, this week, total
  /report     — AI-generated strategy analysis (Claude)
  /pause      — pause new entries (keep existing positions)
  /resume     — resume new entries
  /closeall   — close all open positions immediately
  /close BTC  — close specific pair position
  /risk       — current risk exposure per pair
  /params     — show strategy parameters
  /help       — show all commands
"""

import os
import sys
import json
import time
import math
import hmac
import hashlib
import logging
import asyncio
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent))
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

import httpx
from anthropic import Anthropic
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("openclaw")

# ── Config ─────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
BYBIT_API_KEY    = os.getenv("BYBIT_API_KEY", "")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET", "")
ANTHROPIC_KEY    = os.getenv("ANTHROPIC_API_KEY", "")
BASE_URL         = "https://api-demo.bybit.com"
TRADE_LOG        = Path("trade_log_crypto.jsonl")
PAUSE_FILE       = Path(".bridge_paused")  # bridge checks this file

PAIRS = {
    "BTC": "BTCUSDT",
    "SOL": "SOLUSDT",
    "BNB": "BNBUSDT",
}

claude  = Anthropic(api_key=ANTHROPIC_KEY)
started = datetime.now(timezone.utc)


# ── Bybit client ───────────────────────────────────────────────────────────────
def _sign(params: dict) -> dict:
    ts = str(int(time.time() * 1000))
    rw = "5000"
    q  = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    sig = hmac.new(BYBIT_API_SECRET.encode(),
                   f"{ts}{BYBIT_API_KEY}{rw}{q}".encode(),
                   hashlib.sha256).hexdigest()
    return {"X-BAPI-API-KEY": BYBIT_API_KEY, "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-RECV-WINDOW": rw, "X-BAPI-SIGN": sig}


def bybit_get(endpoint: str, params: dict = None) -> dict:
    params = params or {}
    try:
        r = httpx.get(f"{BASE_URL}{endpoint}", params=params,
                      headers=_sign(params), timeout=10)
        return r.json()
    except Exception as e:
        log.error(f"GET {endpoint}: {e}")
        return {}


def bybit_post(endpoint: str, body: dict) -> dict:
    ts = str(int(time.time() * 1000))
    rw = "5000"
    payload = json.dumps(body)
    sig = hmac.new(BYBIT_API_SECRET.encode(),
                   f"{ts}{BYBIT_API_KEY}{rw}{payload}".encode(),
                   hashlib.sha256).hexdigest()
    headers = {
        "X-BAPI-API-KEY": BYBIT_API_KEY, "X-BAPI-TIMESTAMP": ts,
        "X-BAPI-RECV-WINDOW": rw, "X-BAPI-SIGN": sig,
        "Content-Type": "application/json"
    }
    try:
        r = httpx.post(f"{BASE_URL}{endpoint}",
                       content=payload, headers=headers, timeout=10)
        return r.json()
    except Exception as e:
        log.error(f"POST {endpoint}: {e}")
        return {}


# ── Data helpers ───────────────────────────────────────────────────────────────
def get_balance() -> float:
    resp = bybit_get("/v5/account/wallet-balance",
                     {"accountType": "UNIFIED"})
    try:
        coins = resp["result"]["list"][0]["coin"]
        usdt  = next((c for c in coins if c["coin"] == "USDT"), {})
        return float(usdt.get("walletBalance", 0))
    except Exception:
        return 0.0


def get_all_positions() -> list[dict]:
    positions = []
    for name, symbol in PAIRS.items():
        resp = bybit_get("/v5/position/list",
                         {"category": "linear", "symbol": symbol})
        for pos in resp.get("result", {}).get("list", []):
            size = float(pos.get("size", 0))
            if size > 0:
                entry  = float(pos.get("avgPrice", 0))
                mark   = float(pos.get("markPrice", 0))
                side   = pos["side"]
                upnl   = float(pos.get("unrealisedPnl", 0))
                mult   = 1 if side == "Buy" else -1
                pct    = mult*(mark-entry)/entry*100*3 if entry else 0
                positions.append({
                    "name":   name,
                    "symbol": symbol,
                    "side":   side,
                    "size":   size,
                    "entry":  entry,
                    "mark":   mark,
                    "upnl":   upnl,
                    "pct":    pct,
                })
    return positions


def get_price(symbol: str) -> float:
    resp = bybit_get("/v5/market/tickers",
                     {"category": "linear", "symbol": symbol})
    try:
        return float(resp["result"]["list"][0]["lastPrice"])
    except Exception:
        return 0.0


def read_trades(n: int = 50) -> list[dict]:
    if not TRADE_LOG.exists():
        return []
    trades = []
    with open(TRADE_LOG) as f:
        for line in f:
            try:
                trades.append(json.loads(line.strip()))
            except Exception:
                pass
    return trades[-n:]


def is_paused() -> bool:
    return PAUSE_FILE.exists()


def close_position(symbol: str, side: str, size: float) -> bool:
    close_side = "Sell" if side == "Buy" else "Buy"
    resp = bybit_post("/v5/order/create", {
        "category": "linear", "symbol": symbol,
        "side": close_side, "orderType": "Market",
        "qty": str(size), "timeInForce": "IOC",
        "reduceOnly": True,
    })
    return resp.get("retCode", -1) == 0


# ── Auth check ─────────────────────────────────────────────────────────────────
def authorized(update: Update) -> bool:
    if not TELEGRAM_CHAT_ID:
        return True
    return str(update.effective_chat.id) == str(TELEGRAM_CHAT_ID)


# ── Command handlers ───────────────────────────────────────────────────────────
async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update): return
    await update.message.reply_text(
        "🤖 *OpenClaw v2 — Command Reference*\n\n"
        "📊 *Monitoring*\n"
        "/status — bridge health + positions overview\n"
        "/positions — open positions with live PnL\n"
        "/trades — recent trade history\n"
        "/pnl — P&L breakdown (today/week/total)\n"
        "/risk — current exposure per pair\n"
        "/params — strategy parameters\n\n"
        "🎮 *Control*\n"
        "/pause — pause new entries\n"
        "/resume — resume trading\n"
        "/closeall — close all positions NOW\n"
        "/close BTC — close specific pair\n\n"
        "🧠 *AI Analysis*\n"
        "/report — Claude analysis of recent performance\n\n"
        "💬 *Chat*\n"
        "Just type anything — ask Claude about the strategy",
        parse_mode="Markdown"
    )


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update): return
    await update.message.reply_text("⏳ Fetching status...")

    balance   = get_balance()
    positions = get_all_positions()
    trades    = read_trades(100)
    uptime    = datetime.now(timezone.utc) - started
    paused    = is_paused()

    # Count today's trades
    today = datetime.now(timezone.utc).date()
    today_trades = [t for t in trades
                    if datetime.fromisoformat(t["ts"]).date() == today]

    pos_text = ""
    if positions:
        for p in positions:
            emoji = "🟢" if p["side"] == "Buy" else "🔴"
            sign  = "+" if p["pct"] >= 0 else ""
            pos_text += (f"\n{emoji} {p['name']}: {p['side']} "
                        f"{sign}{p['pct']:.2f}% uPnL: ${p['upnl']:.2f}")
    else:
        pos_text = "\nNo open positions"

    status_emoji = "⏸" if paused else "✅"
    await update.message.reply_text(
        f"*OpenClaw Status*\n\n"
        f"{status_emoji} Bridge: {'PAUSED' if paused else 'RUNNING'}\n"
        f"⏱ Uptime: {str(uptime).split('.')[0]}\n"
        f"💰 Balance: ${balance:,.2f} USDT\n"
        f"📈 Open positions: {len(positions)}\n"
        f"📊 Trades today: {len(today_trades)}\n"
        f"\n*Positions:*{pos_text}",
        parse_mode="Markdown"
    )


async def cmd_positions(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update): return
    positions = get_all_positions()

    if not positions:
        await update.message.reply_text("📭 No open positions")
        return

    text = "*Open Positions*\n\n"
    total_upnl = 0
    for p in positions:
        emoji  = "🟢" if p["side"] == "Buy" else "🔴"
        sign   = "+" if p["pct"] >= 0 else ""
        pnl_e  = "✅" if p["upnl"] >= 0 else "❌"
        text  += (f"{emoji} *{p['name']}* {p['side']}\n"
                 f"  Entry: ${p['entry']:,.2f} → Mark: ${p['mark']:,.2f}\n"
                 f"  {pnl_e} uPnL: ${p['upnl']:+.2f} ({sign}{p['pct']:.2f}%)\n"
                 f"  Size: {p['size']}\n\n")
        total_upnl += p["upnl"]

    sign = "+" if total_upnl >= 0 else ""
    text += f"Total uPnL: *${sign}{total_upnl:.2f}*"
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_trades(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update): return
    trades = read_trades(20)

    if not trades:
        await update.message.reply_text("📭 No trades logged yet")
        return

    # Show last 10 meaningful actions (not HOLD)
    actions = [t for t in trades if t.get("action") != "hold"][-10:]
    text = "*Recent Trades*\n\n"
    for t in reversed(actions):
        ts  = datetime.fromisoformat(t["ts"]).strftime("%m/%d %H:%M")
        sym = t["symbol"].replace("USDT","")
        act = t["action"].upper()
        emoji = ("🟢" if "long" in t["action"] else
                 "🔴" if "short" in t["action"] else
                 "⚪" if "close" in t["action"] else "📊")
        text += f"{emoji} {ts} *{sym}* {act} @ ${t['price']:,.2f}\n"

    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_pnl(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update): return
    await update.message.reply_text("⏳ Calculating P&L...")

    balance = get_balance()
    trades  = read_trades(500)
    now     = datetime.now(timezone.utc)

    # Starting balance estimate from first trade
    START_BALANCE = 48000.0  # demo account starting balance

    positions = get_all_positions()
    total_upnl = sum(p["upnl"] for p in positions)
    realized_pnl = balance - START_BALANCE

    text = "*P&L Report*\n\n"
    text += f"💰 Current balance: ${balance:,.2f}\n"
    text += f"📊 Starting balance: ${START_BALANCE:,.2f}\n"
    text += (f"{'✅' if realized_pnl>=0 else '❌'} "
             f"Realized P&L: ${realized_pnl:+,.2f} "
             f"({realized_pnl/START_BALANCE*100:+.2f}%)\n")
    text += (f"{'✅' if total_upnl>=0 else '❌'} "
             f"Unrealized P&L: ${total_upnl:+,.2f}\n")
    text += (f"\n💼 Total equity: "
             f"${balance+total_upnl:,.2f}\n")

    # Per-pair breakdown from trade log
    text += "\n*Activity (trade log):*\n"
    pair_counts = {}
    for t in trades:
        sym = t["symbol"].replace("USDT","")
        if t.get("action") not in ("hold", "regime_blocked"):
            pair_counts[sym] = pair_counts.get(sym, 0) + 1

    for sym, count in pair_counts.items():
        text += f"  {sym}: {count} actions\n"

    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_risk(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update): return
    balance   = get_balance()
    positions = get_all_positions()

    text = "*Risk Exposure*\n\n"
    text += f"Balance: ${balance:,.2f}\n"
    text += f"Leverage: 3×\n"
    text += f"Risk per trade: 1% = ${balance*0.01:,.2f}\n\n"

    if not positions:
        text += "No open positions — no current exposure"
    else:
        total_notional = 0
        for p in positions:
            notional = p["size"] * p["mark"]
            exposure = notional / balance * 100
            total_notional += notional
            text += (f"*{p['name']}*: ${notional:,.0f} notional "
                    f"({exposure:.1f}% of balance)\n")
        text += f"\nTotal exposure: ${total_notional:,.0f} "
        text += f"({total_notional/balance*100:.1f}% of balance)"

    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_params(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update): return
    text = (
        "*Strategy Parameters*\n\n"
        "*BTC/USDT*\n"
        "  MACD(10,26,9) hist≥0.08% ADX≥25\n\n"
        "*SOL/USDT*\n"
        "  MACD(8,21,5) hist≥0.08% ADX≥20\n\n"
        "*BNB/USDT*\n"
        "  MACD(10,26,3) hist≥0.01% ADX≥30\n\n"
        "*Shared filters*\n"
        "  Kill hours: 16, 17, 18 UTC\n"
        "  Skip Monday: ✅\n"
        "  Min hold bars: 16 (4 hours)\n"
        "  Regime filter: daily ADX ≥ 20\n"
        "  Leverage: 3×\n"
        "  Risk per trade: 1%\n"
        "  Backtested: 2024–2026\n\n"
        "*Performance (backtest, 3×)*\n"
        "  BTC: Sharpe 2.197 | DD -11%\n"
        "  SOL: Sharpe 1.570 | DD -55%\n"
        "  BNB: Sharpe 2.271 | DD -80%\n"
        "  Portfolio: Sharpe 2.260 | DD -20%"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_pause(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update): return
    PAUSE_FILE.touch()
    await update.message.reply_text(
        "⏸ *Bridge PAUSED*\n\n"
        "No new positions will be opened.\n"
        "Existing positions remain open.\n"
        "Use /resume to restart trading.",
        parse_mode="Markdown"
    )


async def cmd_resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update): return
    if PAUSE_FILE.exists():
        PAUSE_FILE.unlink()
    await update.message.reply_text(
        "▶️ *Bridge RESUMED*\n\nTrading is active again.",
        parse_mode="Markdown"
    )


async def cmd_closeall(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update): return
    await update.message.reply_text(
        "⚠️ *Closing ALL positions...*",
        parse_mode="Markdown"
    )
    positions = get_all_positions()
    if not positions:
        await update.message.reply_text("No open positions to close.")
        return

    closed = []
    failed = []
    for p in positions:
        if close_position(p["symbol"], p["side"], p["size"]):
            closed.append(p["name"])
        else:
            failed.append(p["name"])
        time.sleep(0.5)

    text = ""
    if closed:
        text += f"✅ Closed: {', '.join(closed)}\n"
    if failed:
        text += f"❌ Failed: {', '.join(failed)}"
    await update.message.reply_text(text or "Done.")


async def cmd_close(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update): return
    args = ctx.args
    if not args:
        await update.message.reply_text(
            "Usage: /close BTC or /close SOL or /close BNB"
        )
        return

    name = args[0].upper()
    if name not in PAIRS:
        await update.message.reply_text(f"Unknown pair: {name}. Use BTC, SOL, or BNB.")
        return

    positions = get_all_positions()
    pos = next((p for p in positions if p["name"] == name), None)
    if not pos:
        await update.message.reply_text(f"No open position for {name}.")
        return

    if close_position(pos["symbol"], pos["side"], pos["size"]):
        await update.message.reply_text(
            f"✅ Closed {name} {pos['side']} position\n"
            f"uPnL was: ${pos['upnl']:+.2f}"
        )
    else:
        await update.message.reply_text(f"❌ Failed to close {name} — check logs.")


async def cmd_report(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update): return
    await update.message.reply_text("🧠 Generating AI report...")

    trades    = read_trades(100)
    balance   = get_balance()
    positions = get_all_positions()

    actions = [t for t in trades if t.get("action") != "hold"]
    summary = {
        "balance":        balance,
        "open_positions": len(positions),
        "recent_actions": actions[-20:],
        "total_actions":  len(actions),
        "pairs_active":   list({t["symbol"] for t in actions}),
    }

    prompt = f"""You are OpenClaw, an AI trading assistant monitoring a live crypto trading bot.

Current bot state:
{json.dumps(summary, indent=2)}

Provide a concise trading report covering:
1. Current status and activity level
2. Which pairs are most active
3. Any patterns you notice in timing or signal frequency
4. Risk assessment based on position count and balance
5. One actionable recommendation

Keep it under 200 words. Be direct and specific."""

    try:
        resp = claude.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}]
        )
        report = resp.content[0].text
    except Exception as e:
        report = f"Claude unavailable: {e}"

    await update.message.reply_text(
        f"🧠 *AI Report*\n\n{report}",
        parse_mode="Markdown"
    )


async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle free-form chat — ask Claude anything about the strategy."""
    if not authorized(update): return

    user_msg  = update.message.text
    trades    = read_trades(50)
    balance   = get_balance()
    positions = get_all_positions()

    context = (
        f"Balance: ${balance:,.2f} | "
        f"Open positions: {len(positions)} | "
        f"Recent trades logged: {len(trades)}"
    )

    try:
        resp = claude.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=300,
            system=(
                "You are OpenClaw, an AI assistant for a crypto trading bot "
                "running MACD/ADX strategy on BTC, SOL, BNB perpetuals on Bybit. "
                "The strategy uses kill hours 16-18 UTC, skip Monday, min hold 16 bars, "
                "3x leverage, 1% risk per trade. "
                "Be concise, specific, and helpful. Answer in plain text without markdown."
                f"\nCurrent state: {context}"
            ),
            messages=[{"role": "user", "content": user_msg}]
        )
        await update.message.reply_text(resp.content[0].text)
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    if not TELEGRAM_TOKEN:
        log.error("TELEGRAM_BOT_TOKEN not set in .env")
        return

    log.info("🤖 OpenClaw v2 starting...")

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("help",      cmd_help))
    app.add_handler(CommandHandler("start",     cmd_help))
    app.add_handler(CommandHandler("status",    cmd_status))
    app.add_handler(CommandHandler("positions", cmd_positions))
    app.add_handler(CommandHandler("trades",    cmd_trades))
    app.add_handler(CommandHandler("pnl",       cmd_pnl))
    app.add_handler(CommandHandler("risk",      cmd_risk))
    app.add_handler(CommandHandler("params",    cmd_params))
    app.add_handler(CommandHandler("pause",     cmd_pause))
    app.add_handler(CommandHandler("resume",    cmd_resume))
    app.add_handler(CommandHandler("closeall",  cmd_closeall))
    app.add_handler(CommandHandler("close",     cmd_close))
    app.add_handler(CommandHandler("report",    cmd_report))
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND, handle_message
    ))

    log.info("OpenClaw v2 running — waiting for commands")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
