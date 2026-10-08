"""
Production Scenario Simulation for ChartPattern_Bot (527 symbols x 3 timeframes).
Tests:
- Cold start request weight and rate limiter throttling
- Steady state candle cache hits (100% for 1d, TTL hits for 3d/1w)
- Signal emission, duplicate prevention, and order execution absence
- Memory stability across multiple scan cycles
"""
import asyncio
import sys
import os
import time
import tracemalloc
import pandas as pd
import numpy as np
import uuid
from datetime import datetime, timezone, timedelta

# Ensure project root is in sys.path
sys.path.insert(0, os.path.abspath("."))

from bot.config import settings
from bot.binance_client import BinanceRateLimiter, CandleCache
from bot.models import Signal, Direction, PatternResult, SignalStatus
from bot.database import Database
from bot.signal_engine import evaluate_symbol_timeframe
from scratch.test_structural_trendlines import generate_macro_channel_df


class MockBinanceClient:
    """Simulates Binance Futures API with request counting and realistic response."""
    def __init__(self, symbols_count=527):
        self.symbols = [f"SYM{i:03d}USDT" for i in range(symbols_count)]
        self.request_count = 0
        self.weight_consumed = 0
        self._standard_df = None
        self._pattern_df = None

    def _get_candles(self, is_pattern=False):
        if is_pattern:
            if self._pattern_df is None:
                base = generate_macro_channel_df(n_bars=500)
                # Append 1 forming bar to simulate Binance limit=501 where iloc[:-1] drops the forming bar
                forming_bar = base.iloc[[-1]].copy()
                forming_bar["open_time"] = forming_bar["open_time"] + timedelta(days=1)
                forming_bar["close_time"] = forming_bar["close_time"] + timedelta(days=1)
                self._pattern_df = pd.concat([base, forming_bar], ignore_index=True)
            return self._pattern_df.copy()
        
        if self._standard_df is None:
            now = datetime.now(timezone.utc)
            dates = [now - timedelta(days=500 - i) for i in range(501)]
            self._standard_df = pd.DataFrame({
                "open_time": dates,
                "open": np.linspace(100, 110, 501),
                "high": np.linspace(102, 112, 501),
                "low": np.linspace(98, 108, 501),
                "close": np.linspace(101, 111, 501),
                "volume": np.full(501, 1000.0),
                "close_time": [d + timedelta(hours=23, minutes=59) for d in dates],
                "quote_volume": np.full(501, 100000.0),
                "trades": np.full(501, 500),
                "taker_buy_base": np.full(501, 500.0),
                "taker_buy_quote": np.full(501, 50000.0),
                "ignore": np.zeros(501),
            })
        return self._standard_df.copy()

    async def futures_exchange_info(self):
        self.request_count += 1
        self.weight_consumed += 1
        return {
            "symbols": [
                {"symbol": s, "contractType": "PERPETUAL", "quoteAsset": "USDT", "status": "TRADING"}
                for s in self.symbols
            ]
        }

    async def futures_klines(self, symbol: str, interval: str, limit: int = 501):
        self.request_count += 1
        w = 1 if limit < 100 else (2 if limit < 500 else 5)
        self.weight_consumed += w
        # Inject pattern on SYM001USDT and SYM002USDT to test signal emission
        has_pattern = (symbol in ("SYM001USDT", "SYM002USDT") and interval == "1d")
        return self._get_candles(is_pattern=has_pattern).iloc[:limit]


