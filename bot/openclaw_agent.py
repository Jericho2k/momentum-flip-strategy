"""
OpenClaw Oversight Agent — SOL/USDT Strategy
============================================
Runs alongside the Bybit bridge as an intelligent overseer.
Monitors the strategy, generates reports, surfaces observations,
and suggests parameter adjustments — but never touches execution.

Usage:
    python openclaw_agent.py

OpenClaw is used here as a conversational agent loop:
  - You can chat with it in natural language
  - It reads the shared trade log and market state
  - It proactively generates reports on a schedule
  - It never calls place_order or modifies the bridge config directly
"""

import json
import time
import logging
import os
from dotenv import load_dotenv
load_dotenv()
from datetime import datetime, timezone
from pathlib import Path
import anthropic

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("openclaw_agent")

# ── Shared state file written by the bridge ────────────────────────────────────
# The Bybit bridge appends a JSON line here after every decision cycle.
# This agent reads it — completely decoupled, no shared memory.
TRADE_LOG_PATH   = Path("trade_log.jsonl")
REPORT_INTERVAL  = 3600   # auto-report every hour (seconds)

client = anthropic.Anthropic()

# ── Agent memory (in-process, current session) ────────────────────────────────
conversation_history = []

SYSTEM_PROMPT = """You are an expert quantitative trading oversight agent for a SOL/USDT perpetual futures strategy running on Bybit.

YOUR ROLE:
You monitor a live hybrid intraday trading strategy and act as an intelligent advisor to the trader (the user).
You observe, analyse, report, and suggest — but you never execute trades or modify code directly.
Think of yourself as a sharp, experienced quant sitting next to the trader, watching the same screen.

THE STRATEGY YOU ARE MONITORING:
- Asset: SOL/USDT linear perpetual on Bybit
- Timeframe: H1 (hourly candles)
- Leverage: 3× fixed
- Signal: Hybrid — 60% technical (EMA 9/21 crossover + RSI filter + ATR momentum), 40% news sentiment (Claude-scored headlines)
- Entry threshold: fused score must exceed ±0.22
- Risk per trade: 1% of account balance
- Stop-loss: 1.5× ATR from entry
- Take-profit: 2.5× ATR from entry (R:R = 1:1.67)
- Trailing stop: activates at 60% of TP distance, trails at 40% of SL distance
- Max 1 open position at a time

YOUR CAPABILITIES:
1. Read and interpret trade log data passed to you
2. Identify patterns in signal quality, win rate, and drawdown
3. Suggest parameter adjustments (ATR multipliers, signal threshold, leverage, weights)
4. Flag anomalies: suspiciously frequent signals, large losing streaks, sentiment/technical divergence
5. Generate periodic performance reports
6. Answer the trader's questions about what the strategy is doing and why
7. Offer educational context about market conditions affecting SOL

YOUR CONSTRAINTS:
- Never suggest increasing leverage above 5×
- Always frame suggestions as observations, not commands
- When uncertain, say so — do not hallucinate performance data
- If you see a losing streak of 4+ trades, always flag it prominently
- Keep reports concise — the trader is busy

TONE: Direct, analytical, occasionally dry. Like a good quant colleague.
"""


# ── Trade log utilities ────────────────────────────────────────────────────────

def read_recent_trades(n: int = 20) -> list[dict]:
    """Read the last n entries from the trade log."""
    if not TRADE_LOG_PATH.exists():
        return []
    lines = TRADE_LOG_PATH.read_text().strip().split("\n")
    lines = [l for l in lines if l.strip()]
    recent = lines[-n:]
    parsed = []
    for line in recent:
        try:
            parsed.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return parsed


def compute_summary(trades: list[dict]) -> dict:
    """Compute basic performance stats from the trade log."""
    decisions = [t for t in trades if t.get("execute")]
    if not decisions:
        return {"total_signals": 0, "note": "No executed signals in log"}

    total   = len(decisions)
    longs   = sum(1 for t in decisions if t["fused"]["signal"] == "LONG")
    shorts  = total - longs
    flat    = len([t for t in trades if not t.get("execute")])

    # Estimate avg signal strength
    avg_str = sum(t["fused"]["strength"] for t in decisions) / total

    # Sentiment vs technical agreement rate
    agree = sum(
        1 for t in decisions
        if t["technical"]["signal"] == t["sentiment"]["signal"]
    )
    agree_pct = round(agree / total * 100, 1)

    return {
        "total_signals":      total,
        "longs":              longs,
        "shorts":             shorts,
        "flat_skipped":       flat,
        "avg_signal_strength": round(avg_str, 3),
        "tech_sent_agreement_pct": agree_pct,
    }


