"""
Long-Structural Pattern Detectors & Trendline Engine (LONG ONLY).

Designed for multi-timeframe analysis (1D, 3D, 1W) following TradingView
structural chart patterns (macro swing anchors, multiple touches, zero
look-ahead bias, clean projection into the right zone).
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, List
import numpy as np
import pandas as pd
from bot.models import Pivot, PatternResult
from bot.indicators import project, is_near
from bot.config import settings


@dataclass
class StructuralTrendline:
    origin_idx: int
    origin_price: float
    end_idx: int
    end_price: float
    slope: float
    intercept: float
    touches: int
    touch_indices: List[int]
    span_bars: int
    violations: int


def fit_structural_trendline(
    df: pd.DataFrame,
    pivots: list[Pivot],
    is_upper: bool,
    atr_series: pd.Series,
    min_span: Optional[int] = None,
    min_pivot_dist: Optional[int] = None,
    max_span: int = 500,
) -> Optional[StructuralTrendline]:
    """Fits the strongest structural trendline through 65-fractal swing pivots.

    Evaluates:
    - Minimum structural span (enforces settings.structural_min_span, default 65 bars).
    - Minimum pivot separation (enforces settings.trendline_min_pivot_dist, default 20 bars).
    - Multi-touch confirmation within dynamic ATR tolerance (minimum settings.trendline_min_touches).
    - Penetration check: verifies price prior to breakout did NOT close through the line.
    - Zero look-ahead bias: only evaluates up to len(df) - 1.
    """
    if len(pivots) < 2:
        return None

    span_threshold = min_span if min_span is not None else settings.structural_min_span
    pivot_dist_threshold = min_pivot_dist if min_pivot_dist is not None else settings.trendline_min_pivot_dist

    last_idx = len(df) - 1
    piv_sorted = sorted(pivots, key=lambda p: p.index)
    earliest_idx = max(0, last_idx - max_span)
    candidates = [p for p in piv_sorted if p.index >= earliest_idx and p.index < last_idx - 1]
    if len(candidates) < 2:
        return None

    best_tl: Optional[StructuralTrendline] = None
    best_score = -999999.0

    for i in range(len(candidates)):
        p1 = candidates[i]
        for j in range(i + 1, len(candidates)):
            p2 = candidates[j]
            span = p2.index - p1.index
            if span < pivot_dist_threshold:
                continue
            total_span = last_idx - p1.index
            if total_span < span_threshold:
                continue

            dx = p2.index - p1.index
            dy = p2.price - p1.price
            slope = dy / dx
            intercept = p1.price - slope * p1.index

            # Count touches across all available pivots and intermediate swings
            touches = 0
            touch_idxs = []
            for p in candidates:
                if p.index < p1.index or p.index >= last_idx:
                    continue
                expected_y = slope * p.index + intercept
                tol = atr_series.iloc[p.index] * settings.trendline_touch_tol_atr
                if abs(p.price - expected_y) <= tol:
                    # Enforce that touches are separated by at least 5 bars
                    if not touch_idxs or (p.index - touch_idxs[-1] >= 5):
                        touches += 1
                        touch_idxs.append(p.index)

            if touches < settings.trendline_min_touches:
                continue

            # Penetration check between p1.index and last_idx - 1
            check_slice = df.iloc[p1.index: last_idx]
            close_vals = check_slice["close"].values
            bar_idxs = np.arange(p1.index, last_idx)
            line_vals = slope * bar_idxs + intercept
            atr_vals = atr_series.iloc[p1.index: last_idx].values

            if is_upper:
                # Closes above line + 0.35 * ATR count as structural violations
                diffs = close_vals - (line_vals + atr_vals * 0.35)
                violations = int(np.sum(diffs > 0))
            else:
                # Closes below line - 0.35 * ATR count as structural violations
                diffs = (line_vals - atr_vals * 0.35) - close_vals
                violations = int(np.sum(diffs > 0))

            if violations > settings.trendline_max_violations:
                continue

            # Scoring: strong bonus for 3+ touches, reward longer structural span, penalize violations
            score = (touches * 35.0) + (total_span * 0.25) - (violations * 80.0)
            if touches >= 3:
                score += 50.0

            if score > best_score:
                best_score = score
                end_price = slope * last_idx + intercept
                best_tl = StructuralTrendline(
                    origin_idx=p1.index,
                    origin_price=p1.price,
                    end_idx=last_idx,
                    end_price=end_price,
                    slope=slope,
                    intercept=intercept,
                    touches=touches,
                    touch_indices=touch_idxs,
                    span_bars=total_span,
                    violations=violations,
                )

    return best_tl


def _valid_size(height: float, price: float, atr_val: float) -> bool:
    if height is None or not (np.isfinite(height) and np.isfinite(price) and np.isfinite(atr_val)):
        return False
    if height <= 0 or price <= 0 or atr_val <= 0:
        return False
    return height > max(price * settings.min_size_pct, atr_val * settings.min_atr_mult)


def _is_broken(df: pd.DataFrame, atr_series: pd.Series, x1: int, y1: float,
               x2: int, y2: float, is_up: bool, bar_idx: int) -> bool:
    if bar_idx < 0 or bar_idx >= len(df):
        return False
    proj_price = project(x1, y1, x2, y2, bar_idx)
    margin = (atr_series.iloc[bar_idx] * settings.break_atr_mult if settings.use_atr_breakout
              else proj_price * settings.break_pct)
    val = df["close"].iloc[bar_idx] if settings.require_close_break else (df["high"].iloc[bar_idx] if is_up else df["low"].iloc[bar_idx])
    return val > (proj_price + margin) if is_up else val < (proj_price - margin)


def _breakout_ok(df: pd.DataFrame, atr_series: pd.Series, x1: int, y1: float,
                 x2: int, y2: float, is_up: bool, recent_ref_idx: Optional[int] = None) -> bool:
    """True ONLY if the breakout is FRESH:
    1. Pattern structure was active recently (not stale).
    2. The latest closed candle (last_idx) confirms the break.
    3. The prior bars were NOT already broken out.
    """
    last_idx = len(df) - 1
    if last_idx < 2:
        return False

    ref_idx = recent_ref_idx if recent_ref_idx is not None else (min(x1, x2) if max(x1, x2) >= last_idx else max(x1, x2))
    bars_since_pattern = last_idx - ref_idx
    max_allowed_delay = max(settings.structural_fractal_period * 2, 80)
    if bars_since_pattern < 1 or bars_since_pattern > max_allowed_delay:
        return False

    # 1. Latest closed candle must confirm the breakout
    if not _is_broken(df, atr_series, x1, y1, x2, y2, is_up, last_idx):
        return False

    # 2. Freshness: Must be the initial breakout (not already broken 2 bars ago)
    if _is_broken(df, atr_series, x1, y1, x2, y2, is_up, last_idx - 2):
        return False

    return True


def _structural_sl(invalidation_price: float, is_bull: bool, atr_val: float) -> float:
    """Place SL just beyond the pattern's structural invalidation level with 1x ATR buffer."""
    buffer = atr_val * 1.0
    return invalidation_price - buffer if is_bull else invalidation_price + buffer


