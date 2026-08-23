from __future__ import annotations
import logging
import uuid
from datetime import datetime, timezone
import pandas as pd

from bot.config import settings
from bot.indicators import ema, atr, find_pivots
from bot.patterns import detect_all
from bot.models import Signal, Direction, SignalStatus, PatternResult
from bot.chart_generator import describe_confirmation

log = logging.getLogger("signal_engine")


def _validate_signal(signal: Signal, pattern: PatternResult) -> None:
    prices = (signal.entry_price, signal.stop_loss, signal.tp1, signal.tp2, signal.tp3,
              signal.ema200_at_signal, signal.price_at_signal)
    if not all(pd.notna(price) and pd.api.types.is_number(price) for price in prices):
        raise ValueError("Signal contains a non-numeric price")
    if signal.direction == Direction.LONG:
        valid_order = signal.stop_loss < signal.entry_price < signal.tp1 <= signal.tp2 <= signal.tp3
    else:
        valid_order = signal.stop_loss > signal.entry_price > signal.tp1 >= signal.tp2 >= signal.tp3
    if not valid_order:
        raise ValueError(f"Invalid {signal.direction.value} entry/SL/TP ordering for {signal.symbol} {signal.timeframe}")
    if signal.pattern != pattern.name or signal.direction != (
        Direction.LONG if pattern.is_bullish else Direction.SHORT
    ):
        raise ValueError("Signal identity does not match detected pattern")


def _reason(pattern: PatternResult, ema200: float, price: float, candle: pd.Series) -> str:
    side = "above" if price > ema200 else "below"
    bias = "Bullish" if pattern.is_bullish else "Bearish"
    confirmation = describe_confirmation(candle, pattern.is_bullish)
    return (f"{bias} {pattern.name} confirmed by a closed-candle breakout, "
            f"with price trading {side} EMA200 ({price:.6g} vs {ema200:.6g}), "
            f"aligning trend and structure for this direction. "
            f"Confirmation (informational only): {confirmation}.")


def evaluate_symbol_timeframe(symbol: str, timeframe: str, df: pd.DataFrame) -> tuple[Signal | None, PatternResult | None, str | None]:
    """Runs the full pipeline for one symbol/timeframe on already-closed
    candles. Returns (Signal or None, PatternResult or None, reason_text)."""
    if len(df) < max(settings.ema_period + 5, settings.lb_left + settings.lb_right + 20):
        return None, None, None

    df = df.reset_index(drop=True)
    ema200 = ema(df["close"], settings.ema_period)
    atr_series = atr(df, settings.atr_period)
    pivot_highs, pivot_lows = find_pivots(df, settings.lb_left, settings.lb_right)

    if len(pivot_highs) < 2 or len(pivot_lows) < 2:
        return None, None, None

    pattern = detect_all(df, atr_series, pivot_highs, pivot_lows)
    if pattern is None:
        return None, None, None

    last_close = float(df["close"].iloc[-1])
    ema_last = float(ema200.iloc[-1])

    # --- EMA200 trend filter (the core rule from the spec) ---
    if pattern.is_bullish and not (last_close > ema_last):
        log.debug("%s %s: bullish %s rejected, price below EMA200", symbol, timeframe, pattern.name)
        return None, pattern, None
    if not pattern.is_bullish and not (last_close < ema_last):
        log.debug("%s %s: bearish %s rejected, price above EMA200", symbol, timeframe, pattern.name)
        return None, pattern, None

    direction = Direction.LONG if pattern.is_bullish else Direction.SHORT
    entry = pattern.entry_price
    stop = pattern.stop_price
    full_target = pattern.target_price
    move = full_target - entry  # signed: positive for LONG targets, negative for SHORT

    tp1 = entry + move * settings.tp1_fraction
    tp2 = entry + move * settings.tp2_fraction
    tp3 = entry + move * settings.tp3_fraction

    signal = Signal(
        id=str(uuid.uuid4()),
        symbol=symbol,
        timeframe=timeframe,
        pattern=pattern.name,
        direction=direction,
        entry_price=entry,
        stop_loss=stop,
        tp1=tp1, tp2=tp2, tp3=tp3,
        ema200_at_signal=ema_last,
        price_at_signal=last_close,
        signal_time=datetime.now(timezone.utc).isoformat(),
        status=SignalStatus.ACTIVE,
    )
    _validate_signal(signal, pattern)
    reason = _reason(pattern, ema_last, last_close, df.iloc[-1])
    return signal, pattern, reason
