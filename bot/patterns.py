"""
Pattern detectors ported from the supplied Pine Script indicator.

Design difference from the original indicator: Pine re-evaluates every
historical bar looking backward for a breakout. This bot instead runs once
per scan on freshly closed candles and only accepts a pattern if the
breakout/breakdown condition is true on the LAST closed candle - i.e. we
only ever alert on a fresh confirmed break, which is the correct behaviour
for a live signal service (and avoids re-alerting on old, already-broken
structures).

Each detector takes the two/three most recent pivots (matching the Pine
`pivotHighs.get(0)`, `.get(1)`, `.get(2)` newest-first indexing) and returns
a PatternResult or None.
"""
from __future__ import annotations
from typing import Optional
import pandas as pd
from bot.models import Pivot, PatternResult
from bot.indicators import project, is_near
from bot.config import settings


def _valid_size(height: float, price: float, atr_val: float) -> bool:
    if height is None or height <= 0:
        return False
    return height > max(price * settings.min_size_pct, atr_val * settings.min_atr_mult)


def _breakout_ok(df: pd.DataFrame, atr_series: pd.Series, x1: int, y1: float,
                  x2: int, y2: float, is_up: bool) -> bool:
    """True if the LAST closed candle confirms a break of the line
    (x1,y1)-(x2,y2) extrapolated to the last index, using an ATR or
    percentage margin exactly like the Pine `f_get_break_idx_precise`."""
    last_idx = len(df) - 1
    if last_idx < 1:
        return False
    proj_price = project(x1, y1, x2, y2, last_idx)
    margin = (atr_series.iloc[last_idx] * settings.break_atr_mult if settings.use_atr_breakout
              else proj_price * settings.break_pct)
    close = df["close"].iloc[last_idx]
    high = df["high"].iloc[last_idx]
    low = df["low"].iloc[last_idx]
    check_val = close if settings.require_close_break else (high if is_up else low)
    return check_val > proj_price + margin if is_up else check_val < proj_price - margin


def _sl_from_target(entry: float, target: float, is_bull: bool) -> float:
    dist = abs(target - entry)
    return entry - dist * settings.sl_pct_of_target_dist if is_bull \
        else entry + dist * settings.sl_pct_of_target_dist


def _entry_price(df: pd.DataFrame) -> float:
    # Enter at the close of the confirming (breakout) candle.
    return float(df["close"].iloc[-1])


def _rr_ok(entry: float, stop: float, target: float) -> bool:
    risk = abs(entry - stop)
    if risk == 0:
        return False
    fee_cost = entry * settings.taker_fee_pct * 2
    reward = max(0.0, abs(target - entry) - fee_cost)
    return reward / risk >= settings.min_rr


def detect_double(df, atr_series, ph: list[Pivot], pl: list[Pivot], price: float, atr_val: float) -> Optional[PatternResult]:
    if len(ph) >= 2 and len(pl) >= 1:
        p1, p2, mid = ph[0], ph[1], pl[0]
        if p1.index > mid.index > p2.index and is_near(p1.price, p2.price, settings.lvl_tol):
            height = (p1.price + p2.price) / 2 - mid.price
            if _valid_size(height, price, atr_val) and _breakout_ok(df, atr_series, mid.index, mid.price, len(df) - 1, mid.price, False):
                entry = _entry_price(df)
                target = mid.price - height
                stop = _sl_from_target(entry, target, False)
                if _rr_ok(entry, stop, target):
                    return PatternResult("Double Top", False, p2.index, len(df) - 1,
                                          entry, stop, target,
                                          lower_line=(mid.index, mid.price, len(df) - 1, mid.price))
    if len(pl) >= 2 and len(ph) >= 1:
        p1, p2, mid = pl[0], pl[1], ph[0]
        if p1.index > mid.index > p2.index and is_near(p1.price, p2.price, settings.lvl_tol):
            height = mid.price - (p1.price + p2.price) / 2
            if _valid_size(height, price, atr_val) and _breakout_ok(df, atr_series, mid.index, mid.price, len(df) - 1, mid.price, True):
                entry = _entry_price(df)
                target = mid.price + height
                stop = _sl_from_target(entry, target, True)
                if _rr_ok(entry, stop, target):
                    return PatternResult("Double Bottom", True, p2.index, len(df) - 1,
                                          entry, stop, target,
                                          upper_line=(mid.index, mid.price, len(df) - 1, mid.price))
    return None


