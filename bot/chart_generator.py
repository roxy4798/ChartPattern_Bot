from __future__ import annotations
import io
import pandas as pd
import numpy as np
import mplfinance as mpf
import matplotlib.pyplot as plt
from bot.models import PatternResult, Signal
from bot.indicators import ema

WATERMARK_TEXT = "NEOELLA TRADE"


def _add_watermark(fig, ax):
    """Anti-theft branding: a bold centered mark plus a faint repeating
    tile across the whole chart, so a crop/screenshot still carries the
    brand. Kept subtle enough not to obscure candles or price levels."""
    # Faint diagonal tile across the full figure, behind everything.
    for gx in (0.16, 0.5, 0.84):
        for gy in (0.22, 0.5, 0.78):
            fig.text(
                gx, gy, WATERMARK_TEXT,
                transform=fig.transFigure, fontsize=13, fontweight="bold",
                color="#d1d4dc", alpha=0.05, ha="center", va="center",
                rotation=28, zorder=0, family="DejaVu Sans",
            )

    # Dominant centered mark, letter-spaced for a premium wordmark feel.
    # Horizontal (not tilted) so it reads cleanly at a glance, kept faint
    # enough to never compete with the candles or price levels.
    spaced = " ".join(WATERMARK_TEXT)
    ax.text(
        0.5, 0.5, spaced,
        transform=ax.transAxes, fontsize=26, fontweight="bold",
        color="#d1d4dc", alpha=0.07, ha="center", va="center",
        rotation=0, zorder=1, family="DejaVu Sans",
    )

    # Small solid brand tag, bottom-right corner.
    fig.text(
        0.985, 0.015, WATERMARK_TEXT,
        transform=fig.transFigure, fontsize=9, fontweight="bold",
        color="#f5c518", alpha=0.85, ha="right", va="bottom",
        family="DejaVu Sans", zorder=6,
    )


def render_signal_chart(symbol: str, timeframe: str, df: pd.DataFrame,
                         pattern: PatternResult, signal: Signal, lookback: int = 150) -> bytes:
    """Renders a candlestick chart with EMA200, pattern geometry, and
    entry/SL/TP lines. Returns PNG bytes."""
    df = df.reset_index(drop=True).copy()
    plot_df = df.tail(lookback).copy()
    plot_df.index = pd.DatetimeIndex(plot_df["open_time"])
    offset = len(df) - len(plot_df)

    ema200 = ema(df["close"], 200)
    plot_df["EMA200"] = ema200.tail(lookback).values

    addplots = [mpf.make_addplot(plot_df["EMA200"], color="#f5c518", width=1.2)]

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

    label_x = min(len(plot_df) - 1, _clamp(pattern.breakout_idx) + 2)
    price_span = plot_df["high"].max() - plot_df["low"].min()
    ax.annotate(f"{pattern.name}\n{signal.direction.value}", xy=(label_x, plot_df["high"].max()),
                xytext=(label_x, plot_df["high"].max() + price_span * 0.03),
                color=pat_color, fontsize=10, fontweight="bold", ha="left")

    for price, label, color in [
        (signal.entry_price, f"Entry {signal.entry_price:.6g}", "#ffd600"),
        (signal.stop_loss, f"SL {signal.stop_loss:.6g}", "#ff1744"),
        (signal.tp1, f"TP1 {signal.tp1:.6g}", "#00e676"),
        (signal.tp2, f"TP2 {signal.tp2:.6g}", "#00c853"),
        (signal.tp3, f"TP3 {signal.tp3:.6g}", "#00b0ff"),
    ]:
        ax.text(len(plot_df) - 1, price, f"  {label}", color=color, fontsize=8,
                 va="center", fontweight="bold")

    title = f"{symbol}  ·  {timeframe}  ·  {pattern.name}  ·  {signal.direction.value}"
    ax.set_title(title, color="#d1d4dc", fontsize=12, fontweight="bold", loc="left")

    _add_watermark(fig, ax)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return buf.read()
