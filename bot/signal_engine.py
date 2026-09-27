from __future__ import annotations
import logging
import uuid
import numpy as np
import pandas as pd
from typing import Optional

from bot.config import settings
from bot.indicators import atr, find_pivots, find_structural_pivots
from bot.patterns import detect_all
from bot.models import Signal, Direction, SignalStatus, PatternResult
from bot.chart_generator import describe_confirmation

log = logging.getLogger("signal_engine")


def _validate_signal(signal: Signal, pattern: PatternResult) -> None:
    prices = (
        signal.entry_price, signal.stop_loss, signal.tp1, signal.tp2, signal.tp3,
        signal.price_at_signal
    )
    if not all(np.isfinite(p) and p > 0 for p in prices):
        raise ValueError(f"Signal for {signal.symbol} {signal.timeframe} contains non-positive or non-finite prices")

    if signal.direction == Direction.LONG:
        valid_order = signal.stop_loss < signal.entry_price < signal.tp1 < signal.tp2 < signal.tp3
    else:
        valid_order = signal.stop_loss > signal.entry_price > signal.tp1 > signal.tp2 > signal.tp3

    if not valid_order:
        raise ValueError(
            f"Invalid {signal.direction.value} entry/SL/TP ordering for {signal.symbol} {signal.timeframe}: "
            f"SL={signal.stop_loss}, Entry={signal.entry_price}, TP1={signal.tp1}, TP2={signal.tp2}, TP3={signal.tp3}"
        )

    if signal.pattern != pattern.name:
        raise ValueError(f"Signal pattern {signal.pattern} does not match detected pattern {pattern.name}")
    
    expected_dir = Direction.LONG if pattern.is_bullish else Direction.SHORT
    if signal.direction != expected_dir:
        raise ValueError(f"Signal direction {signal.direction} does not match pattern bias {expected_dir}")


def _reason(pattern: PatternResult, price: float, candle: pd.Series) -> str:
    confirmation = describe_confirmation(candle, pattern.is_bullish)
    return (f"Bullish {pattern.name} confirmed by a closed-candle structural breakout "
            f"at {price:.6g}. "
            f"Confirmation: {confirmation}.")


def evaluate_symbol_timeframe(
    symbol: str,
    timeframe: str,
    df: pd.DataFrame,
    scan_id: Optional[str] = None,
) -> tuple[Signal | None, PatternResult | None, str | None]:
    """Runs the full pipeline for one symbol/timeframe on already-closed
    candles. LONG ONLY. Returns (Signal or None, PatternResult or None, reason_text)."""
    min_required_bars = max(settings.structural_fractal_period + 30, 100)
    if df is None or len(df) < min_required_bars:
        return None, None, None

    df = df.reset_index(drop=True).copy()
    atr_series = atr(df, settings.atr_period)
    pivot_highs, pivot_lows = find_structural_pivots(
        df,
        fractal_period=settings.structural_fractal_period,
        min_pivot_dist=settings.trendline_min_pivot_dist,
    )

    if len(pivot_highs) < 2 or len(pivot_lows) < 2:
        return None, None, None

    pattern = detect_all(df, atr_series, pivot_highs, pivot_lows)
    if pattern is None or not pattern.is_bullish:
        # STRATEGY: LONG ONLY - Reject any missing or non-bullish pattern
        return None, None, None

    last_close = float(df["close"].iloc[-1])

    direction = Direction.LONG
    entry = pattern.entry_price
    stop = pattern.stop_price
    risk = abs(entry - stop)

    if settings.use_r_multiple_tp:
        # Proportional TP1, TP2, TP3 scaled directly from SL Risk distance (1R, 2R, 3R)
        if pattern.is_bullish:
            tp1 = entry + risk * settings.tp1_r_multiple
            tp2 = entry + risk * settings.tp2_r_multiple
            tp3 = entry + risk * settings.tp3_r_multiple
        else:
            tp1 = entry - risk * settings.tp1_r_multiple
            tp2 = entry - risk * settings.tp2_r_multiple
            tp3 = entry - risk * settings.tp3_r_multiple
    else:
        full_target = pattern.target_price
        move = full_target - entry  # signed: positive for LONG targets, negative for SHORT
        tp1 = entry + move * settings.tp1_fraction
        tp2 = entry + move * settings.tp2_fraction
        tp3 = entry + move * settings.tp3_fraction


    # Single source of truth for signal time is the close timestamp of the confirming breakout candle
    breakout_candle = df.iloc[-1]
    ts_val = breakout_candle["close_time"] if "close_time" in breakout_candle else breakout_candle.get("open_time", datetime.now(timezone.utc))
    candle_close_time = ts_val.isoformat() if hasattr(ts_val, "isoformat") else str(ts_val)

    signal = Signal(
        id=str(uuid.uuid4()),
        symbol=symbol,
        timeframe=timeframe,
        pattern=pattern.name,
        direction=direction,
        entry_price=entry,
        stop_loss=stop,
        tp1=tp1, tp2=tp2, tp3=tp3,
        price_at_signal=last_close,
        signal_time=candle_close_time,
        status=SignalStatus.ACTIVE,
        scan_id=scan_id,
    )
    _validate_signal(signal, pattern)
    reason = _reason(pattern, last_close, breakout_candle)
    return signal, pattern, reason