def detect_triple(df, atr_series, ph, pl, price, atr_val) -> Optional[PatternResult]:
    if len(ph) >= 3 and len(pl) >= 2:
        h1, h2, h3, l1, l2 = ph[0], ph[1], ph[2], pl[0], pl[1]
        if h1.index > l1.index > h2.index > l2.index > h3.index:
            if is_near(h1.price, h2.price, settings.lvl_tol) and is_near(h2.price, h3.price, settings.lvl_tol):
                neck = min(l1.price, l2.price)
                top = max(h1.price, h2.price, h3.price)
                height = top - neck
                if _valid_size(height, price, atr_val) and _breakout_ok(df, atr_series, l2.index, neck, l1.index, neck, False):
                    entry = _entry_price(df)
                    target = neck - height
                    stop = _sl_from_target(entry, target, False)
                    if _rr_ok(entry, stop, target):
                        return PatternResult("Triple Top", False, h3.index, len(df) - 1,
                                              entry, stop, target,
                                              lower_line=(l2.index, neck, len(df) - 1, neck))
    if len(pl) >= 3 and len(ph) >= 2:
        l1, l2, l3, h1, h2 = pl[0], pl[1], pl[2], ph[0], ph[1]
        if l1.index > h1.index > l2.index > h2.index > l3.index:
            if is_near(l1.price, l2.price, settings.lvl_tol) and is_near(l2.price, l3.price, settings.lvl_tol):
                neck = max(h1.price, h2.price)
                bot = min(l1.price, l2.price, l3.price)
                height = neck - bot
                if _valid_size(height, price, atr_val) and _breakout_ok(df, atr_series, h2.index, neck, h1.index, neck, True):
                    entry = _entry_price(df)
                    target = neck + height
                    stop = _sl_from_target(entry, target, True)
                    if _rr_ok(entry, stop, target):
                        return PatternResult("Triple Bottom", True, l3.index, len(df) - 1,
                                              entry, stop, target,
                                              upper_line=(h2.index, neck, len(df) - 1, neck))
    return None


def detect_head_shoulders(df, atr_series, ph, pl, price, atr_val) -> Optional[PatternResult]:
    if len(ph) >= 3 and len(pl) >= 2:
        rs, head, ls, neck_r, neck_l = ph[0], ph[1], ph[2], pl[0], pl[1]
        if rs.index > neck_r.index > head.index > neck_l.index > ls.index:
            if head.price > rs.price and head.price > ls.price and is_near(ls.price, rs.price, settings.sym_tol):
                neck_avg = (neck_r.price + neck_l.price) / 2
                height = head.price - neck_avg
                if _valid_size(height, price, atr_val) and _breakout_ok(df, atr_series, neck_l.index, neck_l.price, neck_r.index, neck_r.price, False):
                    entry = _entry_price(df)
                    neck_at_break = project(neck_l.index, neck_l.price, neck_r.index, neck_r.price, len(df) - 1)
                    target = neck_at_break - height
                    stop = _sl_from_target(entry, target, False)
                    if _rr_ok(entry, stop, target):
                        return PatternResult("Head & Shoulders", False, ls.index, len(df) - 1,
                                              entry, stop, target,
                                              lower_line=(neck_l.index, neck_l.price, len(df) - 1, neck_at_break))
    if len(pl) >= 3 and len(ph) >= 2:
        rs, head, ls, neck_r, neck_l = pl[0], pl[1], pl[2], ph[0], ph[1]
        if rs.index > neck_r.index > head.index > neck_l.index > ls.index:
            if head.price < rs.price and head.price < ls.price and is_near(ls.price, rs.price, settings.sym_tol):
                neck_avg = (neck_r.price + neck_l.price) / 2
                height = neck_avg - head.price
                if _valid_size(height, price, atr_val) and _breakout_ok(df, atr_series, neck_l.index, neck_l.price, neck_r.index, neck_r.price, True):
                    entry = _entry_price(df)
                    neck_at_break = project(neck_l.index, neck_l.price, neck_r.index, neck_r.price, len(df) - 1)
                    target = neck_at_break + height
                    stop = _sl_from_target(entry, target, True)
                    if _rr_ok(entry, stop, target):
                        return PatternResult("Inv Head & Shoulders", True, ls.index, len(df) - 1,
                                              entry, stop, target,
                                              upper_line=(neck_l.index, neck_l.price, len(df) - 1, neck_at_break))
    return None


