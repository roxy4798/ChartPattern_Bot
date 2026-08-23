from __future__ import annotations
import io
import pandas as pd
import numpy as np
import mplfinance as mpf
import matplotlib.pyplot as plt
from bot.models import PatternResult, Signal
from bot.indicators import ema, find_pivots
from bot.config import settings

WATERMARK_TEXT = "NeoElla Trade"


def _add_watermark(fig, ax):
    """Add consistent, legible branding without obscuring the analysis."""
    # Faint diagonal tile across the full figure, behind everything.
    for gx in (0.16, 0.5, 0.84):
        for gy in (0.22, 0.5, 0.78):
            fig.text(
                gx, gy, WATERMARK_TEXT,
                transform=fig.transFigure, fontsize=12, fontweight="bold",
                color="#d1d4dc", alpha=0.05, ha="center", va="center",
                rotation=28, zorder=0, family="DejaVu Sans",
            )

    ax.text(
        0.5, 0.5, WATERMARK_TEXT,
        transform=ax.transAxes, fontsize=25, fontweight="bold",
        color="#d1d4dc", alpha=0.09, ha="center", va="center",
        rotation=0, zorder=1, family="DejaVu Sans",
    )

    fig.text(
        0.985, 0.015, WATERMARK_TEXT,
        transform=fig.transFigure, fontsize=9, fontweight="bold",
        color="#f5c518", alpha=0.85, ha="right", va="bottom",
        family="DejaVu Sans", zorder=6,
    )


def describe_confirmation(candle: pd.Series, is_bullish: bool) -> str:
    """Describe the breakout candle for information only, never as a filter."""
    open_price = float(candle["open"])
    close = float(candle["close"])
    high = float(candle["high"])
    low = float(candle["low"])
    candle_range = max(high - low, 1e-12)
    body_ratio = abs(close - open_price) / candle_range
    aligned = (close >= open_price) if is_bullish else (close <= open_price)
    close_position = (close - low) / candle_range
    if not aligned:
        return "Possible Fakeout"
    if body_ratio >= 0.65:
        return "Strong Momentum Candle"
    if body_ratio < 0.25:
        return "Weak Confirmation"
    if (is_bullish and close_position < 0.45) or (not is_bullish and close_position > 0.55):
        return "Rejection Candle"
    return "Bullish Confirmation" if is_bullish else "Bearish Confirmation"


def _validate_inputs(symbol: str, timeframe: str, df: pd.DataFrame,
                     pattern: PatternResult, signal: Signal) -> None:
    required = {"open_time", "open", "high", "low", "close"}
    missing = required.difference(df.columns)
    if missing:
        raise ValueError(f"Cannot render chart; missing candle columns: {sorted(missing)}")
    if signal.symbol != symbol or signal.timeframe != timeframe:
        raise ValueError("Cannot render chart; signal identity does not match chart identity")
    if signal.pattern != pattern.name:
        raise ValueError("Cannot render chart; signal pattern does not match detected pattern")
    if not np.isclose(signal.entry_price, pattern.entry_price) or not np.isclose(
        signal.stop_loss, pattern.stop_price
    ):
        raise ValueError("Cannot render chart; signal levels do not match detected pattern")
    if df.empty or df["open_time"].duplicated().any() or not df["open_time"].is_monotonic_increasing:
        raise ValueError("Cannot render chart; candle timestamps are empty, duplicated, or out of order")
    ohlc = df[["open", "high", "low", "close"]].to_numpy(dtype=float)
    if (
        not np.isfinite(ohlc).all()
        or (ohlc[:, 1, None] < ohlc[:, [0, 3]]).any()
        or (ohlc[:, 2, None] > ohlc[:, [0, 3]]).any()
    ):
        raise ValueError("Cannot render chart; OHLC data is invalid")
    if pattern.breakout_idx < 0 or pattern.breakout_idx >= len(df):
        raise ValueError("Cannot render chart; breakout candle is outside the data")
    prices = (signal.entry_price, signal.stop_loss, signal.tp1, signal.tp2, signal.tp3,
              signal.ema200_at_signal)
    if not all(np.isfinite(price) for price in prices):
        raise ValueError("Cannot render chart; signal contains a non-finite price")
    for line_name in ("upper_line", "lower_line"):
        line = getattr(pattern, line_name)
        if line is not None and not all(np.isfinite(value) for value in line):
            raise ValueError(f"Cannot render chart; {line_name} contains invalid geometry")


