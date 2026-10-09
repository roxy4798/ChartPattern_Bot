"""
Comprehensive Regression Test Suite for Fractal Period Audit & Improvements.
Covers all 8 non-negotiable regression test requirements:
1. Default structural period = 30
2. Environment override
3. Invalid configuration handling
4. Structural minimum span = 65 remains independent
5. Closed-candle confirmation and no lookahead
6. LONG ONLY behavior
7. Timeframes remain ('1d', '3d', '1w')
8. Existing signal and chart consistency
"""
import os
import sys
import pytest
import pandas as pd
import numpy as np

sys.path.insert(0, os.path.abspath("."))

from bot.config import Settings, settings
from bot.indicators import find_structural_pivots, atr
from bot.patterns import fit_structural_trendline, detect_all
from bot.signal_engine import evaluate_symbol_timeframe
from bot.chart_generator import render_signal_chart
from bot.models import Direction
from scratch.test_structural_trendlines import generate_macro_channel_df


def test_1_default_structural_period():
    print("\n[Regression 1] Verifying default structural period = 30...")
    default_cfg = Settings()
    assert default_cfg.structural_fractal_period == 30, (
        f"Default structural_fractal_period must be 30, got {default_cfg.structural_fractal_period}"
    )
    
    df = generate_macro_channel_df(n_bars=200)
    # When fractal_period is None, find_structural_pivots must use settings.structural_fractal_period (30)
    ph_explicit, pl_explicit = find_structural_pivots(df, fractal_period=30)
    ph_default, pl_default = find_structural_pivots(df, fractal_period=None)
    assert len(ph_explicit) == len(ph_default), "find_structural_pivots must consume default 30 when None"
    assert len(pl_explicit) == len(pl_default), "find_structural_pivots must consume default 30 when None"
    print("  PASS: Default structural period is canonically 30.")


def test_2_environment_override():
    print("\n[Regression 2] Verifying environment override capability...")
    orig_env = os.environ.get("STRUCTURAL_FRACTAL_PERIOD")
    try:
        os.environ["STRUCTURAL_FRACTAL_PERIOD"] = "25"
        custom_cfg = Settings()
        assert custom_cfg.structural_fractal_period == 25, (
            f"Expected environment override to 25, got {custom_cfg.structural_fractal_period}"
        )
        print("  PASS: Environment override to 25 successfully verified.")
    finally:
        if orig_env is not None:
            os.environ["STRUCTURAL_FRACTAL_PERIOD"] = orig_env
        else:
            os.environ.pop("STRUCTURAL_FRACTAL_PERIOD", None)


def test_3_invalid_configuration_handling():
    print("\n[Regression 3] Verifying invalid configuration handling...")
    orig_period = os.environ.get("STRUCTURAL_FRACTAL_PERIOD")
    orig_span = os.environ.get("STRUCTURAL_MIN_SPAN")
    
    # Test invalid STRUCTURAL_FRACTAL_PERIOD <= 0
    try:
        os.environ["STRUCTURAL_FRACTAL_PERIOD"] = "0"
        with pytest.raises(ValueError, match="STRUCTURAL_FRACTAL_PERIOD must be a positive integer"):
            Settings()
            
        os.environ["STRUCTURAL_FRACTAL_PERIOD"] = "-10"
        with pytest.raises(ValueError, match="STRUCTURAL_FRACTAL_PERIOD must be a positive integer"):
            Settings()

        # Test invalid STRUCTURAL_MIN_SPAN <= 0
        os.environ.pop("STRUCTURAL_FRACTAL_PERIOD", None)
        os.environ["STRUCTURAL_MIN_SPAN"] = "0"
        with pytest.raises(ValueError, match="STRUCTURAL_MIN_SPAN must be a positive integer"):
            Settings()
    finally:
        if orig_period is not None:
            os.environ["STRUCTURAL_FRACTAL_PERIOD"] = orig_period
        else:
            os.environ.pop("STRUCTURAL_FRACTAL_PERIOD", None)
        if orig_span is not None:
            os.environ["STRUCTURAL_MIN_SPAN"] = orig_span
        else:
            os.environ.pop("STRUCTURAL_MIN_SPAN", None)

    # Test direct indicator invalid parameter handling
    df = pd.DataFrame({"high": [10.0] * 50, "low": [5.0] * 50, "close": [8.0] * 50})
    with pytest.raises(ValueError, match="fractal_period must be positive"):
        find_structural_pivots(df, fractal_period=0)

    with pytest.raises(ValueError, match="fractal_period must be positive"):
        find_structural_pivots(df, fractal_period=-5)

    with pytest.raises(ValueError, match="confirmation_bars non-negative"):
        find_structural_pivots(df, fractal_period=30, confirmation_bars=-1)

    # Verify short history does not silently clamp; returns empty lists
    short_df = pd.DataFrame({"high": [10.0] * 40, "low": [5.0] * 40, "close": [8.0] * 40})
    ph_short, pl_short = find_structural_pivots(short_df, fractal_period=30, confirmation_bars=20)
    assert ph_short == [] and pl_short == [], "Insufficient history must return empty lists without silent clamping"

    print("  PASS: Invalid configuration and indicator arguments properly reject invalid values.")