def _entry_price(df: pd.DataFrame) -> float:
    return float(df["close"].iloc[-1])


def _rr_ok(entry: float, stop: float, target: float, is_bull: bool) -> bool:
    if is_bull:
        if not (stop < entry < target):
            return False
        risk = entry - stop
        reward = (target - entry) - (entry * settings.taker_fee_pct * 2)
    else:
        return False  # LONG ONLY: Disallow non-bullish risk-reward
    if risk <= 0 or reward <= 0:
        return False
    return (reward / risk) >= settings.min_rr


# ==============================================================================
# 1. STRUCTURAL WEDGE & CHANNEL DETECTORS (LONG ONLY)
# ==============================================================================

def detect_wedge(df, atr_series, ph: list[Pivot], pl: list[Pivot], price: float, atr_val: float) -> Optional[PatternResult]:
    """Falling Wedge -> Bullish Breakout (LONG ONLY).
    Features long structural upper and lower trendlines with multi-touch confirmation.
    """
    last = len(df) - 1
    # Try structural multi-touch trendline first
    upper_tl = fit_structural_trendline(df, ph, is_upper=True, atr_series=atr_series)
    lower_tl = fit_structural_trendline(df, pl, is_upper=False, atr_series=atr_series)

    if upper_tl and lower_tl:
        # Both must slope downwards, converging towards current bar
        if upper_tl.slope < 0 and lower_tl.slope < 0 and lower_tl.slope < upper_tl.slope:
            if upper_tl.end_price > lower_tl.end_price:
                recent_touch = max(upper_tl.touch_indices[-1] if upper_tl.touch_indices else upper_tl.origin_idx,
                                   lower_tl.touch_indices[-1] if lower_tl.touch_indices else lower_tl.origin_idx)
                if _breakout_ok(df, atr_series, upper_tl.origin_idx, upper_tl.origin_price,
                                last, upper_tl.end_price, True, recent_ref_idx=recent_touch):
                    entry = _entry_price(df)
                    wedge_h = abs(upper_tl.origin_price - lower_tl.origin_price)
                    target = max(upper_tl.origin_price, upper_tl.end_price + wedge_h)
                    stop = entry - (target - entry) * settings.sl_pct_of_target_dist
                    if _rr_ok(entry, stop, target, True):
                        start_bar = min(upper_tl.origin_idx, lower_tl.origin_idx)
                        return PatternResult(
                            name="Falling Wedge",
                            is_bullish=True,
                            start_idx=start_bar,
                            breakout_idx=last,
                            entry_price=entry,
                            stop_price=stop,
                            target_price=target,
                            upper_line=(upper_tl.origin_idx, upper_tl.origin_price, last, upper_tl.end_price),
                            lower_line=(lower_tl.origin_idx, lower_tl.origin_price, last, lower_tl.end_price),
                        )
    return None


