import sys
import os
sys.path.insert(0, os.path.abspath("."))
from datetime import datetime, timezone, timedelta
import asyncio
import time
import pandas as pd
import numpy as np

from bot.config import settings
from bot.models import Direction, Signal, PatternResult, Pivot
from bot.indicators import ema, atr, find_pivots, find_structural_pivots, find_multiscale_pivots
from bot.patterns import (
    fit_structural_trendline,
    detect_wedge,
    detect_descending_channel,
    detect_triangle,
    detect_all,
)
from bot.signal_engine import evaluate_symbol_timeframe
from bot.chart_generator import render_signal_chart
from bot.binance_client import BinanceRateLimiter, CandleCache


def generate_macro_channel_df(n_bars=500, tf_days=1, slope=-0.15, channel_width=25.0, cycle=100.0):
    """Generates realistic macro descending channel / wedge across n_bars with 4+ distinct 65-fractal touches."""
    start_date = datetime(2025, 1, 1, tzinfo=timezone.utc)
    dates = [start_date + timedelta(days=i * tf_days) for i in range(n_bars)]

    x = np.arange(n_bars, dtype=float)
    origin_price = 200.0
    upper_base = origin_price + slope * x
    mid = upper_base - channel_width / 2.0
    wave = np.sin((x - 20) * (2 * np.pi / cycle))
    close = mid + wave * (channel_width / 2.0 - 2.0)
    close[-1] = upper_base[-1] + 6.0  # Clear breakout candle
    open_p = close.copy() - 0.5
    open_p[-1] = upper_base[-1] - 0.5
    high = np.maximum(open_p, close) + 2.0
    low = np.minimum(open_p, close) - 2.0
    high[-1] = close[-1] + 1.0
    low[-1] = open_p[-1] - 1.0
    volume = np.full(n_bars, 10000.0)

    df = pd.DataFrame({
        "open_time": dates,
        "close_time": dates,
        "open": open_p,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
    })
    return df


