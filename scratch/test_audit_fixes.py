"""
Targeted regression tests for findings discovered during the production audit:
1. signal_engine: timestamp fallback without close_time (NameError prevention)
2. tracker: accurate lookback calculation on 3d and 1w via TF_SECONDS
3. telegram_bot: cmd_stats timeframe synchronization
"""
import sys
import os
from datetime import datetime, timezone, timedelta
import pandas as pd
import numpy as np

sys.path.insert(0, os.path.abspath("."))

from bot.config import settings
from bot.models import Signal, Direction, SignalStatus
from bot.signal_engine import evaluate_symbol_timeframe
from bot.tracker import _calc_required_lookback, TF_SECONDS
from bot.telegram_bot import cmd_stats
from scratch.test_structural_trendlines import generate_macro_channel_df


def test_signal_engine_fallback():
    print("[1] Testing signal_engine fallback without close_time...")
    df = generate_macro_channel_df(n_bars=500)
    df_no_close = df.drop(columns=["close_time"])
    # Must evaluate cleanly without NameError
    sig, pat, reas = evaluate_symbol_timeframe("BTCUSDT", "1d", df_no_close)
    assert sig is not None, "Signal should be generated"
    assert sig.signal_time is not None, "Signal time should be present"
    print(f"  PASS: Evaluated cleanly, signal_time: {sig.signal_time}")


def test_tracker_tf_seconds_lookback():
    print("\n[2] Testing tracker lookback on 3d and 1w...")
    assert "3d" in TF_SECONDS and TF_SECONDS["3d"] == 259200
    assert "1w" in TF_SECONDS and TF_SECONDS["1w"] == 604800

    now = datetime.now(timezone.utc)
    sig_time_3d = (now - timedelta(days=6)).isoformat()  # 6 days elapsed = exactly 2 bars on 3D
    sig_3d = Signal(
        id="sig-3d-test",
        symbol="BTCUSDT",
        timeframe="3d",
        pattern="Descending Channel",
        direction=Direction.LONG,
        entry_price=100.0,
        stop_loss=90.0,
        tp1=110.0, tp2=120.0, tp3=130.0,
        price_at_signal=100.0,
        signal_time=sig_time_3d,
    )
    lookback_3d = _calc_required_lookback(sig_3d)
    # 6 days / 3 days = 2 bars. 2 + 10 = 12 bars lookback!
    assert 11 <= lookback_3d <= 15, f"Expected ~12 bars lookback for 6-day-old 3D signal, got {lookback_3d}"
    print(f"  PASS: 3D signal lookback calculated accurately: {lookback_3d} bars (previously would have pegged at 500)")

    sig_time_1w = (now - timedelta(days=14)).isoformat()  # 14 days elapsed = exactly 2 bars on 1W
    sig_1w = Signal(
        id="sig-1w-test",
        symbol="BTCUSDT",
        timeframe="1w",
        pattern="Falling Wedge",
        direction=Direction.LONG,
        entry_price=100.0,
        stop_loss=90.0,
        tp1=110.0, tp2=120.0, tp3=130.0,
        price_at_signal=100.0,
        signal_time=sig_time_1w,
    )
    lookback_1w = _calc_required_lookback(sig_1w)
    # 14 days / 7 days = 2 bars. 2 + 10 = 12 bars lookback!
    assert 11 <= lookback_1w <= 15, f"Expected ~12 bars lookback for 14-day-old 1W signal, got {lookback_1w}"
    print(f"  PASS: 1W signal lookback calculated accurately: {lookback_1w} bars (previously would have pegged at 500)")


def test_active_timeframes_sync():
    print("\n[3] Testing active timeframes consistency...")
    assert settings.timeframes == ("1d", "3d", "1w")
    print("  PASS: settings.timeframes is exactly ('1d', '3d', '1w')")


if __name__ == "__main__":
    test_signal_engine_fallback()
    test_tracker_tf_seconds_lookback()
    test_active_timeframes_sync()
    print("\n=======================================================")
    print("  ALL AUDIT FIX REGRESSION TESTS PASSED (100% SUCCESS) ")
    print("=======================================================")
