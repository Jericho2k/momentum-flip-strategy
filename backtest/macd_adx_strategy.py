"""
MACD Histogram + ADX Flip Strategy
===================================
Core strategy logic — no I/O, no exchange calls.
Designed to be called by the backtester and live bridge alike.

Logic summary:
  - MACD histogram crosses above 0  → go LONG  (or flip from SHORT)
  - MACD histogram crosses below 0  → go SHORT (or flip from LONG)
  - ADX filter: only take signals when ADX >= adx_level (strong trend)
  - Histogram pip filter: |histogram| must exceed min_hist_pips to avoid noise
  - Morning stop: close/reverse position at session open if configured
  - Position is always LONG, SHORT, or FLAT (flat only during session gaps)
"""

from dataclasses import dataclass, field
from typing import Optional
import math


# ── Parameters ─────────────────────────────────────────────────────────────────

@dataclass
class StrategyParams:
    # MACD
    macd_fast:   int   = 8
    macd_slow:   int   = 21
    macd_signal: int   = 9
    min_hist_pips: float = 0.1   # minimum histogram value to take signal

    # ADX
    adx_period: int   = 14
    adx_level:  float = 25.0    # only trade when ADX >= this

    # Session filter (UTC hours, set None to disable)
    session_start: Optional[int] = None
    session_end:   Optional[int] = None
    morning_stop:  bool = False

    # Risk
    sl_pips:      float = 0.0          # hard stop in price units (0 = disabled)
    leverage:     int   = 2


# ── Indicator calculations ─────────────────────────────────────────────────────

def ema_series(prices: list[float], period: int) -> list[float]:
    if len(prices) < period:
        return [float('nan')] * len(prices)
    k = 2.0 / (period + 1)
    out = [float('nan')] * (period - 1)
    out.append(sum(prices[:period]) / period)
    for p in prices[period:]:
        out.append(p * k + out[-1] * (1 - k))
    return out


def macd_series(closes: list[float], fast: int, slow: int, signal: int) -> dict:
    """Returns dict of lists: macd_line, signal_line, histogram."""
    fast_ema = ema_series(closes, fast)
    slow_ema = ema_series(closes, slow)

    macd_line = [
        (f - s) if not (math.isnan(f) or math.isnan(s)) else float('nan')
        for f, s in zip(fast_ema, slow_ema)
    ]

    # Signal EMA only on valid macd values
    valid_start = next((i for i, v in enumerate(macd_line) if not math.isnan(v)), len(macd_line))
    signal_line = [float('nan')] * len(macd_line)
    valid_macd = [v for v in macd_line if not math.isnan(v)]
    if len(valid_macd) >= signal:
        sig_ema = ema_series(valid_macd, signal)
        for i, idx in enumerate(range(valid_start, len(macd_line))):
            signal_line[idx] = sig_ema[i] if i < len(sig_ema) else float('nan')

    histogram = [
        (m - s) if not (math.isnan(m) or math.isnan(s)) else float('nan')
        for m, s in zip(macd_line, signal_line)
    ]

    return {"macd": macd_line, "signal": signal_line, "histogram": histogram}


def adx_series(highs: list[float], lows: list[float], closes: list[float], period: int) -> list[float]:
    """Wilder-smoothed ADX."""
    n = len(closes)
    adx_out = [float('nan')] * n
    if n < period * 2 + 1:
        return adx_out

    tr_list, pdm_list, ndm_list = [], [], []
    for i in range(1, n):
        tr  = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
        pdm = max(highs[i] - highs[i-1], 0) if (highs[i] - highs[i-1]) > (lows[i-1] - lows[i]) else 0
        ndm = max(lows[i-1] - lows[i], 0) if (lows[i-1] - lows[i]) > (highs[i] - highs[i-1]) else 0
        tr_list.append(tr); pdm_list.append(pdm); ndm_list.append(ndm)

    def wilder_smooth(data, p):
        out = [float('nan')] * (p - 1)
        out.append(sum(data[:p]))
        for v in data[p:]:
            out.append(out[-1] - out[-1] / p + v)
        return out

    atr_s  = wilder_smooth(tr_list,  period)
    pdm_s  = wilder_smooth(pdm_list, period)
    ndm_s  = wilder_smooth(ndm_list, period)

    dx_list = []
    for a, p, nd in zip(atr_s, pdm_s, ndm_s):
        if math.isnan(a) or a == 0:
            dx_list.append(float('nan'))
            continue
        pdi = 100 * p / a
        ndi = 100 * nd / a
        denom = pdi + ndi
        dx_list.append(100 * abs(pdi - ndi) / denom if denom else 0)

    # ADX = smoothed DX
    valid_dx = [(i, v) for i, v in enumerate(dx_list) if not math.isnan(v)]
    if len(valid_dx) < period:
        return adx_out

    start_i = valid_dx[period - 1][0]
    adx_val  = sum(v for _, v in valid_dx[:period]) / period
    adx_out[start_i + 1] = adx_val   # +1 offset for the tr/dm shift

    for i, v in valid_dx[period:]:
        adx_val = (adx_val * (period - 1) + v) / period
        if i + 1 < n:
            adx_out[i + 1] = adx_val

    return adx_out