def detect_descending_channel(df, atr_series, ph: list[Pivot], pl: list[Pivot], price: float, atr_val: float) -> Optional[PatternResult]:
    """Descending Channel -> Bullish Breakout (LONG ONLY).
    Prominent macro channel like in POLYX 1W & FET 1D.
    Upper descending trendline connecting major lower highs, lower descending trendline
    connecting major lower lows, confirmed bullish breakout candle.
    """
    last = len(df) - 1
    upper_tl = fit_structural_trendline(df, ph, is_upper=True, atr_series=atr_series)
    lower_tl = fit_structural_trendline(df, pl, is_upper=False, atr_series=atr_series)

    if upper_tl and lower_tl:
        if upper_tl.slope < 0 and lower_tl.slope < 0:
            # Parallel or near-parallel downward channel
            parallel = is_near(upper_tl.slope, lower_tl.slope, 0.40) or (abs(upper_tl.slope - lower_tl.slope) < 0.05)
            width = upper_tl.end_price - lower_tl.end_price
            if parallel and width > atr_val * 1.2:
                recent_touch = max(upper_tl.touch_indices[-1] if upper_tl.touch_indices else upper_tl.origin_idx,
                                   lower_tl.touch_indices[-1] if lower_tl.touch_indices else lower_tl.origin_idx)
                if _breakout_ok(df, atr_series, upper_tl.origin_idx, upper_tl.origin_price,
                                last, upper_tl.end_price, True, recent_ref_idx=recent_touch):
                    entry = _entry_price(df)
                    target = max(upper_tl.origin_price, upper_tl.end_price + width * 1.5)
                    stop = entry - (target - entry) * settings.sl_pct_of_target_dist
                    if _rr_ok(entry, stop, target, True):
                        start_bar = min(upper_tl.origin_idx, lower_tl.origin_idx)
                        return PatternResult(
                            name="Descending Channel",
                            is_bullish=True,
                            start_idx=start_bar,
                            breakout_idx=last,
                            entry_price=entry,
                            stop_price=stop,
                            target_price=target,
                            upper_line=(upper_tl.origin_idx, upper_tl.origin_price, last, upper_tl.end_price),
                            lower_line=(lower_tl.origin_idx, lower_tl.origin_price, last, lower_tl.end_price),
                        )
    return None


