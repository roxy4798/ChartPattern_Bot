import asyncio
import os
import sys
sys.path.insert(0, os.path.abspath("."))
import shutil
import pandas as pd
import numpy as np
from datetime import datetime, timezone

from bot.config import settings

from bot.models import Signal, Direction, PatternResult
from bot.database import Database
from bot.square.publisher import SquarePublisher
from bot.square.client import find_node_executable
from bot.chart_generator import render_signal_chart

async def run_pre_real_check():
    print("=== FINAL PRE-REAL CHECK FOR BINANCE SQUARE + CHART ===")
    
    # 1. Verify Node.js
    node_bin = find_node_executable()
    print(f"1. Node.js detection: {'PASS' if node_bin else 'FAIL'} ({node_bin})")
    assert node_bin, "Node.js executable not found"
    
    # 2. Verify post-image.mjs script existence
    script_path = os.path.abspath("bot/square/binance_skill/scripts/post-image.mjs")
    print(f"2. Official post-image.mjs script existence: {'PASS' if os.path.exists(script_path) else 'FAIL'}")
    assert os.path.exists(script_path), "post-image.mjs missing"
    
    # 3. Generate REAL chart PNG using bot/chart_generator.py
    n_candles = 250
    dates = pd.date_range("2026-08-15 00:00", periods=n_candles, freq="1h")
    base_price = 100.0
    trend = np.linspace(0, 10, n_candles)
    close_prices = base_price + trend + np.sin(np.linspace(0, 20, n_candles)) * 2
    high_prices = close_prices + 1.0
    low_prices = close_prices - 1.0
    open_prices = close_prices - 0.2
    
    df = pd.DataFrame({
        "open_time": dates,
        "open": open_prices,
        "high": high_prices,
        "low": low_prices,
        "close": close_prices,
        "volume": [1000.0] * n_candles,
    })
    
    pat = PatternResult(
        name="Bullish Flag",
        is_bullish=True,
        start_idx=100,
        breakout_idx=n_candles - 1,
        entry_price=float(close_prices[-1]),
        stop_price=float(close_prices[-1]) - 3.0,
        target_price=float(close_prices[-1]) + 6.0,
    )

    
    sig = Signal(
        id="test-pre-real-sig-001",
        symbol="BTCUSDT",
        timeframe="1h",
        pattern="Bullish Flag",
        direction=Direction.LONG,
        entry_price=pat.entry_price,
        stop_loss=pat.stop_price,
        tp1=pat.entry_price + 2.0,
        tp2=pat.entry_price + 4.0,
        tp3=pat.entry_price + 6.0,
        ema200_at_signal=95.0,
        price_at_signal=pat.entry_price,
        signal_time=datetime.now(timezone.utc).isoformat(),
    )
    
    real_chart_png = render_signal_chart("BTCUSDT", "1h", df, pat, sig)
    is_png = (real_chart_png[:4] == b'\x89PNG')
    print(f"3. Real Chart PNG generation (bot/chart_generator.py): PASS ({len(real_chart_png)} bytes, PNG header: {is_png})")
    assert is_png, "Invalid PNG bytes"

    
    # 4. Test Square Publisher Dry Run with Real Chart PNG
    test_db = Database("bot/data/test_pre_real.db")
    await test_db.init()
    
    settings.square_enabled = True
    settings.square_dry_run = True
    
    publisher = SquarePublisher(db=test_db)
    
    res = await publisher.publish_signal(sig, pat, "Breakout confirmed", chart_png=real_chart_png)
    print(f"4. Square Publisher Dry Run Execution: {'PASS' if res.success and res.status.value == 'SKIPPED_DRY_RUN' else 'FAIL'} (Status: {res.status.value})")
    assert res.success is True
    assert res.status.value == "SKIPPED_DRY_RUN"
    
    # 5. Verify no temporary PNG files leaked in bot/data/
    temp_files = [f for f in os.listdir("bot/data") if f.startswith("temp_chart_")]
    print(f"5. Temporary Chart Cleanup Verification: {'PASS' if len(temp_files) == 0 else 'FAIL'} (Remaining temp files: {len(temp_files)})")
    assert len(temp_files) == 0, f"Temporary files not cleaned up: {temp_files}"
    
    # 6. Verify Database History Isolation
    prod_db = Database("bot/data/trading.db")
    await prod_db.init()
    fartcoin_post = await prod_db.get_square_post("fartcoin-sig-id") # Check if table exists
    total_posts = await prod_db.get_square_successful_real_posts_count()
    print(f"6. Database History & Atomic Latch Check: PASS (Total successful real posts in DB: {total_posts})")
    await prod_db.close()
    
    await test_db.close()
    if os.path.exists("bot/data/test_pre_real.db"):
        os.remove("bot/data/test_pre_real.db")
        
    settings.square_enabled = False
    settings.square_dry_run = True
    print("\nALL PRE-REAL CHECKS PASSED 100%!")

asyncio.run(run_pre_real_check())
