from __future__ import annotations
from typing import Optional
import numpy as np
import pandas as pd
from bot.models import Pivot
from bot.config import settings


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def find_pivots(df: pd.DataFrame, lb_left: int, lb_right: int) -> tuple[list[Pivot], list[Pivot]]:
    """
    Same definition as the Pine Script: a pivot high at index i is the
    highest high in [i-lb_left, i+lb_right]; symmetric for pivot lows.
    Only indices with a full lb_right of confirmation are eligible (i.e. we
    only look up to len-1-lb_right), so pivots never repaint.
    Returns pivots newest-first, matching the Pine `unshift` history arrays.
    """
    highs = df["high"].values
    lows = df["low"].values
    n = len(df)
    pivot_highs: list[Pivot] = []
    pivot_lows: list[Pivot] = []

    last_valid = n - 1 - lb_right
    for i in range(lb_left, last_valid + 1):
        window_h = highs[i - lb_left: i + lb_right + 1]
        if highs[i] == window_h.max() and (window_h == highs[i]).sum() == 1:
            pivot_highs.append(Pivot(price=float(highs[i]), index=i))
        window_l = lows[i - lb_left: i + lb_right + 1]
        if lows[i] == window_l.min() and (window_l == lows[i]).sum() == 1:
            pivot_lows.append(Pivot(price=float(lows[i]), index=i))

    pivot_highs.reverse()  # newest first
    pivot_lows.reverse()
    return pivot_highs, pivot_lows


def find_structural_pivots(
    df: pd.DataFrame,
    fractal_period: Optional[int] = None,
    confirmation_bars: int = 20,
    min_pivot_dist: int = 20,
) -> tuple[list[Pivot], list[Pivot]]:
    """Extracts major structural pivots using a 30 fractal period.

    Pipeline:
    1. A structural pivot high at index i must be the dominant peak across
       at least `fractal_period` (30) bars in its left neighborhood, confirmed
       by `confirmation_bars` (e.g. 20) subsequent bars.
    2. Cluster suppression: pivots within `min_pivot_dist` are filtered to keep
       only the macro structural extreme (highest high / lowest low).
    3. Strictly zero look-ahead bias (evaluates only closed history up to len(df) - 1 - confirmation_bars).
    4. Returns (pivot_highs, pivot_lows) ordered newest-first.
    """
    if fractal_period is None:
        fractal_period = settings.structural_fractal_period
    if fractal_period <= 0 or confirmation_bars < 0:
        raise ValueError("fractal_period must be positive and confirmation_bars non-negative")

    highs = df["high"].values
    lows = df["low"].values
    n = len(df)

    last_valid = n - 1 - confirmation_bars
    if last_valid < fractal_period:
        return [], []

    raw_highs: list[Pivot] = []
    raw_lows: list[Pivot] = []

    for i in range(fractal_period, last_valid + 1):
        window_h = highs[i - fractal_period : i + confirmation_bars + 1]
        if highs[i] == window_h.max() and (window_h == highs[i]).sum() == 1:
            raw_highs.append(Pivot(price=float(highs[i]), index=i))
        window_l = lows[i - fractal_period : i + confirmation_bars + 1]
        if lows[i] == window_l.min() and (window_l == lows[i]).sum() == 1:
            raw_lows.append(Pivot(price=float(lows[i]), index=i))

    def _suppress_clusters(pivots: list[Pivot], is_high: bool) -> list[Pivot]:
        if not pivots:
            return []
        filtered = [pivots[0]]
        for p in pivots[1:]:
            prev = filtered[-1]
            if p.index - prev.index < min_pivot_dist:
                if (is_high and p.price > prev.price) or (not is_high and p.price < prev.price):
                    filtered[-1] = p
            else:
                filtered.append(p)
        return filtered

    clean_highs = _suppress_clusters(raw_highs, True)
    clean_lows = _suppress_clusters(raw_lows, False)

    clean_highs.reverse()  # newest first
    clean_lows.reverse()
    return clean_highs, clean_lows


def find_multiscale_pivots(
    df: pd.DataFrame,
    major_lb: Optional[int] = None,
    medium_lb: int = 25,
) -> dict[str, tuple[list[Pivot], list[Pivot]]]:
    """Extract both 30-fractal structural and medium swing pivots for multi-scale trendline fitting.
    Returns dict with keys 'structural', 'major' and 'medium', each containing (pivot_highs, pivot_lows)
    ordered newest-first. Zero look-ahead bias.
    """
    if major_lb is None:
        major_lb = settings.structural_fractal_period
    struct_ph, struct_pl = find_structural_pivots(df, fractal_period=major_lb)
    medium_ph, medium_pl = find_pivots(df, medium_lb, min(medium_lb, 15))
    return {
        "structural": (struct_ph, struct_pl),
        "major": (struct_ph, struct_pl),
        "medium": (medium_ph, medium_pl),
    }


def project(x1: float, y1: float, x2: float, y2: float, target_x: float) -> float:
    if x2 == x1 or not np.isfinite(x1) or not np.isfinite(x2):
        return float(y1)
    return float(y1 + ((y2 - y1) / (x2 - x1)) * (target_x - x1))


def is_near(v1: float, v2: float, tol_pct: float) -> bool:
    if v1 is None or v2 is None:
        return False
    if not (np.isfinite(v1) and np.isfinite(v2)):
        return False
    denom = (abs(v1) + abs(v2)) / 2
    if denom == 0:
        return True
    return abs(v1 - v2) <= denom * tol_pct