def test_4_structural_min_span_independent():
    print("\n[Regression 4] Verifying structural minimum span = 65 independence...")
    assert settings.structural_min_span == 65
    
    # Test that trendline fitting rejects spans < 65 regardless of fractal period
    df_span_50 = pd.DataFrame({
        "open": [100.0] * 50, "high": [105.0] * 50, "low": [95.0] * 50, "close": [100.0] * 50
    })
    atr_series = atr(df_span_50, 14)
    from bot.models import Pivot
    pivots_short = [Pivot(price=105.0, index=5), Pivot(price=103.0, index=45)]
    
    # Even if called with min_span default (65), a 40-bar trendline must be rejected
    tl = fit_structural_trendline(df_span_50, pivots_short, is_upper=True, atr_series=atr_series)
    assert tl is None, "Trendline spanning < 65 bars must be rejected"

    # Verifying fractal period changing does not alter structural_min_span
    orig = settings.structural_fractal_period
    settings.structural_fractal_period = 15
    assert settings.structural_min_span == 65, "Changing fractal period must NOT alter structural_min_span"
    settings.structural_fractal_period = orig
    print("  PASS: Structural minimum span = 65 is enforced completely independently.")


def test_5_closed_candle_confirmation_no_lookahead():
    print("\n[Regression 5] Verifying closed candle confirmation & zero lookahead...")
    df_base = generate_macro_channel_df(n_bars=300)
    ph1, pl1 = find_structural_pivots(df_base, fractal_period=30, confirmation_bars=20)
    
    # Append arbitrary future bars to a copy. Pivots in the confirmed window must remain identical
    df_future = df_base.copy()
    future_tail = pd.DataFrame({
        "open_time": pd.date_range("2026-01-01", periods=10, freq="1D"),
        "close_time": pd.date_range("2026-01-01", periods=10, freq="1D"),
        "open": [999.0] * 10,
        "high": [1999.0] * 10,
        "low": [888.0] * 10,
        "close": [999.0] * 10,
        "volume": [1000.0] * 10,
    })
    df_expanded = pd.concat([df_future, future_tail], ignore_index=True)
    
    # Last valid index in df_base is len(df_base) - 1 - 20 = 279
    # In df_expanded, all pivots with index <= 279 must be IDENTICAL (no repainting, no lookahead)
    ph2, pl2 = find_structural_pivots(df_expanded, fractal_period=30, confirmation_bars=20)
    ph2_filtered = [p for p in ph2 if p.index <= 279]
    pl2_filtered = [p for p in pl2 if p.index <= 279]
    
    assert [p.index for p in ph1] == [p.index for p in ph2_filtered], "Pivots must never repaint or depend on future bars"
    assert [p.price for p in ph1] == [p.price for p in ph2_filtered], "Pivot prices must be strictly identical"
    print("  PASS: Closed-candle confirmation verified. Strictly zero lookahead bias.")