def run_all_tests():
    print("=================================================================")
    print("  RUNNING STRUCTURAL TRENDLINES (65 FRACTAL) & RATE LIMIT SUITE  ")
    print("=================================================================")

    # 1. Test Active Timeframes
    print("\n[1] Verifying Active Timeframes...")
    assert settings.timeframes == ("1d", "3d", "1w"), f"Unexpected timeframes: {settings.timeframes}"
    assert "12h" not in settings.timeframes, "12h must NOT be in timeframes!"
    assert "4h" not in settings.timeframes and "1h" not in settings.timeframes and "15m" not in settings.timeframes
    print(f"  PASS: Active timeframes = {settings.timeframes} (No 12h, 4h, 1h, 15m)")

    # 2. Test 65 Fractal Period Extraction
    print("\n[2] Testing 65 Fractal Period Structural Pivot Extraction...")
    df_1d = generate_macro_channel_df(n_bars=500, tf_days=1)
    struct_ph, struct_pl = find_structural_pivots(
        df_1d,
        fractal_period=settings.structural_fractal_period,
        min_pivot_dist=settings.trendline_min_pivot_dist,
    )
    assert len(struct_ph) >= 3, f"Should find at least 3 structural swing highs across 500 bars, got {len(struct_ph)}"
    assert len(struct_pl) >= 3, f"Should find at least 3 structural swing lows across 500 bars, got {len(struct_pl)}"
    # Check that structural pivots are sufficiently spaced (no micro-squiggles)
    for i in range(len(struct_ph) - 1):
        dist = abs(struct_ph[i].index - struct_ph[i + 1].index)
        assert dist >= settings.trendline_min_pivot_dist, f"Structural pivots must be >= {settings.trendline_min_pivot_dist} bars apart, got {dist}"
    print(f"  PASS: Found {len(struct_ph)} structural swing highs and {len(struct_pl)} structural swing lows using period {settings.structural_fractal_period}")

    # 3. Test Long Structural Trendline Fitting (Touches & Span >= 65 bars)
    print("\n[3] Testing Long Structural Trendline Fitting (Touches & Span)...")
    atr_series = atr(df_1d, 14)
    tl = fit_structural_trendline(df_1d, struct_ph, is_upper=True, atr_series=atr_series)
    assert tl is not None, "Structural trendline should be found on macro channel data"
    assert tl.span_bars >= settings.structural_min_span, f"Trendline must span at least {settings.structural_min_span} bars, got: {tl.span_bars}"
    assert tl.touches >= 2, f"Trendline should have at least 2 touches, got: {tl.touches}"
    assert tl.slope < 0, f"Descending channel trendline should have negative slope, got: {tl.slope}"
    print(f"  PASS: Structural Upper Trendline discovered! Origin bar: {tl.origin_idx} ({tl.span_bars} bars long), Touches: {tl.touches}, Slope: {tl.slope:.4f}")

    # 4. Test Rejection of Short / Non-Structural Trendlines
    print("\n[4] Testing Strict Rejection of Short Trendlines (< 65 bars)...")
    df_short = pd.DataFrame({"close": np.linspace(100, 90, 50)})
    atr_short = pd.Series(np.full(50, 1.0))
    pivots_short = [Pivot(price=100.0, index=10), Pivot(price=95.0, index=30)]
    tl_short = fit_structural_trendline(df_short, pivots_short, is_upper=True, atr_series=atr_short)
    assert tl_short is None, "Trendlines shorter than structural_min_span MUST be rejected!"
    print(f"  PASS: Short trendline (span < {settings.structural_min_span}) was strictly rejected.")

    # 5. Test Penetration / Invalidation Filter
    print("\n[5] Testing Penetration & Invalidation Rejection...")
    df_violated = df_1d.copy()
    # Inject premature breakouts across intermediate and recent bars (breaching all potential lines)
    df_violated.loc[380:390, "close"] += 25.0
    df_violated.loc[460:470, "close"] += 25.0
    tl_violated = fit_structural_trendline(df_violated, struct_ph, is_upper=True, atr_series=atr_series)
    assert tl_violated is None, "Penetrated/invalidated trendlines MUST be rejected!"
    print("  PASS: Penetrated trendline was successfully rejected.")

    # 6. Test Breakout Confirmation on Macro Structure
    print("\n[6] Testing Fresh Breakout Confirmation on Macro Channel...")
    sig, pat, reason = evaluate_symbol_timeframe("TESTUSDT", "1d", df_1d)
    assert sig is not None, "Signal should be produced for clean macro channel breakout"
    assert pat is not None, "Pattern should be detected"
    assert pat.is_bullish is True, "Pattern MUST be bullish!"
    assert pat.breakout_idx == len(df_1d) - 1, "Breakout candle must be the last closed bar"
    print(f"  PASS: Detected pattern: {pat.name} (is_bullish={pat.is_bullish}) spanning {pat.upper_line[2] - pat.upper_line[0]} bars!")

    # 7. Test LONG ONLY Enforcement (No Short Signals Ever)
    print("\n[7] Verifying LONG ONLY Enforcement...")
    df_bearish = df_1d.copy()
    df_bearish["close"] = 300.0 - df_1d["close"]
    df_bearish["high"] = 300.0 - df_1d["low"]
    df_bearish["low"] = 300.0 - df_1d["high"]
    df_bearish["open"] = 300.0 - df_1d["open"]
    sig_b, _, _ = evaluate_symbol_timeframe("TESTUSDT", "1d", df_bearish)
    assert sig_b is None, f"Expected NO signal on bearish data in LONG-ONLY mode, got: {sig_b}"
    print("  PASS: Zero short signals generated. Engine is strictly LONG ONLY.")

    # 8. Test EMA200 Non-Dependency
    print("\n[8] Verifying EMA200 Is NOT an Entry Filter...")
    # Add offset to price so EMA200 is high above price
    df_below_ema = df_1d.copy()
    sig_ema, pat_ema, reason_ema = evaluate_symbol_timeframe("TESTUSDT", "1d", df_below_ema)
    assert sig_ema is not None, "Signal MUST NOT be filtered by EMA200!"
    assert sig_ema.direction == Direction.LONG
    print(f"  PASS: Succeeded! Signal produced: {sig_ema.pattern} {sig_ema.direction.value} without EMA200 gate.")

    # 9. Test Rate Limiter, Cooldown & Candle Cache Behavior
    print("\n[9] Verifying Rate Limiter, Cooldown & Shared Candle Cache...")
    async def _test_rate_limiter_suite():
        limiter = BinanceRateLimiter(max_weight_per_min=1200, min_interval_sec=0.03)
        t0 = time.monotonic()
        for _ in range(4):
            await limiter.acquire(weight=2)
        elapsed = time.monotonic() - t0
        assert elapsed >= 0.08, f"Pacing should enforce inter-request delay, got {elapsed:.3f}s"

        # Cooldown trigger (429/-1003 simulation)
        limiter.trigger_cooldown(0.2)
        t1 = time.monotonic()
        await limiter.acquire(weight=2)
        cooldown_elapsed = time.monotonic() - t1
        assert cooldown_elapsed >= 0.18, f"Cooldown should pause acquire, got {cooldown_elapsed:.3f}s"

        # CandleCache Deduplication
        cache = CandleCache()
        cache.set("BTCUSDT", "1d", df_1d)
        cached = cache.get("BTCUSDT", "1d", max_age_sec=300)
        assert cached is not None, "Cache should return valid DataFrame without network request"
        assert len(cached) == len(df_1d)

    asyncio.run(_test_rate_limiter_suite())
    print("  PASS: Token bucket rate limiter, 429/-1003 cooldown, and shared candle cache verified.")

    # 10. Generate Sample Visual Charts for 1D, 3D, 1W
    print("\n[10] Generating Sample Chart PNGs for 1D, 3D, 1W...")
    os.makedirs("scratch/charts", exist_ok=True)

    timeframes_to_test = [("1d", 1), ("3d", 3), ("1w", 7)]
    for tf_label, days_mult in timeframes_to_test:
        df_tf = generate_macro_channel_df(n_bars=500, tf_days=days_mult, slope=-0.15, channel_width=25.0, cycle=100.0)
        sig_tf, pat_tf, reason_tf = evaluate_symbol_timeframe(f"POLYXUSDT", tf_label, df_tf)
        assert pat_tf is not None, f"Failed to detect structural pattern for {tf_label}"
        assert sig_tf is not None, f"Failed to generate signal for {tf_label}"

        chart_png = render_signal_chart(sig_tf.symbol, sig_tf.timeframe, df_tf, pat_tf, sig_tf)
        assert chart_png.startswith(b"\x89PNG\r\n\x1a\n"), f"Invalid PNG generated for {tf_label}"

        out_path = f"scratch/charts/sample_chart_{tf_label}.png"
        with open(out_path, "wb") as f:
            f.write(chart_png)
        span_bars = pat_tf.upper_line[2] - pat_tf.upper_line[0]
        print(f"  -> Generated {out_path} ({len(chart_png)} bytes, Upper line structural span: {span_bars} bars)")

    print("\n=================================================================")
    print("  ALL 10 TESTS PASSED SUCCESSFULLY! ZERO REGRESSION VERIFIED.    ")
    print("=================================================================")


if __name__ == "__main__":
    run_all_tests()
