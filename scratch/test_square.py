import sys
import os
sys.path.insert(0, os.path.abspath("."))

import asyncio
import sqlite3
import pandas as pd
import numpy as np

from bot.config import settings
from bot.models import Signal, PatternResult, Direction, SignalStatus
from bot.database import Database
from bot.square.models import SquareStatus, SquarePostResult
from bot.square.formatter import format_square_post, extract_cashtag
from bot.square.validator import validate_square_post, compute_signal_hash
from bot.square.circuit_breaker import SquareCircuitBreaker
from bot.square.client import BinanceSquareClient
from bot.square.publisher import SquarePublisher


async def run_tests():
    print("=== BINANCE SQUARE TEST SUITE ===")
    
    # 1. Test Cashtag extraction
    assert extract_cashtag("BTCUSDT") == "$BTC"
    assert extract_cashtag("ETHUSDT") == "$ETH"
    assert extract_cashtag("SOLUSDC") == "$SOL"
    assert extract_cashtag("1000PEPEUSDT") == "$1000PEPE"
    print("1. Cashtag extraction: PASS")

    # 2. Test Deterministic Formatter & Content Length
    entry = 112500.0
    stop = 110800.0
    tp1 = 114200.0
    tp2 = 115900.0
    tp3 = 117600.0

    sig = Signal(
        id="test-sig-12345",
        symbol="BTCUSDT",
        timeframe="1h",
        pattern="Bullish Flag",
        direction=Direction.LONG,
        entry_price=entry,
        stop_loss=stop,
        tp1=tp1, tp2=tp2, tp3=tp3,
        ema200_at_signal=110000.0,
        price_at_signal=entry,
        signal_time="2026-08-24T13:00:00Z",
    )
    pat = PatternResult("Bullish Flag", True, 10, 50, entry, stop, tp2)

    content = format_square_post(sig, pat)
    assert "$BTC" in content
    assert "112500" in content
    assert "110800" in content
    assert "117600" in content
    assert len(content) <= 600
    print("2. Deterministic Formatter (len <= 600): PASS")

    # 3. Test Validation - Valid Signal
    is_valid, reason = validate_square_post(sig, pat, daily_post_count=0, content=content)
    assert is_valid is True, f"Validation failed: {reason}"
    print("3. Valid Signal Validation: PASS")

    # 4. Test Validation - Missing/Invalid Entry
    invalid_sig = Signal(
        id="bad-1", symbol="BTCUSDT", timeframe="1h", pattern="Bullish Flag",
        direction=Direction.LONG, entry_price=-10.0, stop_loss=stop, tp1=tp1, tp2=tp2, tp3=tp3,
        ema200_at_signal=110000.0, price_at_signal=entry, signal_time="2026-08-24T13:00:00Z",
    )
    is_valid, reason = validate_square_post(invalid_sig, pat, 0, content)
    assert is_valid is False and "invalid_or_missing" in reason
    print("4. Invalid Entry Detection: PASS")

    # 5. Test Validation - Inverted SL (SL above Entry for Long)
    invalid_sl_sig = Signal(
        id="bad-2", symbol="BTCUSDT", timeframe="1h", pattern="Bullish Flag",
        direction=Direction.LONG, entry_price=entry, stop_loss=115000.0, tp1=tp1, tp2=tp2, tp3=tp3,
        ema200_at_signal=110000.0, price_at_signal=entry, signal_time="2026-08-24T13:00:00Z",
    )
    is_valid, reason = validate_square_post(invalid_sl_sig, pat, 0, content)
    assert is_valid is False and "invalid_long_price_ordering" in reason
    print("5. Inverted SL Detection: PASS")

    # 6. Test Validation - Daily Limit Reached
    is_valid, reason = validate_square_post(sig, pat, daily_post_count=50, content=content)
    assert is_valid is False and "daily_limit" in reason
    print("6. Daily Quota Limit Enforcement: PASS")

    # 7. Test Circuit Breaker
    cb = SquareCircuitBreaker()
    assert cb.is_open is False
    for _ in range(4):
        cb.record_failure("test error")
    assert cb.is_open is False
    cb.record_failure("5th error")
    assert cb.is_open is True  # Tripped
    cb.record_success()
    assert cb.is_open is False  # Reset
    print("7. Circuit Breaker Trips & Resets: PASS")

    # 8. Test Database Square Posts Table & Publisher Dry Run
    import uuid
    test_db_path = f"bot/data/test_square_{uuid.uuid4().hex[:8]}.db"
    
    db = Database(test_db_path)
    await db.init()


    # Enable Square & Dry Run for test
    settings.square_enabled = True
    settings.square_dry_run = True

    publisher = SquarePublisher(db=db)

    # 9. Test First Dry Run Publish with Chart Image
    mock_chart_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    res = await publisher.publish_signal(sig, pat, "Confirmed Breakout", chart_png=mock_chart_bytes)
    assert res.success is True
    assert res.status == SquareStatus.SKIPPED_DRY_RUN
    print("8. Publisher Dry Run Post with Chart Image: PASS")

    # 10. Test Duplicate Protection
    res_dup = await publisher.publish_signal(sig, pat, "Confirmed Breakout", chart_png=mock_chart_bytes)
    assert res_dup.success is True
    assert res_dup.status == SquareStatus.SKIPPED_DUPLICATE
    print("9. Duplicate Protection (Idempotency): PASS")


    # 11. Test Daily Count Tracking
    count = await db.get_square_posts_today_count()
    assert count == 1
    print("10. Daily Posts DB Tracking: PASS")

    # 12. Test Multi-Signal Production Processing & Duplicate Protection
    # In production mode, multiple distinct signals are published normally without 1x test lock
    settings.square_enabled = True
    settings.square_dry_run = True
    
    prod_publisher = SquarePublisher(db=db)

    # Create 5 distinct signals
    signals_batch = [
        Signal(
            id=f"prod-sig-{i}",
            symbol=f"COIN{i}USDT",
            timeframe="1h",
            pattern="Bullish Flag",
            direction=Direction.LONG,
            entry_price=100.0,
            stop_loss=90.0,
            tp1=110.0, tp2=120.0, tp3=130.0,
            ema200_at_signal=80.0,
            price_at_signal=100.0,
            signal_time="2026-08-24T14:00:00Z",
        )
        for i in range(5)
    ]
    
    # Process all 5 distinct signals
    batch_results = await asyncio.gather(
        *(prod_publisher.publish_signal(s, pat, "Breakout", chart_png=mock_chart_bytes) for s in signals_batch)
    )

    for r in batch_results:
        assert r.success is True
        assert r.status == SquareStatus.SKIPPED_DRY_RUN

    # Test that re-submitting the exact same batch is 100% caught by duplicate protection
    dup_results = await asyncio.gather(
        *(prod_publisher.publish_signal(s, pat, "Breakout", chart_png=mock_chart_bytes) for s in signals_batch)
    )
    for r in dup_results:
        assert r.success is True
        assert r.status == SquareStatus.SKIPPED_DUPLICATE

    print("12. Multi-Signal Production Processing & Duplicate Protection: PASS")


    # 13. Test Node.js Detection & Official post-image.mjs Dry Run Runner
    from bot.square.client import find_node_executable
    node_bin = find_node_executable()
    assert node_bin is not None, "Node.js executable should be detected on Windows"
    assert os.path.exists(node_bin), f"Node binary {node_bin} must exist"

    script_path = os.path.join(
        os.path.dirname(__file__), "..", "bot", "square", "binance_skill", "scripts", "post-image.mjs"
    )
    assert os.path.exists(script_path), f"Official post-image.mjs must exist at {script_path}"

    # Test client post_image in dry run mode
    res_img = await publisher.client.post_image(
        content="Test Signal",
        chart_path="bot/data/test_chart.png",
        dry_run=True,
    )
    assert res_img.success is True
    assert res_img.status == SquareStatus.SKIPPED_DRY_RUN
    print("13. Node.js Detection & Official post-image.mjs Dry Run Runner: PASS")

    # Clean up test db
    await db.close()
    if os.path.exists(test_db_path):
        os.remove(test_db_path)

    # Restore default safe settings
    settings.square_enabled = False
    settings.square_dry_run = True

    print("\nALL 13 BINANCE SQUARE UNIT TESTS (OFFICIAL SCRIPT INTEGRATION) PASSED SUCCESSFULLY!")

asyncio.run(run_tests())