def detect_triangle(df, atr_series, ph: list[Pivot], pl: list[Pivot], price: float, atr_val: float) -> Optional[PatternResult]:
    """Ascending & Symmetrical Triangles -> Bullish Breakout (LONG ONLY)."""
    last = len(df) - 1
    upper_tl = fit_structural_trendline(df, ph, is_upper=True, atr_series=atr_series)
    lower_tl = fit_structural_trendline(df, pl, is_upper=False, atr_series=atr_series)

    if upper_tl and lower_tl:
        flat_tol = price * 0.001
        is_flat_upper = abs(upper_tl.slope) < flat_tol
        is_ascending_lower = lower_tl.slope > 0
        is_descending_upper = upper_tl.slope < 0

        converging = upper_tl.end_price > lower_tl.end_price
        base_h = abs(upper_tl.origin_price - lower_tl.origin_price)

        if converging and _valid_size(base_h, price, atr_val):
            # Bullish breakout of Ascending Triangle or Symmetrical Triangle
            if (is_flat_upper and is_ascending_lower) or (is_descending_upper and is_ascending_lower):
                recent_touch = max(upper_tl.touch_indices[-1] if upper_tl.touch_indices else upper_tl.origin_idx,
                                   lower_tl.touch_indices[-1] if lower_tl.touch_indices else lower_tl.origin_idx)
                if _breakout_ok(df, atr_series, upper_tl.origin_idx, upper_tl.origin_price,
                                last, upper_tl.end_price, True, recent_ref_idx=recent_touch):
                    entry = _entry_price(df)
                    target = upper_tl.end_price + base_h
                    stop = entry - (target - entry) * settings.sl_pct_of_target_dist
                    if _rr_ok(entry, stop, target, True):
                        name = "Ascending Triangle" if is_flat_upper else "Symmetrical Triangle"
                        start_bar = min(upper_tl.origin_idx, lower_tl.origin_idx)
                        return PatternResult(
                            name=name,
                            is_bullish=True,
                            start_idx=start_bar,
                            breakout_idx=last,
                            entry_price=entry,
                            stop_price=stop,
                            target_price=target,
                            upper_line=(upper_tl.origin_idx, upper_tl.origin_price, last, upper_tl.end_price),
                            lower_line=(lower_tl.origin_idx, lower_tl.origin_price, last, lower_tl.end_price),
                        )
    return None


def detect_flag_pennant(df, atr_series, ph: list[Pivot], pl: list[Pivot], price: float, atr_val: float) -> Optional[PatternResult]:
    """Bullish Flag & Pennant -> Bullish Breakout (LONG ONLY)."""
    if len(ph) < 2 or len(pl) < 2:
        return None
    last = len(df) - 1
    h1, h2, l1, l2 = ph[0], ph[1], pl[0], pl[1]
    start_bar = min(h2.index, l2.index)
    if (last - start_bar) < settings.structural_min_span:
        return None
    pole_len = min(200, len(df) - start_bar)
    if pole_len < 3:
        return None
    window = df.iloc[max(0, len(df) - pole_len):]
    pole_move = window["high"].max() - window["low"].min()
    if pole_move <= atr_val * 3:
        return None
    slope_u = (h1.price - h2.price) / max(1, h1.index - h2.index)
    slope_l = (l1.price - l2.price) / max(1, l1.index - l2.index)
    parallel = is_near(slope_u, slope_l, 0.25) if slope_u and slope_l else abs(slope_u - slope_l) < 1e-9

    # Bullish Flag/Pennant ONLY: downward consolidation after bullish impulse
    if slope_u < 0 and slope_l < 0:
        if _breakout_ok(df, atr_series, h2.index, h2.price, h1.index, h1.price, True):
            entry = _entry_price(df)
            upper_at_break = project(h2.index, h2.price, h1.index, h1.price, last)
            lower_at_break = project(l2.index, l2.price, l1.index, l1.price, last)
            target = upper_at_break + pole_move
            stop = _structural_sl(lower_at_break, True, atr_val)
            if _rr_ok(entry, stop, target, True):
                name = "Bullish Flag" if parallel else "Bullish Pennant"
                return PatternResult(
                    name=name,
                    is_bullish=True,
                    start_idx=h2.index,
                    breakout_idx=last,
                    entry_price=entry,
                    stop_price=stop,
                    target_price=target,
                    upper_line=(h2.index, h2.price, last, upper_at_break),
                    lower_line=(l2.index, l2.price, last, lower_at_break),
                )
    return None


# ==============================================================================
# 2. REVERSAL PATTERNS (LONG ONLY: Double Bottom, Triple Bottom, Inv H&S)
# ==============================================================================