def detect_flag_pennant(df, atr_series, ph, pl, price, atr_val) -> Optional[PatternResult]:
    if len(ph) < 2 or len(pl) < 2:
        return None
    h1, h2, l1, l2 = ph[0], ph[1], pl[0], pl[1]
    start_bar = min(h2.index, l2.index)
    pole_len = min(200, len(df) - start_bar)
    if pole_len < 3:
        return None
    window = df.iloc[max(0, len(df) - pole_len):]
    pole_move = window["high"].max() - window["low"].min()
    if pole_move <= atr_val * 3:
        return None
    slope_u = 0.0 if h1.index == h2.index else (h1.price - h2.price) / max(1, h1.index - h2.index)
    slope_l = 0.0 if l1.index == l2.index else (l1.price - l2.price) / max(1, l1.index - l2.index)
    parallel = is_near(slope_u, slope_l, 0.2) if slope_u and slope_l else abs(slope_u - slope_l) < 1e-9
    last = len(df) - 1
    if slope_u < 0 and slope_l < 0:
        if _breakout_ok(df, atr_series, h2.index, h2.price, h1.index, h1.price, True):
            entry = _entry_price(df)
            upper_at_break = project(h2.index, h2.price, h1.index, h1.price, last)
            target = upper_at_break + pole_move
            stop = _sl_from_target(entry, target, True)
            if _rr_ok(entry, stop, target):
                name = "Bullish Flag" if parallel else "Bullish Pennant"
                return PatternResult(name, True, h2.index, last, entry, stop, target,
                                      upper_line=(start_bar, project(h2.index, h2.price, h1.index, h1.price, start_bar), last, upper_at_break),
                                      lower_line=(start_bar, project(l2.index, l2.price, l1.index, l1.price, start_bar), last, project(l2.index, l2.price, l1.index, l1.price, last)))
    elif slope_u > 0 and slope_l > 0:
        if _breakout_ok(df, atr_series, l2.index, l2.price, l1.index, l1.price, False):
            entry = _entry_price(df)
            lower_at_break = project(l2.index, l2.price, l1.index, l1.price, last)
            target = lower_at_break - pole_move
            stop = _sl_from_target(entry, target, False)
            if _rr_ok(entry, stop, target):
                name = "Bearish Flag" if parallel else "Bearish Pennant"
                return PatternResult(name, False, h2.index, last, entry, stop, target,
                                      upper_line=(start_bar, project(h2.index, h2.price, h1.index, h1.price, start_bar), last, project(h2.index, h2.price, h1.index, h1.price, last)),
                                      lower_line=(start_bar, project(l2.index, l2.price, l1.index, l1.price, start_bar), last, lower_at_break))
    return None


