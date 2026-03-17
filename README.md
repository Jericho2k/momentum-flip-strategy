# SOL/USDT Trading Bot

Solana perpetual futures trading system on Bybit with OpenClaw AI oversight.

## Components

- **bot/** — live trading engine (hybrid EMA/sentiment signals, Bybit execution)
- **backtest/** — MACD/ADX strategy backtester, optimizer, and browser dashboard
- **openclaw_agent.py** — AI oversight agent (monitors strategy, answers questions)

## Setup

1. Clone the repo
2. Install dependencies: `pip install -r requirements.txt`
3. Copy `.env.example` to `.env` and fill in your API keys
4. Get keys from:
   - Anthropic: https://console.anthropic.com
   - Bybit testnet: https://testnet.bybit.com (start here)
   - NewsAPI: https://newsapi.org (free tier)

## Running

Start on testnet first — set TESTNET=True in bybit_bridge.py (it is by default).

**MACD/ADX bot (validated, recommended for testnet):**
```bash
python bot/macd_adx_bridge.py
```

**OpenClaw oversight agent** (run in a second terminal alongside either bot):
```bash
python bot/openclaw_agent.py
```

**Hybrid EMA/sentiment bot:**
```bash
python bot/bybit_bridge.py
```

**Backtester CLI:**
```bash
python backtest/backtester.py          # single run with default params
python backtest/backtester.py optimize # full grid search
```

**Interactive dashboard:**
Open `backtest/backtest_dashboard.html` directly in Firefox. No server needed.

## Strategy

**Bot:** Hybrid intraday — EMA 9/21 crossover + RSI + ATR (60%) combined with Claude news sentiment scoring (40%). H1 timeframe, 3× leverage, 1% risk per trade, ATR-based SL/TP with trailing stop.

**MACD/ADX:** Histogram crossover flip strategy with ADX trend filter. Stays long or short, flips on signal. Optimized via grid search on 365 days of real Bybit data.

## Status

- [x] Bot logic + Bybit bridge
- [x] OpenClaw oversight agent
- [x] MACD/ADX backtester + optimizer
- [x] Interactive dashboard
- [ ] Backtest the hybrid bot signals
- [ ] Validate best MACD/ADX params on out-of-sample data
- [ ] Paper trade for 4 weeks before going live