def format_trades_for_agent(trades: list[dict]) -> str:
    """Format trade log into a readable summary for the agent."""
    if not trades:
        return "No trade log data available yet."

    lines = ["Recent strategy decisions (newest last):"]
    for t in trades[-10:]:
        ts   = t.get("timestamp", "?")[:16]
        sig  = t.get("fused", {}).get("signal", "?")
        str_ = t.get("fused", {}).get("strength", 0)
        scr  = t.get("fused", {}).get("score", 0)
        tech = t.get("technical", {}).get("signal", "?")
        sent = t.get("sentiment", {}).get("signal", "?")
        sent_sum = t.get("sentiment", {}).get("summary", "")
        exe  = t.get("execute", False)
        qty  = t.get("position", {}).get("qty", 0)
        sl   = t.get("position", {}).get("sl", 0)
        tp   = t.get("position", {}).get("tp", 0)

        line = (
            f"[{ts}] {sig:5s} str={str_:.2f} score={scr:+.3f} "
            f"tech={tech} sent={sent} exe={'YES' if exe else 'no'}"
        )
        if exe:
            line += f" qty={qty} SL={sl} TP={tp}"
        if sent_sum:
            line += f"\n  Sentiment: {sent_sum}"
        lines.append(line)

    summary = compute_summary(trades)
    lines.append(f"\nSummary: {json.dumps(summary, indent=2)}")
    return "\n".join(lines)


# ── Agent interaction ──────────────────────────────────────────────────────────

def chat(user_message: str, include_trade_context: bool = True) -> str:
    """Send a message to the OpenClaw agent and get a response."""

    # Attach trade log context to every message
    if include_trade_context:
        trades = read_recent_trades(30)
        context = format_trades_for_agent(trades)
        full_message = f"{user_message}\n\n--- Current trade log context ---\n{context}"
    else:
        full_message = user_message

    conversation_history.append({"role": "user", "content": full_message})

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1000,
        system=SYSTEM_PROMPT,
        messages=conversation_history,
    )

    reply = response.content[0].text
    conversation_history.append({"role": "assistant", "content": reply})

    # Keep history manageable — last 20 turns
    if len(conversation_history) > 40:
        conversation_history[:] = conversation_history[-40:]

    return reply


def generate_hourly_report() -> str:
    """Auto-generate a periodic strategy health report."""
    prompt = (
        "Generate a concise hourly strategy report. Cover: "
        "signal quality, any concerning patterns, SOL market context if relevant, "
        "and one actionable observation. Keep it under 200 words."
    )
    return chat(prompt)


def write_trade_decision(decision: dict):
    """Called by the bridge to log each decision. Append to shared log file."""
    with open(TRADE_LOG_PATH, "a") as f:
        f.write(json.dumps(decision) + "\n")


# ── CLI REPL ───────────────────────────────────────────────────────────────────

COMMANDS = {
    "/report":  "Generate a full strategy report",
    "/trades":  "Show recent trade log summary",
    "/params":  "Ask the agent to review current parameters",
    "/risk":    "Get a risk assessment of current settings",
    "/sol":     "Ask agent for SOL market context right now",
    "/help":    "Show this command list",
    "/quit":    "Exit",
}

def print_help():
    print("\nAvailable commands:")
    for cmd, desc in COMMANDS.items():
        print(f"  {cmd:12s}  {desc}")
    print("  Or just type any question in natural language.\n")


def run_repl():
    print("\n" + "="*60)
    print("  OpenClaw SOL Strategy Oversight Agent")
    print("  Type /help for commands or ask anything")
    print("="*60 + "\n")

    # Greet with an initial context read
    greeting = chat(
        "Introduce yourself briefly and give me a one-sentence status on the strategy based on whatever data is available.",
        include_trade_context=True
    )
    print(f"Agent: {greeting}\n")

    last_report_time = time.time()

    while True:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nAgent: Shutting down oversight. Trade safe.")
            break

        if not user_input:
            continue

        # Auto hourly report
        if time.time() - last_report_time > REPORT_INTERVAL:
            print("\n[Auto-report triggered]")
            report = generate_hourly_report()
            print(f"Agent: {report}\n")
            last_report_time = time.time()

        # Commands
        if user_input == "/quit":
            print("Agent: Shutting down oversight. Trade safe.")
            break
        elif user_input == "/help":
            print_help()
            continue
        elif user_input == "/report":
            user_input = "Generate a full strategy performance report including signal quality, parameter health, and any concerns."
        elif user_input == "/trades":
            user_input = "Summarise the recent trades for me — what signals fired, did tech and sentiment agree, any patterns?"
        elif user_input == "/params":
            user_input = "Review the current strategy parameters (ATR multipliers 1.5/2.5, signal threshold 0.22, 60/40 weight split, 3× leverage). Are they well-calibrated for SOL's current volatility? Any suggestions?"
        elif user_input == "/risk":
            user_input = "Give me a risk assessment: is 1% per trade appropriate, is 3× leverage reasonable, what's the worst-case scenario I should be prepared for?"
        elif user_input == "/sol":
            user_input = "What should I know about SOL market conditions right now that might affect the strategy? Consider macro, on-chain trends, and any known catalysts."

        response = chat(user_input)
        print(f"\nAgent: {response}\n")


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    run_repl()
