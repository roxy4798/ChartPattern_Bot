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
import asyncio
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

TF_SECONDS = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "2h": 7200,
    "4h": 14400,
    "6h": 21600,
    "8h": 28800,
    "12h": 43200,
    "1d": 86400,
    "3d": 259200,
    "1w": 604800,
}


def _pnl_pct(entry: float, exit_price: float, direction: Direction) -> float:
    raw = ((exit_price - entry) / entry) if direction == Direction.LONG else ((entry - exit_price) / entry)
    fees = settings.taker_fee_pct * 2
    return (raw - fees) * 100


def _calc_required_lookback(signal: Signal) -> int:
    try:
        sig_dt = pd.to_datetime(signal.signal_time, utc=True)
        now_dt = datetime.now(timezone.utc)
        elapsed_sec = max(0, int((now_dt - sig_dt).total_seconds()))
        tf_sec = TF_SECONDS.get(signal.timeframe.lower(), 900)
        bars_elapsed = elapsed_sec // tf_sec
        return max(5, min(settings.candles_lookback, int(bars_elapsed + 10)))
    except Exception:
        return settings.candles_lookback


async def check_signal(market: BinanceMarket, db: Database, notifier: Notifier, signal: Signal):
    lookback = _calc_required_lookback(signal)
    df = await market.get_klines(signal.symbol, signal.timeframe, limit=lookback)
    if df.empty:
        return

    sig_time_ts = pd.to_datetime(signal.signal_time, utc=True)
    df = df[df["close_time"] >= sig_time_ts].reset_index(drop=True)
    if df.empty:
        return

    is_long = signal.direction == Direction.LONG
    current_status = signal.status

    for _, candle in df.iterrows():
        if current_status in (SignalStatus.SL_HIT, SignalStatus.CLOSED):
            break

        sl_touched = (candle["low"] <= signal.stop_loss) if is_long else (candle["high"] >= signal.stop_loss)

        next_target_status, next_target_field = None, None
        for st, field_name in TARGET_ORDER:
            if STATUS_RANK[st] > STATUS_RANK[current_status]:
                next_target_status, next_target_field = st, field_name
                break

        target_touched = False
        if next_target_field:
            target_price = getattr(signal, next_target_field)
            target_touched = (candle["high"] >= target_price) if is_long else (candle["low"] <= target_price)

        candle_close = candle["close_time"]
        candle_time = candle_close.isoformat() if hasattr(candle_close, "isoformat") else str(candle_close)

        if sl_touched and target_touched:
            # Ambiguous ordering within one candle: assume SL first (conservative).
            await _resolve_sl(db, notifier, signal, candle_time, current_status)
            break
        elif sl_touched:
            await _resolve_sl(db, notifier, signal, candle_time, current_status)
            break
        elif target_touched and next_target_status and next_target_field:
            status = next_target_status
            price = float(getattr(signal, next_target_field))
            updated = await db.update_status(
                signal.id, status, expected_status=current_status, hit_time=candle_time,
                notifications=[{
                    "event_key": f"{signal.id}:{status.value}:{candle_time}",
                    "kind": "update",
                    "payload": {"event": status.value, "price": price},
                }],
            )
            if not updated:
                break
            log.info("🎯 TARGET HIT: %s (%s) → %s @ %.6g",
                     signal.symbol, signal.timeframe.upper(), status.value.replace('_', ' '), price)
            current_status = status
            signal.status = status
            if status == SignalStatus.TP3_HIT:
                await _resolve_tp3(db, notifier, signal, candle_time, price, SignalStatus.TP3_HIT)
                break


async def _resolve_sl(db: Database, notifier: Notifier, signal: Signal, hit_time: str,
                      expected_status: SignalStatus) -> bool:
    pnl = _pnl_pct(signal.entry_price, signal.stop_loss, signal.direction)
    sig_ts = pd.to_datetime(signal.signal_time, utc=True)
    hit_ts = pd.to_datetime(hit_time, utc=True)
    duration = max(0, int((hit_ts - sig_ts).total_seconds()))

    updated = await db.update_status(
        signal.id, SignalStatus.SL_HIT, hit_time=hit_time, exit_price=signal.stop_loss,
        exit_time=hit_time, result="LOSS", pnl_pct=pnl, duration_sec=duration,
        expected_status=expected_status,
        notifications=[
            {"event_key": f"{signal.id}:SL_HIT:{hit_time}", "kind": "update",
             "payload": {"event": "SL_HIT", "price": signal.stop_loss}},
            {"event_key": f"{signal.id}:CLOSED:LOSS:{hit_time}", "kind": "closed",
             "payload": {"result": "LOSS"}},
        ],
    )
    if not updated:
        return False
    log.info("🛑 STOP LOSS HIT: %s (%s) @ %.6g │ Result: LOSS (PnL: %+.2f%%)",
             signal.symbol, signal.timeframe.upper(), signal.stop_loss, pnl)
    signal.status = SignalStatus.SL_HIT
    signal.exit_price = signal.stop_loss
    signal.pnl_pct = pnl
    signal.duration_sec = duration
    return True


async def _resolve_tp3(db: Database, notifier: Notifier, signal: Signal, hit_time: str,
                       price: float, expected_status: SignalStatus):
    pnl = _pnl_pct(signal.entry_price, price, signal.direction)
    sig_ts = pd.to_datetime(signal.signal_time, utc=True)
    hit_ts = pd.to_datetime(hit_time, utc=True)
    duration = max(0, int((hit_ts - sig_ts).total_seconds()))

    updated = await db.update_status(
        signal.id, SignalStatus.CLOSED, exit_price=price, exit_time=hit_time,
        result="WIN", pnl_pct=pnl, duration_sec=duration, expected_status=expected_status,
        notifications=[{
            "event_key": f"{signal.id}:CLOSED:WIN:{hit_time}",
            "kind": "closed", "payload": {"result": "WIN"},
        }],
    )
    if not updated:
        return
    log.info("🏆 FINAL TARGET HIT: %s (%s) @ %.6g │ Result: WIN (PnL: %+.2f%%)",
             signal.symbol, signal.timeframe.upper(), price, pnl)
    signal.status = SignalStatus.CLOSED
    signal.exit_price = price
    signal.pnl_pct = pnl
    signal.duration_sec = duration



async def run_tracker_once(market: BinanceMarket, db: Database, notifier: Notifier):
    active = await db.get_active_signals()
    if not active:
        await db.set_metadata("last_tracker_at", datetime.now(timezone.utc).isoformat())
        await db.set_metadata("tracker_active_count", "0")
        await notifier.flush_notifications(db)
        return

    log.debug("Tracker evaluating %d active signal(s)...", len(active))
    sem = asyncio.Semaphore(settings.max_tracker_concurrency)

    async def _safe_check(sig: Signal):
        async with sem:
            try:
                await check_signal(market, db, notifier, sig)
            except Exception:
                log.exception("[%s] Tracker failed for %s %s", sig.id[:8], sig.symbol, sig.timeframe)

    await asyncio.gather(*(_safe_check(s) for s in active))
    await db.set_metadata("last_tracker_at", datetime.now(timezone.utc).isoformat())
    await db.set_metadata("tracker_active_count", str(len(active)))
    await notifier.flush_notifications(db)