# ── Signal generation ──────────────────────────────────────────────────────────

@dataclass
class Bar:
    timestamp: int    # unix seconds
    open:  float
    high:  float
    low:   float
    close: float
    volume: float = 0.0


@dataclass
class Signal:
    bar_index: int
    timestamp: int
    action:    str        # 'LONG', 'SHORT', 'CLOSE', 'MORNING_STOP'
    price:     float
    reason:    str
    hist_value: float = 0.0
    adx_value:  float = 0.0


def in_session(ts: int, params: StrategyParams) -> bool:
    """Check if timestamp falls within the trading session."""
    if params.session_start is None or params.session_end is None:
        return True
    from datetime import datetime, timezone
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    h  = dt.hour
    if params.session_start < params.session_end:
        return params.session_start <= h < params.session_end
    else:  # wraps midnight
        return h >= params.session_start or h < params.session_end


def is_session_open(ts: int, prev_ts: int, params: StrategyParams) -> bool:
    """Detect the first bar of a new session."""
    if not params.morning_stop or params.session_start is None:
        return False
    return (not in_session(prev_ts, params)) and in_session(ts, params)


def generate_signals(bars: list[Bar], params: StrategyParams) -> list[Signal]:
    """
    Walk through bars and generate trading signals.
    Returns list of Signal objects.
    """
    closes = [b.close for b in bars]
    highs  = [b.high  for b in bars]
    lows   = [b.low   for b in bars]

    macd   = macd_series(closes, params.macd_fast, params.macd_slow, params.macd_signal)
    hist   = macd["histogram"]
    adx    = adx_series(highs, lows, closes, params.adx_period)

    signals   = []
    position  = None   # None, 'LONG', 'SHORT'
    warmup    = max(params.macd_slow + params.macd_signal, params.adx_period * 2) + 5

    for i in range(warmup, len(bars)):
        bar      = bars[i]
        prev_bar = bars[i - 1]
        h_now    = hist[i]
        h_prev   = hist[i - 1]
        adx_now  = adx[i]

        if math.isnan(h_now) or math.isnan(h_prev) or math.isnan(adx_now):
            continue

        price = bar.close

        # Morning stop — close/reverse at session open
        if params.morning_stop and is_session_open(bar.timestamp, prev_bar.timestamp, params):
            if position is not None:
                new_pos = 'SHORT' if position == 'LONG' else 'LONG'
                signals.append(Signal(i, bar.timestamp, new_pos, price,
                                      "morning_stop_reverse", h_now, adx_now))
                position = new_pos
            continue

        # Only trade in session
        if not in_session(bar.timestamp, params):
            if position is not None:
                signals.append(Signal(i, bar.timestamp, 'CLOSE', price,
                                      "session_end", h_now, adx_now))
                position = None
            continue

        # ADX filter
        if adx_now < params.adx_level:
            continue

        # Histogram pip filter
        if abs(h_now) < params.min_hist_pips:
            continue

        # MACD histogram crossover
        cross_up   = h_prev <= 0 and h_now > 0
        cross_down = h_prev >= 0 and h_now < 0

        if cross_up and position != 'LONG':
            signals.append(Signal(i, bar.timestamp, 'LONG', price,
                                  "macd_hist_cross_up", h_now, adx_now))
            position = 'LONG'

        elif cross_down and position != 'SHORT':
            signals.append(Signal(i, bar.timestamp, 'SHORT', price,
                                  "macd_hist_cross_down", h_now, adx_now))
            position = 'SHORT'

    return signals
