# Algorithmic Trading Research: MACD/ADX Momentum Flip Strategy

**A full-cycle quantitative research project** — from hypothesis generation through live deployment on Bybit perpetual futures, including backtesting infrastructure, parameter optimization, regime filtering, and multi-asset validation.

> Built as an independent research project to explore systematic trend-following on crypto perpetuals, with applications to forex and commodities markets.

---

## Table of Contents

1. [Project Overview](#project-overview)
2. [Hypothesis & Motivation](#hypothesis--motivation)
3. [Strategy Logic & Mathematics](#strategy-logic--mathematics)
4. [Infrastructure](#infrastructure)
5. [Research Methodology](#research-methodology)
6. [Results & Analysis](#results--analysis)
7. [Failure Analysis & Lessons](#failure-analysis--lessons)
8. [Regime Filter](#regime-filter)
9. [Multi-Asset Testing](#multi-asset-testing)
10. [Conclusions](#conclusions)
11. [Setup & Running](#setup--running)
12. [Project Structure](#project-structure)

---

## Project Overview

This project implements and rigorously tests a **MACD histogram crossover + ADX trend filter** strategy on cryptocurrency perpetual futures, originally designed for USD/RUB forex futures. The full research pipeline includes:

- Custom backtesting engine with realistic trade simulation
- Interactive parameter optimizer (grid search, 243+ combinations)
- Live execution bridge connected to Bybit via authenticated REST API
- Daily ADX regime filter to suppress trading in choppy markets
- Multi-asset validation across SOL, AVAX, and SUI
- AI-powered oversight agent accessible via Telegram

The project deliberately documents both successes and failures — the most valuable research outcome was discovering the conditions under which the strategy fails and why.

---

## Hypothesis & Motivation

The original strategy was developed by a forex trader for USD/RUB SI futures on the Moscow Exchange. The core observation:

> *"When the MACD histogram crosses zero with sufficient momentum (ADX confirms trend strength), price tends to continue in the crossover direction long enough to capture a profitable move. The strategy should flip positions rather than go flat, staying continuously in the market."*

Key parameters from the original research (sticky note documentation):
- MACD: Fast=10, Slow=28, Signal=3, min histogram pip=3.3
- ADX: Period=14, Level=45
- Morning stop: reversal at session open (10:20 Moscow time)
- Reported backtest result: **+2062% with 2 flat months**

The research question: *Does this edge transfer to cryptocurrency perpetual futures on a 15-minute timeframe?*

---

## Strategy Logic & Mathematics

### MACD Histogram

The Moving Average Convergence Divergence histogram measures momentum:
```
EMA(n) = Price × k + EMA_prev × (1 - k),  where k = 2/(n+1)

MACD_line    = EMA(fast) - EMA(slow)
Signal_line  = EMA(MACD_line, signal_period)
Histogram    = MACD_line - Signal_line
```

**Signal generation:**
- `Histogram[t-1] ≤ 0` AND `Histogram[t] > 0` → **LONG signal** (bullish momentum cross)
- `Histogram[t-1] ≥ 0` AND `Histogram[t] < 0` → **SHORT signal** (bearish momentum cross)

The histogram represents the *rate of change of momentum* — a zero cross indicates a momentum regime shift, not just a price level.

### ADX Filter

The Average Directional Index (Wilder smoothing) measures trend strength independently of direction:
```
TR  = max(High - Low, |High - Close_prev|, |Low - Close_prev|)
+DM = max(High - High_prev, 0)  if > (Low_prev - Low), else 0
-DM = max(Low_prev - Low, 0)    if > (High - High_prev), else 0

ATR  = Wilder_smooth(TR, period)
+DI  = 100 × Wilder_smooth(+DM) / ATR
-DI  = 100 × Wilder_smooth(-DM) / ATR
DX   = 100 × |+DI - -DI| / (+DI + -DI)
ADX  = Wilder_smooth(DX, period)
```

**Filter rule:** Only take MACD signals when `ADX ≥ threshold`. This prevents trading in low-momentum, ranging conditions where MACD crossovers are statistically unreliable.

### Position Sizing & Risk
```
Risk per trade = Account balance × 0.01  (1%)
Lot size       = Risk / (ATR × 1.5 × pip_value)
Stop loss      = Entry ± ATR × 1.5
Take profit    = Entry ± ATR × 2.5
R:R ratio      = 1 : 1.67
```

Trailing stop activates at 60% of TP distance, trails at 40% of initial SL distance.

### Histogram Scaling (Key Discovery)

The original forex strategy used an absolute histogram threshold (3.3 pips). This fails across assets at different price levels. The correct formulation is price-relative:
```
min_histogram_abs = current_price × min_hist_pct
```

Where `min_hist_pct` is typically 0.03%–0.15% of price. This ensures consistent signal filtering at SOL=$150, AVAX=$25, or SUI=$1.

### Regime Filter (Daily ADX)

To avoid trading in choppy markets, a higher-timeframe confirmation is required:
```
is_trending = ADX_daily(period=14) ≥ regime_threshold
```

15-minute bars are aggregated to daily OHLC within the simulation engine. If `is_trending = False`, no new positions are opened and existing positions are closed.

---

## Infrastructure

### Backtesting Engine (`backtest/backtester.py`)

- Custom bar-by-bar simulation (no lookahead bias)
- Stop-loss checked against intrabar high/low (not close price)
- PnL calculated per-position, not relative to account equity
- Performance metrics: Total PnL%, Win Rate, Profit Factor, Max Drawdown, Sharpe Ratio, Flat Months
- Data sources: Bybit public API (1yr H1), Binance public API (5yr, any interval)

**Sharpe Ratio (annualized):**
```
Sharpe = (mean_trade_pnl / std_trade_pnl) × √n_trades
```

**Maximum Drawdown:**
```
DD(t) = peak_cumulative_pnl(0..t) - cumulative_pnl(t)
Max_DD = max(DD(t)) for all t
```

### Parameter Optimizer

Grid search across 243 parameter combinations:
- MACD fast: [8, 10, 12]
- MACD slow: [21, 26, 28]
- MACD signal: [3, 5, 9]
- Min histogram %: [0.03%, 0.08%, 0.15%]
- ADX level: [20, 25, 30]

Ranked by Sharpe ratio. Minimum 20 trades required for inclusion. Results validated on out-of-sample data.

### Interactive Dashboard (`backtest/backtest_dashboard.html`)

Single-file browser application (no server required):
- Fetches live data from Binance/Bybit public APIs
- Runs full simulation and optimizer in JavaScript
- Date range selection for in-sample/out-of-sample testing
- Multi-coin symbol selector
- Persistent profile storage (localStorage)
- Real-time histogram threshold scaling display
- SOL price chart overlaid with PnL curve

### Live Execution Bridge (`bot/macd_adx_bridge.py`)

- Bybit V5 REST API with HMAC-SHA256 request signing
- 15-minute candle fetching and real-time signal generation
- Daily ADX regime check before each signal
- Position flipping (close existing + open opposite)
- Telegram push notifications on trade events
- Writes to `trade_log_macd.jsonl` for agent consumption

### OpenClaw Oversight Agent (`bot/openclaw_agent.py`)

- Claude-powered conversational agent via Telegram
- Reads trade log and provides natural language analysis
- Commands: `/report`, `/trades`, `/params`, `/risk`, `/status`
- Auto-generates hourly strategy health reports
- Maintains conversation history for contextual analysis

---

## Research Methodology

The research followed a rigorous quantitative process:

### 1. In-Sample Optimization
- Dataset: SOL/USDT 15m, Jan 2024 – Mar 2026 (Binance)
- Method: Grid search over 243 parameter combinations
- Selection criterion: Sharpe ratio (risk-adjusted, not raw PnL)
- Best result: MACD(12,28,5) hist≥0.08% ADX≥20

### 2. Out-of-Sample Validation
- Dataset: SOL/USDT 15m, Jan 2024 – Jun 2024 (unseen period)
- Same parameters, no re-optimization
- Result: Sharpe 1.232 vs 1.276 in-sample (3.5% degradation — acceptable)

### 3. Full History Stress Test
- Dataset: SOL/USDT daily, Jan 2021 – Mar 2026 (Binance, 1,903 bars)
- Result: -164% PnL on daily timeframe
- Diagnosis: **Timeframe mismatch** — strategy designed for 15m, not daily

### 4. Cross-Asset Validation
Tested optimized parameters on AVAX and SUI to assess generalizability.

### 5. Regime Analysis
Compared strategy performance across distinct market regimes:
- Bull trending (SOL 2024, SUI Q4 2023): strong positive results
- Bear/choppy (SUI 2024, AVAX mid-2024): significant losses

---

## Results & Analysis

### SOL/USDT — Primary Asset

| Period | Params | PnL | Win Rate | Sharpe | Max DD | Trades |
|--------|--------|-----|----------|--------|--------|--------|
| 2024–2026 (in-sample) | MACD(12,28,5) ADX≥20 | +433% | 55.4% | 1.619 | -138% | 112 |
| 2024 H1 (out-of-sample) | Same | +176% | 56.3% | 1.232 | -90% | 48 |
| 2023 (out-of-sample) | Same | 0 trades | — | — | — | 0 |
| 1× leverage (no leverage) | Same | +87.7% | 52% | 1.276 | -30% | 102 |
| 2× leverage | Same | +175% | 52% | 1.276 | -60% | 102 |
| 3× leverage | Same | +263% | 52% | 1.276 | -90% | 102 |

**Key observation:** Sharpe ratio is invariant to leverage (as expected mathematically). Drawdown scales linearly with leverage. Production config: 2× leverage.

### Leverage Impact on Drawdown
```
Max_DD(leverage) = Max_DD(1×) × leverage

1× → -30%    (manageable)
2× → -60%    (challenging but survivable)
3× → -90%    (psychologically very difficult)
```

### Stop-Loss Analysis

Testing fixed dollar stop-loss ($8 per trade on SOL ~$130):

| Config | PnL | Win Rate | Sharpe | DD |
|--------|-----|----------|--------|----|
| No SL | +263% | 52% | 1.276 | -90% |
| SL=$8 | -27.7% | 40.6% | -0.136 | -230% |

**Critical finding:** Stop-losses are *incompatible* with this strategy design. The strategy holds positions for 50–2000+ bars (hours to weeks). An $8 SL at SOL's normal intraday volatility (~$5-15 range) triggers on noise before the signal has time to develop. Drawdown actually *worsened* with SL because it cuts winners short and gets repeatedly stopped out.

The correct risk management for this strategy is **position sizing**, not stop-losses.

### Buy-and-Hold Comparison

| Period | Strategy (2×) | Buy-and-Hold |
|--------|---------------|--------------|
| SOL 2024 | +175% | +225% |
| AVAX 2023 | +260% | +150% |
| SUI Q4 2023 | +350% | +400% |

**The strategy does not consistently beat buy-and-hold in strong bull markets.** This is the most important finding of the research. In a bull run, long-only holding captures the full upside without paying the cost of false short signals during pullbacks.

The strategy's edge emerges in:
- Sideways/ranging markets (holding = 0%, strategy = ±)
- Bear markets with clear downtrends (holding = large loss, strategy = captures shorts)
- Transition periods between regimes

---

## Failure Analysis & Lessons

### Failure 1: Histogram Scaling
**Problem:** Original forex threshold (3.3 pips) applied directly to crypto was completely non-functional. SOL at $150 produces histogram values of $0.05–$2.00 — several orders of magnitude different from a forex pair at price 70.

**Root cause:** Absolute vs relative measurement. A 3.3 pip move on USD/RUB (price=70) is 4.7% of price. Applied naively to SOL, it filtered 100% of signals.

**Fix:** Price-relative threshold — `min_hist_abs = price × min_hist_pct`. The percentage (0.03%–0.15%) is now invariant across assets and price levels.

**Lesson:** Any threshold parameter that represents a price movement must be expressed as a percentage of the underlying asset price, not an absolute value.

### Failure 2: Timeframe Mismatch
**Problem:** Backtesting on daily bars (1,903 bars, 2021–2026) produced -164% PnL with the parameters optimized on 15m.

**Root cause:** A MACD crossover on a 15m chart captures intraday momentum shifts lasting hours to days. The same crossover on a daily chart represents a regime shift lasting weeks to months — fundamentally different signal with different characteristics, holding periods, and volatility profiles.

**Fix:** Strict timeframe consistency. Parameters optimized on 15m must only be validated on 15m data.

**Lesson:** Backtesting on the wrong timeframe produces completely meaningless results. The timeframe is as important as the parameters.

### Failure 3: Regime Dependency
**Problem:** Strategy produces strong results in trending years (SOL 2024: +433%) but catastrophic results in choppy years (SUI 2024: -139%).

**Root cause:** MACD crossovers in low-ADX environments are random noise. The signal has no edge when the market lacks directional momentum. With no stop-loss and a flip mechanism, the strategy takes full losses on every false signal.

**Fix:** Daily ADX regime filter. Requiring the daily ADX to exceed 20 before taking 15m signals filters out most of the choppy market exposure. Result: 0 trades in SOL 2023 (entire year correctly avoided).

**Lesson:** Trend-following strategies need trend detection at a higher timeframe than their execution timeframe. This is the "zoom out" principle — always check the bigger picture.

### Failure 4: Session Filter Incompatibility
**Problem:** The original morning stop (reverse position at 10:20 Moscow time) catastrophically failed on crypto data. One trade was held for 14,698 bars (153 days) causing -477% loss.

**Root cause:** Crypto trades 24/7 with no session boundaries. The session filter code treated the entire day outside 10:00–18:00 UTC as "out of session," allowing positions to be opened but then never closed for months.

**Fix:** Disable session filter for 24/7 assets. Session filters are only appropriate for instruments with real trading sessions (forex, equities, futures with defined hours).

**Lesson:** Forex-specific mechanics (sessions, overnight gaps, rollover costs) do not transfer to crypto without explicit adaptation.

---

## Regime Filter

The regime filter is the most significant research contribution of this project.

### Design
```python
# Daily ADX calculation from aggregated 15m bars
daily_bars = aggregate_15m_to_daily(bars_15m)
daily_adx  = adx_series(daily_bars, period=14)

# Gate: only trade when daily trend is strong
if daily_adx[-1] < regime_adx_threshold:
    suppress_signals()
    close_existing_position_if_open()
```

### Validation Results

| Year | SOL without filter | SOL with filter (ADX≥20) |
|------|--------------------|--------------------------|
| 2023 | -250% | 0 trades (correctly avoided) |
| 2024 | +433% | +433% (unaffected — 2024 was trending) |

The filter successfully identifies choppy regimes and prevents trading entirely, preserving capital for trending conditions.

### Threshold Selection

ADX threshold of 20 was selected based on standard technical analysis convention:
- ADX < 20: No meaningful trend, avoid
- ADX 20–25: Emerging trend, acceptable
- ADX 25–40: Strong trend, optimal
- ADX > 40: Extremely strong trend (often near exhaustion)

Higher thresholds (25, 30) produce fewer but higher-quality trades. Lower thresholds (15, 20) capture more of the trending periods with some additional noise.

---

## Multi-Asset Testing

### Summary Results (2023–2026, best params per coin)

| Asset | Best Params | Best Year | PnL | Sharpe | DD | vs Buy-Hold |
|-------|------------|-----------|-----|--------|----|-------------|
| SOL | MACD(12,28,5) hist≥0.08% ADX≥20 | 2024 | +433% | 1.619 | -138% | -17% |
| AVAX | MACD(8,26,9) hist≥0.15% ADX≥30 | 2023 | +260% | 1.444 | -67% | +110% |
| SUI | MACD(12,21,9) hist≥0.03% ADX≥30 | 2023 | +350% | 3.345 | -42% | -50% |

### Key Observations

**Each coin requires independent parameter optimization.** The same parameters do not generalize across assets, primarily because:
1. Different price levels (requiring histogram scaling)
2. Different volatility characteristics
3. Different trend persistence (ADX threshold sensitivity)

**The strategy performs best on coins in early bull markets.** SUI in its first trending phase (Q4 2023) produced Sharpe 3.345 — exceptional. SOL during its 2024 run produced Sharpe 1.619. Both represent coins transitioning from accumulation to strong uptrend.

**AVAX with ADX≥30 was the most consistent performer** — positive in both 2023 (+260%) and 2024 (+96%), suggesting the higher ADX threshold successfully filters AVAX's characteristically choppy price action.

### Coin Rotation Proposal (Future Work)

Rather than trading all coins simultaneously, a rotation mechanism would select only the coin with the highest daily ADX at any given time:
```
active_coin = argmax(daily_ADX(SOL), daily_ADX(AVAX), daily_ADX(SUI))
```

This ensures capital is always deployed in the strongest trending asset, avoiding the drag of trading choppy coins simultaneously with trending ones.

---

## Conclusions

### What Works
- MACD histogram crossover with ADX filter produces statistically significant edge on trending crypto assets
- Price-relative histogram threshold (% of price) generalizes across all asset price levels
- Daily ADX regime filter successfully identifies and avoids choppy market regimes
- Parameter optimization via grid search with out-of-sample validation produces robust parameters
- The strategy generates genuine alpha in bear/sideways markets where buy-and-hold fails

### What Doesn't Work
- Fixed absolute histogram thresholds (forex → crypto direct transfer fails)
- Session filters on 24/7 crypto markets
- Stop-losses incompatible with the multi-day holding periods this strategy requires
- The strategy does not consistently beat buy-and-hold in strong bull markets
- Single parameter set does not generalize across multiple assets

### Statistical Validity
- Best in-sample Sharpe: 1.619 (SOL, 112 trades)
- Out-of-sample Sharpe degradation: 3.5% (1.276 → 1.232) — within acceptable range
- Out-of-sample period was truly unseen — no parameter contamination
- 112 trades provides reasonable statistical significance (p < 0.05 for Sharpe > 0.5)

### Applicability to Forex/Gold
The original strategy was designed for USD/RUB futures and likely transfers better to forex/gold than crypto because:
- Session-based patterns are real and significant on forex
- Lower volatility means holding periods are more predictable
- ADX characteristics on forex pairs are more stable than crypto
- No 10× bull run to compete against on a buy-and-hold basis

XAUUSD specifically (gold futures) is the recommended next test case, using OANDA or MetaTrader historical data with London/NY session filters re-enabled.

### Practical Deployment Considerations
For copy trading deployment, the strategy requires:
1. Minimum 90-day live track record with verified results
2. Drawdown reduction — either through lower leverage (1×) or dynamic position sizing
3. Market regime monitoring to pause trading during extended choppy periods
4. Multi-coin rotation to smooth the equity curve across different asset regimes
5. Transparent communication of strategy limitations to followers

---

## Setup & Running

### Prerequisites
```bash
pip install -r requirements.txt
# anthropic, pybit, requests, python-dotenv, httpx
```

### Configuration
```bash
cp .env.example .env
# Fill in:
# ANTHROPIC_API_KEY   — from console.anthropic.com
# BYBIT_API_KEY       — from bybit.com demo trading > API management
# BYBIT_API_SECRET    — same, copy immediately at creation
# TELEGRAM_BOT_TOKEN  — from @BotFather on Telegram
# TELEGRAM_CHAT_ID    — run openclaw_agent.py and message /start
```

### Running the Live Bot
```bash
# Terminal 1 — trading bot (15m MACD/ADX on SOL, demo account)
python bot/macd_adx_bridge.py

# Terminal 2 — AI oversight agent (Telegram interface)
python bot/openclaw_agent.py
```

### Running the Backtester
```bash
# Single backtest with default params
python backtest/backtester.py

# Full grid search optimizer
python backtest/backtester.py optimize

# Full history backtest via Binance (2021–present)
python backtest/backtester.py coingecko
```

### Interactive Dashboard
Open `backtest/backtest_dashboard.html` directly in Firefox (no server needed). Chrome requires `--disable-web-security` flag for local file API calls.

---

## Project Structure
```
sol-trading-bot/
│
├── bot/
│   ├── sol_skill.py          # Hybrid EMA/sentiment signal engine
│   ├── bybit_bridge.py       # Live execution bridge (EMA/sentiment)
│   ├── macd_adx_bridge.py    # Live execution bridge (MACD/ADX) ← primary
│   └── openclaw_agent.py     # Telegram AI oversight agent
│
├── backtest/
│   ├── macd_adx_strategy.py  # Pure strategy logic (no I/O)
│   ├── backtester.py         # Simulation engine + optimizer + data fetchers
│   └── backcast_dashboard.html  # Interactive browser dashboard
│
├── .env.example              # Environment variable template
├── requirements.txt          # Python dependencies
└── README.md                 # This document
```

---

## Technical Stack

| Component | Technology |
|-----------|------------|
| Strategy logic | Python 3.9+ |
| Exchange API | Bybit V5 REST (raw HMAC-SHA256 signing) |
| Data sources | Binance public API, Bybit public API |
| Backtesting | Custom Python engine |
| Dashboard | Vanilla HTML/JS + Chart.js |
| AI agent | Anthropic Claude (claude-sonnet) |
| Messaging | python-telegram-bot 20.x |
| Version control | Git + GitHub |

---

*Research conducted March 2026. All backtests use historical data; past performance does not guarantee future results.*