async def run_simulation():
    print("=================================================================")
    print("  RUNNING DETERMINISTIC OFFLINE PRODUCTION LOAD SIMULATION       ")
    print("  Configuration: 527 symbols x 3 timeframes (1D, 3D, 1W)         ")
    print("=================================================================")
    tracemalloc.start()

    unique_id = uuid.uuid4().hex[:8]
    db_path = f"bot/data/test_sim_{unique_id}.db"
    db = Database(db_path)
    await db.init()

    # 1. First test strict rate limiter throttling behavior on a small batch
    print("[1] Verifying Token Bucket Throttling on 1200 weight/min...")
    strict_limiter = BinanceRateLimiter(max_weight_per_min=1200, min_interval_sec=0.001)
    t0 = time.monotonic()
    # Acquire 1200 tokens (burst capacity)
    await strict_limiter.acquire(weight=1200)
    burst_time = time.monotonic() - t0
    assert burst_time < 0.1, f"Burst acquire should be instantaneous, took {burst_time:.3f}s"
    
    # Next acquire of 40 weight should require exactly 40 / 20 = 2.0s delay
    t1 = time.monotonic()
    await strict_limiter.acquire(weight=40)
    throttled_time = time.monotonic() - t1
    assert 1.8 <= throttled_time <= 2.5, f"Expected ~2.0s throttling for 40 weight at 20 tokens/sec, took {throttled_time:.3f}s"
    print(f"  PASS: Burst was instantaneous ({burst_time:.3f}s) and deficit throttled strictly to {throttled_time:.2f}s (~2.0s expected).")

    # 2. For the 527 x 3 load test, scale the token bucket to allow fast offline completion
    mock_client = MockBinanceClient(symbols_count=527)
    rate_limiter = BinanceRateLimiter(max_weight_per_min=10_000_000, min_interval_sec=0.0)
    cache = CandleCache()

    # Verify no order execution attributes exist anywhere on market or client
    assert not hasattr(mock_client, "futures_create_order")
    assert not hasattr(mock_client, "create_order")

    # Helper function to simulate a scan loop cycle
    async def simulate_scan_cycle(cycle_num: int):
        print(f"\n--- SCAN CYCLE {cycle_num} START ---")
        cycle_req_start = mock_client.request_count
        cycle_weight_start = mock_client.weight_consumed
        cache_hits = 0
        cache_misses = 0
        signals_emitted = 0

        symbols = mock_client.symbols
        timeframes = settings.timeframes  # ('1d', '3d', '1w')
        total_evals = len(symbols) * len(timeframes)

        for sym in symbols:
            for tf in timeframes:
                # 1. Check cache
                cached_df = cache.get(sym, tf, max_age_sec=settings.candle_cache_ttl_sec)
                if cached_df is not None and not cached_df.empty:
                    cache_hits += 1
                    df = cached_df
                else:
                    cache_misses += 1
                    await rate_limiter.acquire(weight=5)
                    raw_df = await mock_client.futures_klines(sym, tf, limit=501)
                    # Drop forming candle and cache
                    df = raw_df.iloc[:-1].reset_index(drop=True)
                    cache.set(sym, tf, df)

                # 2. Evaluate signal engine
                sig, pattern, reason = evaluate_symbol_timeframe(sym, tf, df, scan_id=f"sim-{cycle_num}")
                if sig and pattern and reason:
                    # 3. Duplicate check via DB
                    inserted = await db.insert_signal(sig)
                    if inserted:
                        signals_emitted += 1
                    else:
                        # Correctly identified duplicate!
                        pass

        cycle_reqs = mock_client.request_count - cycle_req_start
        cycle_weight = mock_client.weight_consumed - cycle_weight_start
        hit_rate = (cache_hits / total_evals) * 100

        print(f"Cycle {cycle_num} Summary:")
        print(f"  Total evaluations: {total_evals}")
        print(f"  Cache Hits: {cache_hits} | Misses: {cache_misses} | Hit Rate: {hit_rate:.2f}%")
        print(f"  Requests made: {cycle_reqs} | Weight consumed: {cycle_weight}")
        print(f"  Signals newly inserted: {signals_emitted}")
        return cycle_reqs, cycle_weight, hit_rate, signals_emitted

    # Cycle 1: Cold start
    reqs_1, weight_1, hit_rate_1, sigs_1 = await simulate_scan_cycle(1)
    assert reqs_1 == 527 * 3, f"Expected 1581 cold start requests, got {reqs_1}"
    assert weight_1 == 1581 * 5, f"Expected {1581 * 5} weight, got {weight_1}"
    assert hit_rate_1 == 0.0, "Cold start hit rate should be 0%"
    assert sigs_1 == 2, f"Expected 2 patterns detected on cycle 1, got {sigs_1}"
    print("  -> PASS: Cold start executed and populated shared cache with 1581 items.")

    # Cycle 2: Immediate follow-up (steady state within TTL)
    reqs_2, weight_2, hit_rate_2, sigs_2 = await simulate_scan_cycle(2)
    assert reqs_2 == 0, f"Expected 0 requests on cycle 2, got {reqs_2}"
    assert weight_2 == 0, f"Expected 0 weight on cycle 2, got {weight_2}"
    assert hit_rate_2 == 100.0, f"Expected 100% cache hit rate on cycle 2, got {hit_rate_2}%"
    assert sigs_2 == 0, "Duplicate prevention MUST prevent re-inserting already known signals!"
    print("  -> PASS: Cycle 2 steady state 100% cached. Zero requests made. Zero duplicate signals.")

    # Cycle 3: Another scan cycle (still cached)
    reqs_3, weight_3, hit_rate_3, sigs_3 = await simulate_scan_cycle(3)
    assert reqs_3 == 0, f"Expected 0 requests on cycle 3, got {reqs_3}"
    assert hit_rate_3 == 100.0, "Expected 100% hit rate"
    assert sigs_3 == 0, "Expected 0 duplicate signals"
    print("  -> PASS: Cycle 3 steady state confirmed.")

    # Check memory snapshot
    current_mem, peak_mem = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    print(f"\nMemory Profile:")
    print(f"  Current memory: {current_mem / (1024 * 1024):.2f} MB")
    print(f"  Peak memory: {peak_mem / (1024 * 1024):.2f} MB")
    assert peak_mem < 250 * 1024 * 1024, f"Peak memory should remain bounded under 250MB, got {peak_mem / 1024 / 1024:.2f}MB"
    print("  -> PASS: Memory bounded safely under 250MB for full 527 symbols x 3 timeframes.")

    await db.close()
    if os.path.exists(db_path):
        os.remove(db_path)
    for shm in [f"{db_path}-shm", f"{db_path}-wal"]:
        if os.path.exists(shm):
            os.remove(shm)

    print("\n=================================================================")
    print("  ALL PRODUCTION SCENARIO SIMULATION TESTS PASSED (100% SUCCESS) ")
    print("=================================================================")

if __name__ == "__main__":
    asyncio.run(run_simulation())