def detect_wedge(df, atr_series, ph, pl, price, atr_val) -> Optional[PatternResult]:
    if len(ph) < 2 or len(pl) < 2:
        return None
    h1, h2, l1, l2 = ph[0], ph[1], pl[0], pl[1]
    slope_u = 0.0 if h1.index == h2.index else (h1.price - h2.price) / max(1, h1.index - h2.index)
    slope_l = 0.0 if l1.index == l2.index else (l1.price - l2.price) / max(1, l1.index - l2.index)
    last = len(df) - 1
    proj_u_now = project(h2.index, h2.price, h1.index, h1.price, last)
    proj_l_now = project(l2.index, l2.price, l1.index, l1.price, last)
    if proj_u_now - proj_l_now <= 0:
        return None
    if slope_u < 0 and slope_l < 0 and slope_l < slope_u:
        if _breakout_ok(df, atr_series, h2.index, h2.price, h1.index, h1.price, True):
            entry = _entry_price(df)
            target = h2.price
            stop = _sl_from_target(entry, target, True)
            if _rr_ok(entry, stop, target):
                return PatternResult("Falling Wedge", True, h2.index, last, entry, stop, target,
                                      upper_line=(h2.index, h2.price, last, proj_u_now),
                                      lower_line=(l2.index, l2.price, last, proj_l_now))
    elif slope_u > 0 and slope_l > 0 and slope_l > slope_u:
        if _breakout_ok(df, atr_series, l2.index, l2.price, l1.index, l1.price, False):
            entry = _entry_price(df)
            target = l2.price
            stop = _sl_from_target(entry, target, False)
            if _rr_ok(entry, stop, target):
                return PatternResult("Rising Wedge", False, h2.index, last, entry, stop, target,
                                      upper_line=(h2.index, h2.price, last, proj_u_now),
                                      lower_line=(l2.index, l2.price, last, proj_l_now))
    return None


def detect_triangle(df, atr_series, ph, pl, price, atr_val) -> Optional[PatternResult]:
    if len(ph) < 2 or len(pl) < 2:
        return None
    h1, h2, l1, l2 = ph[0], ph[1], pl[0], pl[1]
    slope_u = 0.0 if h1.index == h2.index else (h1.price - h2.price) / max(1, h1.index - h2.index)
    slope_l = 0.0 if l1.index == l2.index else (l1.price - l2.price) / max(1, l1.index - l2.index)
    start = min(h2.index, l2.index)
    base_height = abs(project(h2.index, h2.price, h1.index, h1.price, start) - project(l2.index, l2.price, l1.index, l1.price, start))
    flat_tol = price * 0.0005
    last = len(df) - 1
    converging = project(h2.index, h2.price, h1.index, h1.price, last) > project(l2.index, l2.price, l1.index, l1.price, last)
    if not converging or not _valid_size(base_height, price, atr_val):
        return None
    if (slope_u < 0 and slope_l > 0) or (abs(slope_u) < flat_tol and slope_l > 0):
        if _breakout_ok(df, atr_series, h2.index, h2.price, h1.index, h1.price, True):
            entry = _entry_price(df)
            upper_at_break = project(h2.index, h2.price, h1.index, h1.price, last)
            target = upper_at_break + base_height
            stop = _sl_from_target(entry, target, True)
            if _rr_ok(entry, stop, target):
                name = "Ascending Triangle" if abs(slope_u) < flat_tol else "Symmetrical Triangle"
                return PatternResult(name, True, h2.index, last, entry, stop, target,
                                      upper_line=(h2.index, h2.price, last, upper_at_break),
                                      lower_line=(l2.index, l2.price, last, project(l2.index, l2.price, l1.index, l1.price, last)))
    if (slope_u < 0 and slope_l > 0) or (slope_u < 0 and abs(slope_l) < flat_tol):
        if _breakout_ok(df, atr_series, l2.index, l2.price, l1.index, l1.price, False):
            entry = _entry_price(df)
            lower_at_break = project(l2.index, l2.price, l1.index, l1.price, last)
            target = lower_at_break - base_height
            stop = _sl_from_target(entry, target, False)
            if _rr_ok(entry, stop, target):
                name = "Descending Triangle" if abs(slope_l) < flat_tol else "Symmetrical Triangle"
                return PatternResult(name, False, h2.index, last, entry, stop, target,
                                      upper_line=(h2.index, h2.price, last, project(h2.index, h2.price, h1.index, h1.price, last)),
                                      lower_line=(l2.index, l2.price, last, lower_at_break))
    return None


