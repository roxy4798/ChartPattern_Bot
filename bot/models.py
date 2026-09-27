from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional
from enum import Enum


class Direction(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class SignalStatus(str, Enum):
    ACTIVE = "ACTIVE"
    TP1_HIT = "TP1_HIT"
    TP2_HIT = "TP2_HIT"
    TP3_HIT = "TP3_HIT"
    SL_HIT = "SL_HIT"
    CLOSED = "CLOSED"


@dataclass
class Pivot:
    price: float
    index: int  # absolute integer index into the candle series (not bar_index offset)


@dataclass
class PatternResult:
    """One detected chart pattern, geometry included for chart rendering."""
    name: str
    is_bullish: bool
    start_idx: int
    breakout_idx: int
    entry_price: float
    stop_price: float
    target_price: float  # full measured-move target (used to derive TP1/2/3)
    # trendline / neckline geometry as (x1, y1, x2, y2) pairs, index-based.
    upper_line: Optional[tuple] = None
    lower_line: Optional[tuple] = None
    fill_upper: Optional[tuple] = None
    fill_lower: Optional[tuple] = None


@dataclass
class Signal:
    id: str
    symbol: str
    timeframe: str
    pattern: str
    direction: Direction
    entry_price: float
    stop_loss: float
    tp1: float
    tp2: float
    tp3: float
    price_at_signal: float
    signal_time: str  # ISO8601 UTC
    ema200_at_signal: Optional[float] = None
    status: SignalStatus = SignalStatus.ACTIVE
    tp1_hit_time: Optional[str] = None
    tp2_hit_time: Optional[str] = None
    tp3_hit_time: Optional[str] = None
    sl_hit_time: Optional[str] = None
    exit_price: Optional[float] = None
    exit_time: Optional[str] = None
    result: Optional[str] = None  # "WIN" | "LOSS"
    pnl_pct: Optional[float] = None
    duration_sec: Optional[int] = None
    telegram_message_id: Optional[int] = None
    telegram_chart_message_id: Optional[int] = None
    created_at: Optional[str] = None
    scan_id: Optional[str] = None