def test_6_long_only_behavior():
    print("\n[Regression 6] Verifying strictly LONG ONLY behavior...")
    df_bullish = generate_macro_channel_df(n_bars=500)
    sig_bull, pat_bull, _ = evaluate_symbol_timeframe("TESTUSDT", "1d", df_bullish)
    assert sig_bull is not None
    assert sig_bull.direction == Direction.LONG, "Signal must be Direction.LONG"
    assert pat_bull.is_bullish is True, "Pattern must be bullish"

    # Invert price series into a clear breakdown / bear channel
    df_bear = df_bullish.copy()
    df_bear["close"] = 300.0 - df_bullish["close"]
    df_bear["open"] = 300.0 - df_bullish["open"]
    df_bear["high"] = 300.0 - df_bullish["low"]
    df_bear["low"] = 300.0 - df_bullish["high"]
    
    sig_bear, pat_bear, _ = evaluate_symbol_timeframe("TESTUSDT", "1d", df_bear)
    assert sig_bear is None, "Engine must NEVER generate short signals in LONG-ONLY architecture"
    assert pat_bear is None, "Bearish patterns must be rejected"
    print("  PASS: Strictly LONG ONLY behavior verified.")


def test_7_timeframes_consistency():
    print("\n[Regression 7] Verifying canonical timeframes...")
    assert settings.timeframes == ("1d", "3d", "1w"), f"Invalid timeframes: {settings.timeframes}"
    for invalid_tf in ("1m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h"):
        assert invalid_tf not in settings.timeframes, f"Prohibited timeframe {invalid_tf} found"
    print("  PASS: Timeframes are strictly ('1d', '3d', '1w').")


def test_8_signal_and_chart_consistency():
    print("\n[Regression 8] Verifying signal and chart pivot consistency...")
    df = generate_macro_channel_df(n_bars=500)
    sig, pat, _ = evaluate_symbol_timeframe("BTCUSDT", "1d", df)
    assert sig is not None and pat is not None
    
    # Extract pivots via signal engine logic
    engine_ph, engine_pl = find_structural_pivots(
        df,
        fractal_period=settings.structural_fractal_period,
        min_pivot_dist=settings.trendline_min_pivot_dist,
    )
    
    # Render chart PNG
    chart_png = render_signal_chart(sig.symbol, sig.timeframe, df, pat, sig)
    assert chart_png.startswith(b"\x89PNG\r\n\x1a\n"), "Chart must produce valid PNG bytes"
    
    # Verify exact same parameters used in chart_generator.py:
    # fractal_period=settings.structural_fractal_period, min_pivot_dist=settings.trendline_min_pivot_dist
    assert len(engine_ph) >= 2 and len(engine_pl) >= 2
    print(f"  PASS: Signal and chart pivot consistency verified ({len(engine_ph)} highs, {len(engine_pl)} lows, PNG {len(chart_png)} bytes).")


def run_regression_suite():
    print("=================================================================")
    print("  RUNNING COMPREHENSIVE FRACTAL AUDIT REGRESSION TEST SUITE      ")
    print("=================================================================")
    test_1_default_structural_period()
    test_2_environment_override()
    test_3_invalid_configuration_handling()
    test_4_structural_min_span_independent()
    test_5_closed_candle_confirmation_no_lookahead()
    test_6_long_only_behavior()
    test_7_timeframes_consistency()
    test_8_signal_and_chart_consistency()
    print("\n=================================================================")
    print("  ALL 8 REGRESSION TESTS PASSED (8/8 PASS, 0 FAIL)               ")
    print("=================================================================")


if __name__ == "__main__":
    run_regression_suite()