def detect_double(df, atr_series, ph: list[Pivot], pl: list[Pivot], price: float, atr_val: float) -> Optional[PatternResult]:
    """Double Bottom -> Bullish Breakout (LONG ONLY)."""
    if len(pl) >= 2 and len(ph) >= 1:
        p1, p2, mid = pl[0], pl[1], ph[0]
        last = len(df) - 1
        if (last - p2.index) < settings.structural_min_span:
            return None
        if p1.index > mid.index > p2.index and is_near(p1.price, p2.price, settings.lvl_tol):
            height = mid.price - (p1.price + p2.price) / 2
            if _valid_size(height, price, atr_val) and _breakout_ok(df, atr_series, mid.index, mid.price, last, mid.price, True, recent_ref_idx=mid.index):
                entry = _entry_price(df)
                target = mid.price + height
                stop = _structural_sl(min(p1.price, p2.price), True, atr_val)
                if _rr_ok(entry, stop, target, True):
                    return PatternResult(
                        name="Double Bottom",
                        is_bullish=True,
                        start_idx=p2.index,
                        breakout_idx=last,
                        entry_price=entry,
                        stop_price=stop,
                        target_price=target,
                        upper_line=(p2.index, mid.price, last, mid.price),
                        lower_line=(p2.index, min(p1.price, p2.price), last, min(p1.price, p2.price)),
                    )
    return None


def detect_triple(df, atr_series, ph: list[Pivot], pl: list[Pivot], price: float, atr_val: float) -> Optional[PatternResult]:
    """Triple Bottom -> Bullish Breakout (LONG ONLY)."""
    if len(pl) >= 3 and len(ph) >= 2:
        l1, l2, l3, h1, h2 = pl[0], pl[1], pl[2], ph[0], ph[1]
        last = len(df) - 1
        if (last - l3.index) < settings.structural_min_span:
            return None
        if l1.index > h1.index > l2.index > h2.index > l3.index:
            if is_near(l1.price, l2.price, settings.lvl_tol) and is_near(l2.price, l3.price, settings.lvl_tol):
                neck = max(h1.price, h2.price)
                bot = min(l1.price, l2.price, l3.price)
                height = neck - bot
                if _valid_size(height, price, atr_val) and _breakout_ok(df, atr_series, h2.index, neck, last, neck, True, recent_ref_idx=h1.index):
                    entry = _entry_price(df)
                    target = neck + height
                    stop = _structural_sl(bot, True, atr_val)
                    if _rr_ok(entry, stop, target, True):
                        return PatternResult(
                            name="Triple Bottom",
                            is_bullish=True,
                            start_idx=l3.index,
                            breakout_idx=last,
                            entry_price=entry,
                            stop_price=stop,
                            target_price=target,
                            upper_line=(l3.index, neck, last, neck),
                            lower_line=(l3.index, bot, last, bot),
                        )
    return None


def detect_head_shoulders(df, atr_series, ph: list[Pivot], pl: list[Pivot], price: float, atr_val: float) -> Optional[PatternResult]:
    """Inverse Head & Shoulders -> Bullish Breakout (LONG ONLY)."""
    if len(pl) >= 3 and len(ph) >= 2:
        rs, head, ls, neck_r, neck_l = pl[0], pl[1], pl[2], ph[0], ph[1]
        last = len(df) - 1
        if (last - ls.index) < settings.structural_min_span:
            return None
        if rs.index > neck_r.index > head.index > neck_l.index > ls.index:
            if head.price < rs.price and head.price < ls.price and is_near(ls.price, rs.price, settings.sym_tol):
                neck_avg = (neck_r.price + neck_l.price) / 2
                height = neck_avg - head.price
                if _valid_size(height, price, atr_val) and _breakout_ok(df, atr_series, neck_l.index, neck_l.price, neck_r.index, neck_r.price, True, recent_ref_idx=neck_r.index):
                    entry = _entry_price(df)
                    neck_at_break = project(neck_l.index, neck_l.price, neck_r.index, neck_r.price, last)
                    target = neck_at_break + height
                    stop = _structural_sl(rs.price, True, atr_val)
                    if _rr_ok(entry, stop, target, True):
                        return PatternResult(
                            name="Inv Head & Shoulders",
                            is_bullish=True,
                            start_idx=ls.index,
                            breakout_idx=last,
                            entry_price=entry,
                            stop_price=stop,
                            target_price=target,
                            upper_line=(neck_l.index, neck_l.price, last, neck_at_break),
                            lower_line=(ls.index, head.price, last, rs.price),
                        )
    return None