def detect_rectangle(df, atr_series, ph, pl, price, atr_val) -> Optional[PatternResult]:
    if len(ph) < 2 or len(pl) < 2:
        return None
    h1, h2, l1, l2 = ph[0], ph[1], pl[0], pl[1]
    slope_u = 0.0 if h1.index == h2.index else (h1.price - h2.price) / max(1, h1.index - h2.index)
    slope_l = 0.0 if l1.index == l2.index else (l1.price - l2.price) / max(1, l1.index - l2.index)
    flat_tol = atr_val * 0.05
    if abs(slope_u) >= flat_tol or abs(slope_l) >= flat_tol:
        return None
    start = min(h2.index, l2.index)
    top = (h1.price + h2.price) / 2
    bot = (l1.price + l2.price) / 2
    height = top - bot
    if not _valid_size(height, price, atr_val):
        return None
    last = len(df) - 1
    if _breakout_ok(df, atr_series, start, top, last, top, True):
        entry = _entry_price(df)
        target = top + height
        stop = _sl_from_target(entry, target, True)
        if _rr_ok(entry, stop, target):
            return PatternResult("Rectangle", True, h2.index, last, entry, stop, target,
                                  upper_line=(start, top, last, top), lower_line=(start, bot, last, bot))
    if _breakout_ok(df, atr_series, start, bot, last, bot, False):
        entry = _entry_price(df)
        target = bot - height
        stop = _sl_from_target(entry, target, False)
        if _rr_ok(entry, stop, target):
            return PatternResult("Rectangle", False, h2.index, last, entry, stop, target,
                                  upper_line=(start, top, last, top), lower_line=(start, bot, last, bot))
    return None


def detect_cup_handle(df, atr_series, ph, pl, price, atr_val) -> Optional[PatternResult]:
    if len(ph) < 2 or len(pl) < 2:
        return None
    last = len(df) - 1
    # Standard cup & handle (bullish): rim(high[0]) - bottom(low[1]) - handle low(low[0])
    h_rim, h_left = ph[0], ph[1]
    l_handle, l_bot = pl[0], pl[1]
    if l_handle.index > h_rim.index > l_bot.index > h_left.index:
        if is_near(h_rim.price, h_left.price, settings.sym_tol) and l_bot.price < l_handle.price < h_rim.price:
            cup_h = h_rim.price - l_bot.price
            if _valid_size(cup_h, price, atr_val) and _breakout_ok(df, atr_series, h_left.index, h_left.price, h_rim.index, h_rim.price, True):
                entry = _entry_price(df)
                rim_at_break = project(h_left.index, h_left.price, h_rim.index, h_rim.price, last)
                target = rim_at_break + cup_h
                stop = _sl_from_target(entry, target, True)
                if _rr_ok(entry, stop, target):
                    return PatternResult("Cup & Handle", True, h_left.index, last, entry, stop, target,
                                          upper_line=(h_left.index, h_left.price, last, rim_at_break))
    # Inverted cup & handle (bearish)
    l_rim, l_left = pl[0], pl[1]
    h_handle, h_top = ph[0], ph[1]
    if h_handle.index > l_rim.index > h_top.index > l_left.index:
        if is_near(l_rim.price, l_left.price, settings.sym_tol) and l_rim.price < h_handle.price < h_top.price:
            cup_h = h_top.price - l_rim.price
            if _valid_size(cup_h, price, atr_val) and _breakout_ok(df, atr_series, l_left.index, l_left.price, l_rim.index, l_rim.price, False):
                entry = _entry_price(df)
                rim_at_break = project(l_left.index, l_left.price, l_rim.index, l_rim.price, last)
                target = rim_at_break - cup_h
                stop = _sl_from_target(entry, target, False)
                if _rr_ok(entry, stop, target):
                    return PatternResult("Inv Cup & Handle", False, l_left.index, last, entry, stop, target,
                                          lower_line=(l_left.index, l_left.price, last, rim_at_break))
    return None


DETECTORS = [
    detect_double, detect_triple, detect_head_shoulders,
    detect_flag_pennant, detect_wedge, detect_triangle,
    detect_rectangle, detect_cup_handle,
]


def detect_all(df, atr_series, pivot_highs, pivot_lows) -> Optional[PatternResult]:
    price = float(df["close"].iloc[-1])
    atr_val = float(atr_series.iloc[-1])
    for fn in DETECTORS:
        result = fn(df, atr_series, pivot_highs, pivot_lows, price, atr_val)
        if result and result.name in settings.enabled_patterns:
            return result
    return None
