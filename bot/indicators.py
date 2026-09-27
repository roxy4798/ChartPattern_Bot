from __future__ import annotations
import numpy as np
import pandas as pd
from bot.models import Pivot


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


def find_multiscale_pivots(
    df: pd.DataFrame,
    major_lb: int = 15,
    medium_lb: int = 8,
) -> dict[str, tuple[list[Pivot], list[Pivot]]]:
    """Extract both major and medium swing pivots for multi-scale trendline fitting.
    Returns dict with keys 'major' and 'medium', each containing (pivot_highs, pivot_lows)
    ordered newest-first. Zero look-ahead bias.
    """
    major_ph, major_pl = find_pivots(df, major_lb, major_lb)
    medium_ph, medium_pl = find_pivots(df, medium_lb, medium_lb)
    return {
        "major": (major_ph, major_pl),
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

