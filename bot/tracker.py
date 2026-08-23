"""
Tracks every ACTIVE/partially-filled signal against subsequently CLOSED
candles until it resolves at TP3 or SL. Re-derives status from full OHLC
history on every tick (idempotent), so a restart just resumes correctly
from whatever is already persisted in the database - no in-memory state
required.

Ordering assumption: if a single candle's range touches BOTH the next
target and the stop loss, we conservatively assume the stop was hit first.
This avoids overstating win rate when intra-candle order is unknown from
OHLC alone.
"""
from __future__ import annotations
import logging
from datetime import datetime, timezone
import pandas as pd

from bot.config import settings
from bot.models import Signal, SignalStatus, Direction
from bot.database import Database
from bot.binance_client import BinanceMarket
from bot.telegram_bot import Notifier

log = logging.getLogger("tracker")

TARGET_ORDER = [
    (SignalStatus.TP1_HIT, "tp1"),
    (SignalStatus.TP2_HIT, "tp2"),
    (SignalStatus.TP3_HIT, "tp3"),
]

STATUS_RANK = {
    SignalStatus.ACTIVE: 0,
    SignalStatus.TP1_HIT: 1,
    SignalStatus.TP2_HIT: 2,
    SignalStatus.TP3_HIT: 3,
    SignalStatus.SL_HIT: 99,
    SignalStatus.CLOSED: 100,
}


def _pnl_pct(entry: float, exit_price: float, direction: Direction) -> float:
    raw = ((exit_price - entry) / entry) if direction == Direction.LONG else ((entry - exit_price) / entry)
    fees = settings.taker_fee_pct * 2
    return (raw - fees) * 100


async def check_signal(market: BinanceMarket, db: Database, notifier: Notifier, signal: Signal):
    df = await market.get_klines(signal.symbol, signal.timeframe, limit=settings.candles_lookback)
    if df.empty:
        return
    df = df[df["close_time"] >= pd.Timestamp(signal.signal_time)].reset_index(drop=True)
    if df.empty:
        return

    is_long = signal.direction == Direction.LONG
    status = signal.status
    remaining_idx = STATUS_RANK[status]  # how far we've already progressed

    for _, candle in df.iterrows():
        if status in (SignalStatus.SL_HIT, SignalStatus.CLOSED):
            break

        sl_touched = (candle["low"] <= signal.stop_loss) if is_long else (candle["high"] >= signal.stop_loss)

        next_target_status, next_target_field = None, None
        for st, field_name in TARGET_ORDER:
            if STATUS_RANK[st] > STATUS_RANK[status]:
                next_target_status, next_target_field = st, field_name
                break

        target_touched = False
        if next_target_field:
            target_price = getattr(signal, next_target_field)
            target_touched = (candle["high"] >= target_price) if is_long else (candle["low"] <= target_price)

        candle_time = candle["close_time"].isoformat()

        if sl_touched and target_touched:
            # Ambiguous ordering within one candle: assume SL first (conservative).
            status = SignalStatus.SL_HIT
            await _resolve_sl(db, notifier, signal, candle_time)
            break
        elif sl_touched:
            status = SignalStatus.SL_HIT
            await _resolve_sl(db, notifier, signal, candle_time)
            break
        elif target_touched:
            status = next_target_status
            price = getattr(signal, next_target_field)
            await db.update_status(signal.id, status, hit_time=candle_time)
            signal.status = status
            if status == SignalStatus.TP3_HIT:
                await _resolve_tp3(db, notifier, signal, candle_time, price)
                status = SignalStatus.CLOSED
                break
            else:
                await notifier.send_update(signal, status.value, price)
            # continue scanning subsequent candles for further hits


async def _resolve_sl(db: Database, notifier: Notifier, signal: Signal, hit_time: str):
    pnl = _pnl_pct(signal.entry_price, signal.stop_loss, signal.direction)
    duration = int((pd.Timestamp(hit_time) - pd.Timestamp(signal.signal_time)).total_seconds())
    await db.update_status(
        signal.id, SignalStatus.SL_HIT, hit_time=hit_time, exit_price=signal.stop_loss,
        exit_time=hit_time, result="LOSS", pnl_pct=pnl, duration_sec=duration,
    )
    signal.status = SignalStatus.SL_HIT
    signal.exit_price = signal.stop_loss
    signal.pnl_pct = pnl
    signal.duration_sec = duration
    await notifier.send_update(signal, "SL_HIT", signal.stop_loss)
    await notifier.send_closed(signal, "LOSS")


async def _resolve_tp3(db: Database, notifier: Notifier, signal: Signal, hit_time: str, price: float):
    pnl = _pnl_pct(signal.entry_price, price, signal.direction)
    duration = int((pd.Timestamp(hit_time) - pd.Timestamp(signal.signal_time)).total_seconds())
    await db.update_status(
        signal.id, SignalStatus.CLOSED, exit_price=price, exit_time=hit_time,
        result="WIN", pnl_pct=pnl, duration_sec=duration,
    )
    signal.status = SignalStatus.CLOSED
    signal.exit_price = price
    signal.pnl_pct = pnl
    signal.duration_sec = duration
    await notifier.send_closed(signal, "WIN")


async def run_tracker_once(market: BinanceMarket, db: Database, notifier: Notifier):
    active = await db.get_active_signals()
    for signal in active:
        try:
            await check_signal(market, db, notifier, signal)
        except Exception:
            log.exception("Tracker failed for %s %s (%s)", signal.symbol, signal.timeframe, signal.id)