def detect_rectangle(df, atr_series, ph: list[Pivot], pl: list[Pivot], price: float, atr_val: float) -> Optional[PatternResult]:
    """Horizontal Consolidation -> Bullish Breakout (LONG ONLY)."""
    if len(ph) < 2 or len(pl) < 2:
        return None
    last = len(df) - 1
    h1, h2, l1, l2 = ph[0], ph[1], pl[0], pl[1]
    start = min(h2.index, l2.index)
    if (last - start) < settings.structural_min_span:
        return None
    slope_u = (h1.price - h2.price) / max(1, h1.index - h2.index)
    slope_l = (l1.price - l2.price) / max(1, l1.index - l2.index)
    flat_tol = atr_val * 0.08
    if abs(slope_u) >= flat_tol or abs(slope_l) >= flat_tol:
        return None
    top = (h1.price + h2.price) / 2
    bot = (l1.price + l2.price) / 2
    height = top - bot
    if not _valid_size(height, price, atr_val):
        return None
    # Bullish breakout above rectangle top
    if _breakout_ok(df, atr_series, start, top, last, top, True, recent_ref_idx=h1.index):
        entry = _entry_price(df)
        target = top + height
        stop = _structural_sl(bot, True, atr_val)
        if _rr_ok(entry, stop, target, True):
            return PatternResult(
                name="Rectangle",
                is_bullish=True,
                start_idx=start,
                breakout_idx=last,
                entry_price=entry,
                stop_price=stop,
                target_price=target,
                upper_line=(start, top, last, top),
                lower_line=(start, bot, last, bot),
            )
    return None


def detect_cup_handle(df, atr_series, ph: list[Pivot], pl: list[Pivot], price: float, atr_val: float) -> Optional[PatternResult]:
    """Cup & Handle -> Bullish Breakout (LONG ONLY)."""
    if len(ph) < 2 or len(pl) < 2:
        return None
    last = len(df) - 1
    h_rim, h_left = ph[0], ph[1]
    l_handle, l_bot = pl[0], pl[1]
    if (last - h_left.index) < settings.structural_min_span:
        return None
    if l_handle.index > h_rim.index > l_bot.index > h_left.index:
        if is_near(h_rim.price, h_left.price, settings.sym_tol) and l_bot.price < l_handle.price < h_rim.price:
            cup_h = h_rim.price - l_bot.price
            if _valid_size(cup_h, price, atr_val) and _breakout_ok(df, atr_series, h_left.index, h_left.price, h_rim.index, h_rim.price, True, recent_ref_idx=h_rim.index):
                entry = _entry_price(df)
                rim_at_break = project(h_left.index, h_left.price, h_rim.index, h_rim.price, last)
                target = rim_at_break + cup_h
                stop = _structural_sl(l_handle.price, True, atr_val)
                if _rr_ok(entry, stop, target, True):
                    return PatternResult(
                        name="Cup & Handle",
                        is_bullish=True,
                        start_idx=h_left.index,
                        breakout_idx=last,
                        entry_price=entry,
                        stop_price=stop,
                        target_price=target,
                        upper_line=(h_left.index, h_left.price, last, rim_at_break),
                        lower_line=(h_left.index, l_bot.price, last, l_handle.price),
                    )
    return None


# ==============================================================================
# DETECTORS ORCHESTRATION (LONG ONLY)
# ==============================================================================

DETECTORS = [
    detect_wedge,
    detect_descending_channel,
    detect_triangle,
    detect_flag_pennant,
    detect_double,
    detect_triple,
    detect_head_shoulders,
    detect_cup_handle,
    detect_rectangle,
]


def detect_all(df, atr_series, pivot_highs, pivot_lows) -> Optional[PatternResult]:
    """Runs all detectors on freshly closed candles. LONG ONLY.
    Guaranteed zero look-ahead bias and no short signals.
    """
    price = float(df["close"].iloc[-1])
    atr_val = float(atr_series.iloc[-1])
    for fn in DETECTORS:
        result = fn(df, atr_series, pivot_highs, pivot_lows, price, atr_val)
        if result and result.is_bullish and result.name in settings.enabled_patterns:
            return result
    return None