def render_signal_chart(symbol: str, timeframe: str, df: pd.DataFrame,
                         pattern: PatternResult, signal: Signal, lookback: int = 150) -> bytes:
    """Renders a candlestick chart with EMA200, pattern geometry, and
    entry/SL/TP lines. Returns PNG bytes."""
    _validate_inputs(symbol, timeframe, df, pattern, signal)
    df = df.reset_index(drop=True).copy()
    plot_df = df.tail(lookback).copy()
    if plot_df.empty:
        raise ValueError("Cannot render chart; candle data is empty")
    plot_df.index = pd.DatetimeIndex(plot_df["open_time"])
    offset = len(df) - len(plot_df)

    ema200 = ema(df["close"], 200)
    plot_df["EMA200"] = ema200.tail(lookback).values

    addplots = [mpf.make_addplot(plot_df["EMA200"], color="#f5c518", width=1.2)]
    confirmation = pd.Series(np.nan, index=plot_df.index, dtype=float)
    confirmation.iloc[-1] = (
        plot_df["high"].iloc[-1] if pattern.is_bullish else plot_df["low"].iloc[-1]
    )
    addplots.append(mpf.make_addplot(
        confirmation, type="scatter", marker="^" if pattern.is_bullish else "v",
        markersize=80, color="#00e676" if pattern.is_bullish else "#ff1744",
    ))

    mc = mpf.make_marketcolors(up="#26a69a", down="#ef5350", edge="inherit",
                                wick="inherit", volume="in")
    style = mpf.make_mpf_style(base_mpf_style="nightclouds", marketcolors=mc,
                                gridstyle="--", gridcolor="#2a2e39", facecolor="#131722",
                                figcolor="#131722", edgecolor="#131722",
                                rc={"font.size": 9, "text.color": "#d1d4dc",
                                    "axes.labelcolor": "#d1d4dc", "xtick.color": "#787b86",
                                    "ytick.color": "#787b86"})

    hlines = dict(
        hlines=[signal.entry_price, signal.stop_loss, signal.tp1, signal.tp2, signal.tp3],
        colors=["#ffd600", "#ff1744", "#00e676", "#00c853", "#00b0ff"],
        linestyle="--", linewidths=1.0,
    )

    fig, axes = mpf.plot(
        plot_df, type="candle", style=style, addplot=addplots, hlines=hlines,
        volume=False, returnfig=True, figsize=(11, 6.5), tight_layout=True,
        datetime_format="%m-%d %H:%M", xrotation=15,
    )
    ax = axes[0]

    def _clamp(idx):
        return max(0, min(len(plot_df) - 1, idx - offset))

    def _draw_line(line, color):
        if not line:
            return
        x1, y1, x2, y2 = line
        ax.plot([_clamp(x1), _clamp(x2)], [y1, y2], color=color, linewidth=2, alpha=0.9, zorder=5)

    pat_color = "#00e676" if pattern.is_bullish else "#ff1744"
    _draw_line(pattern.upper_line, pat_color)
    _draw_line(pattern.lower_line, pat_color)
    if pattern.upper_line and pattern.lower_line:
        upper = pattern.upper_line
        lower = pattern.lower_line
        ax.fill_between(
            [_clamp(upper[0]), _clamp(upper[2])],
            [upper[1], upper[3]], [lower[1], lower[3]],
            color=pat_color, alpha=0.06, zorder=1,
        )

    # Plot actual pivots in the detected structure so the geometry can be
    # checked against candles rather than relying on the pattern name.
    pivot_highs, pivot_lows = find_pivots(df, settings.lb_left, settings.lb_right)
    structure_start = max(0, pattern.start_idx)
    for pivot, marker, color in (
        *((p, "^", "#ffb300") for p in pivot_highs
          if structure_start <= p.index <= pattern.breakout_idx),
        *((p, "v", "#29b6f6") for p in pivot_lows
          if structure_start <= p.index <= pattern.breakout_idx),
    ):
        ax.scatter(_clamp(pivot.index), pivot.price, marker=marker, s=28,
                   color=color, edgecolors="#131722", linewidths=0.5, zorder=6)

    label_x = min(len(plot_df) - 1, _clamp(pattern.breakout_idx) + 2)
    price_span = plot_df["high"].max() - plot_df["low"].min()
    confirmation_label = describe_confirmation(df.iloc[-1], pattern.is_bullish)
    label_y = (
        plot_df["high"].max() - price_span * 0.06
        if pattern.is_bullish else plot_df["low"].min() + price_span * 0.06
    )
    ax.annotate(
        f"{pattern.name} · {signal.direction.value}\n"
        f"CONFIRMATION CANDLE\n{confirmation_label}",
        xy=(_clamp(pattern.breakout_idx), float(df["close"].iloc[-1])),
        xytext=(label_x, label_y), color=pat_color, fontsize=8,
        fontweight="bold", ha="right" if label_x >= len(plot_df) - 2 else "left",
        va="top" if pattern.is_bullish else "bottom", clip_on=True,
        bbox={"boxstyle": "round,pad=0.3", "facecolor": "#131722",
              "edgecolor": pat_color, "alpha": 0.8},
    )

    for price, label, color in [
        (signal.entry_price, f"Entry {signal.entry_price:.6g}", "#ffd600"),
        (signal.stop_loss, f"SL {signal.stop_loss:.6g}", "#ff1744"),
        (signal.tp1, f"TP1 {signal.tp1:.6g}", "#00e676"),
        (signal.tp2, f"TP2 {signal.tp2:.6g}", "#00c853"),
        (signal.tp3, f"TP3 {signal.tp3:.6g}", "#00b0ff"),
    ]:
        ax.annotate(label, xy=(len(plot_df) - 1, price), xytext=(-4, 0),
                    textcoords="offset points", color=color, fontsize=8,
                    va="center", ha="right", fontweight="bold", clip_on=True)

    title = f"{symbol}  ·  {timeframe}  ·  {pattern.name}  ·  {signal.direction.value}  ·  EMA 200"
    ax.set_title(title, color="#d1d4dc", fontsize=12, fontweight="bold", loc="left")

    _add_watermark(fig, ax)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    image = buf.read()
    if not image.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("Chart rendering produced an invalid PNG")
    return image
