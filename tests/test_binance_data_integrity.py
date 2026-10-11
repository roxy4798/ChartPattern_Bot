import asyncio
import json
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pandas as pd

from bot.binance_client import BinanceMarket, CandleCache
from bot.config import Settings, settings
from bot.indicators import find_structural_pivots, find_multiscale_pivots
from bot.signal_engine import evaluate_symbol_timeframe
from bot.chart_generator import render_signal_chart
from scratch.test_structural_trendlines import generate_macro_channel_df


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "binance_usdm_klines_sample.json"
FIXTURE = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
NUMERIC_FIELDS = {
    "open": 1, "high": 2, "low": 3, "close": 4,
    "volume": 5, "quote_volume": 7,
}


class BinanceCandleIntegrityTests(unittest.IsolatedAsyncioTestCase):
    def market(self, rows):
        market = BinanceMarket()
        market._client = AsyncMock()
        market._rate_limiter.acquire = AsyncMock()
        market._client.futures_klines.return_value = rows
        return market

    async def test_saved_futures_fixture_exact_fields_and_closed_filter(self):
        captured_at = pd.Timestamp(FIXTURE["captured_at_utc"])
        for key, rows in FIXTURE["fixtures"].items():
            symbol, interval = key.split(":")
            with self.subTest(symbol=symbol, interval=interval):
                market = self.market(rows)
                with patch("bot.binance_client.pd.Timestamp.now", return_value=captured_at):
                    actual = await market.get_klines(symbol, interval, limit=2)

                # The fixture is captured while the third kline is still open.
                expected = rows[:2]
                self.assertEqual(len(actual), 2)
                self.assertEqual(
                    [list(row) for row in actual.attrs["binance_raw_klines"]],
                    expected,
                )
                self.assertEqual(actual["open_time"].dt.as_unit("ms").astype("int64").tolist(),
                                 [int(row[0]) for row in expected])
                self.assertEqual(actual["close_time"].dt.as_unit("ms").astype("int64").tolist(),
                                 [int(row[6]) for row in expected])
                self.assertEqual(actual["trades"].tolist(), [row[8] for row in expected])
                self.assertTrue((actual["close_time"] < captured_at).all())
                for column, index in NUMERIC_FIELDS.items():
                    for got, raw in zip(actual[column], (row[index] for row in expected)):
                        # Compare shortest round-trip decimal strings so binary
                        # float display noise is not mistaken for a market-data mismatch.
                        self.assertEqual(Decimal(str(got)), Decimal(raw), (key, column, raw, got))
                self.assertTrue(actual["open_time"].is_monotonic_increasing)
                self.assertFalse(actual["open_time"].duplicated().any())
                self.assertEqual(market._client.futures_klines.await_args.kwargs,
                                 {"symbol": symbol, "interval": interval, "limit": 3})
                market._client.klines.assert_not_awaited()

    async def test_cache_isolated_by_symbol_interval_and_required_history(self):
        cache = CandleCache()
        now = pd.Timestamp.now(tz="UTC")
        frame = pd.DataFrame({
            "open_time": pd.date_range(end=now, periods=3, freq="D"),
            "close_time": pd.date_range(end=now, periods=3, freq="D"),
        })
        cache.set("BTCUSDT", "1d", frame)
        self.assertEqual(len(cache.get("btcusdt", "1d", 2)), 2)
        self.assertIsNone(cache.get("BTCUSDT", "3d", 2))
        self.assertIsNone(cache.get("ETHUSDT", "1d", 2))
        self.assertIsNone(cache.get("BTCUSDT", "1d", 4))

    async def test_application_cache_reuses_only_sufficient_history(self):
        rows = FIXTURE["fixtures"]["BTCUSDT:1d"]
        market = self.market(rows)
        captured_at = pd.Timestamp(FIXTURE["captured_at_utc"])
        with patch("bot.binance_client.pd.Timestamp.now", return_value=captured_at):
            long_result = await market.get_klines("BTCUSDT", "1d", limit=2)
            short_result = await market.get_klines("BTCUSDT", "1d", limit=1)
            self.assertEqual(len(long_result), 2)
            self.assertEqual(len(short_result), 1)
            self.assertEqual(
                short_result.attrs["binance_raw_klines"],
                long_result.attrs["binance_raw_klines"][-1:],
            )
            self.assertEqual(market._client.futures_klines.await_count, 1)
            await market.get_klines("BTCUSDT", "1d", limit=3)
            self.assertEqual(market._client.futures_klines.await_count, 2)

    async def test_api_short_response_does_not_drop_latest_closed_row(self):
        row = [1_599_955_200_000, "1", "2", "0.5", "1.5", "10",
               1_600_041_599_999, "15", 1, "5", "7.5", "0"]
        market = self.market([row])
        with patch("bot.binance_client.pd.Timestamp.now",
                   return_value=pd.Timestamp("2026-10-11T00:00:00Z")):
            result = await market.get_klines("BTCUSDT", "1d", limit=1)
        self.assertEqual(len(result), 1)
        self.assertEqual(float(result.iloc[0]["close"]), 1.5)

    async def test_missing_daily_candle_fails_closed(self):
        first_open = 1_791_504_000_000
        opens = (first_open, first_open + 2 * 86_400_000)
        rows = [[ts, "1", "2", "0.5", "1.5", "10", ts + 86_399_999,
                 "15", 1, "5", "7.5", "0"] for ts in opens]
        market = self.market(rows)
        with patch("bot.binance_client.pd.Timestamp.now",
                   return_value=pd.Timestamp("2026-10-12T00:00:00Z")):
            with self.assertRaisesRegex(ValueError, "Misaligned or missing 1d candles"):
                await market.get_klines("BTCUSDT", "1d", limit=2)

    async def test_three_day_rows_require_cadence_without_phase_assumption(self):
        # Verify all three distinct 3D phases (offset 0, 1, and 2 days) are accepted
        phase_offsets = {}
        for symbol in ["BTCUSDT", "SEIUSDT", "CYBERUSDT"]:
            rows = FIXTURE["fixtures"][f"{symbol}:3d"]
            market = self.market(rows)
            with patch("bot.binance_client.pd.Timestamp.now",
                       return_value=pd.Timestamp("2026-10-20T00:00:00Z")):
                result = await market.get_klines(symbol, "3d", limit=3)
            self.assertEqual(len(result), 3)
            opens = result["open_time"].dt.as_unit("ms").astype("int64").tolist()
            self.assertEqual([b - a for a, b in zip(opens, opens[1:])], [259_200_000] * 2)
            phase_offsets[symbol] = (opens[0] % 259_200_000) // 86_400_000

        # Assert all three distinct mathematical phases (0, 1, 2) are represented
        self.assertEqual(phase_offsets["CYBERUSDT"], 0)
        self.assertEqual(phase_offsets["BTCUSDT"], 1)
        self.assertEqual(phase_offsets["SEIUSDT"], 2)

    async def test_three_day_missing_candle_fails_closed(self):
        first_open = 1_791_158_400_000
        opens = (first_open, first_open + 2 * 259_200_000)
        rows = [[ts, "1", "2", "0.5", "1.5", "10", ts + 259_199_999,
                 "15", 1, "5", "7.5", "0"] for ts in opens]
        market = self.market(rows)
        with patch("bot.binance_client.pd.Timestamp.now",
                   return_value=pd.Timestamp("2026-10-20T00:00:00Z")):
            with self.assertRaisesRegex(ValueError, "Misaligned or missing 3d candles"):
                await market.get_klines("BTCUSDT", "3d", limit=2)

    async def test_three_day_missing_candle_surfaces_structured_gap_diagnostics(self):
        first_open = 1_791_158_400_000
        opens = (first_open, first_open + 2 * 259_200_000)
        rows = [[ts, "1", "2", "0.5", "1.5", "10", ts + 259_199_999,
                 "15", 1, "5", "7.5", "0"] for ts in opens]
        market = self.market(rows)
        with patch("bot.binance_client.pd.Timestamp.now",
                   return_value=pd.Timestamp("2026-10-20T00:00:00Z")):
            with self.assertRaises(ValueError) as ctx:
                await market.get_klines("BTCUSDT", "3d", limit=2)
            msg = str(ctx.exception)
            self.assertIn("Misaligned or missing 3d candles received for BTCUSDT", msg)
            self.assertIn("total_candles=2", msg)
            self.assertIn("missing/gap: 1 gap(s) found", msg)
            self.assertIn("delta=518400000ms", msg)
            self.assertIn("expected=259200000ms", msg)

    async def test_upstream_boundary_gap_with_sufficient_history_retains_contiguous_suffix(self):
        # 10 historical rows prior to August 2023 boundary, 1-day gap, then 230 contiguous 3d rows
        interval_ms = 259_200_000
        start_ts = 1_689_000_000_000
        disjoint_rows = [
            [start_ts + i * interval_ms, "1", "2", "0.5", "1.5", "10",
             start_ts + i * interval_ms + interval_ms - 1, "15", 1, "5", "7.5", "0"]
            for i in range(10)
        ]
        # Step of 86_400_000 (1 day) instead of 259_200_000 (3 days)
        boundary_ts = disjoint_rows[-1][0] + 86_400_000
        contiguous_rows = [
            [boundary_ts + i * interval_ms, "1", "2", "0.5", "1.5", "10",
             boundary_ts + i * interval_ms + interval_ms - 1, "15", 1, "5", "7.5", "0"]
            for i in range(230)
        ]
        all_rows = disjoint_rows + contiguous_rows
        market = self.market(all_rows)
        # Pretend current time is after the last row
        last_close_dt = pd.Timestamp(contiguous_rows[-1][6], unit="ms", tz="UTC") + pd.Timedelta(hours=1)
        with patch("bot.binance_client.pd.Timestamp.now", return_value=last_close_dt):
            result = await market.get_klines("BTCUSDT", "3d", limit=240)

        self.assertEqual(len(result), 230)
        opens = result["open_time"].dt.as_unit("ms").astype("int64").tolist()
        # Verify 100% contiguous cadence with zero gaps
        self.assertEqual([b - a for a, b in zip(opens, opens[1:])], [interval_ms] * 229)
        self.assertEqual(len(result.attrs["binance_raw_klines"]), 230)
        # Verify cache hits for limit <= 240
        cached = market._candle_cache.get("BTCUSDT", "3d", limit=240)
        self.assertIsNotNone(cached)
        self.assertEqual(len(cached), 230)

    async def test_one_day_gap_fails_closed_even_with_large_contiguous_suffix(self):
        # 10 daily rows, 2-day gap, then 230 contiguous daily rows (all phase-aligned at 00:00 UTC)
        interval_ms = 86_400_000
        first_open = 1_700_000_000_000 - (1_700_000_000_000 % interval_ms)
        disjoint_rows = [
            [first_open + i * interval_ms, "1", "2", "0.5", "1.5", "10",
             first_open + i * interval_ms + interval_ms - 1, "15", 1, "5", "7.5", "0"]
            for i in range(10)
        ]
        # Skip 2 days (gap)
        boundary_open = disjoint_rows[-1][0] + 2 * interval_ms
        contiguous_rows = [
            [boundary_open + i * interval_ms, "1", "2", "0.5", "1.5", "10",
             boundary_open + i * interval_ms + interval_ms - 1, "15", 1, "5", "7.5", "0"]
            for i in range(230)
        ]
        all_rows = disjoint_rows + contiguous_rows
        market = self.market(all_rows)
        last_close_dt = pd.Timestamp(contiguous_rows[-1][6], unit="ms", tz="UTC") + pd.Timedelta(hours=1)
        with patch("bot.binance_client.pd.Timestamp.now", return_value=last_close_dt):
            with self.assertRaises(ValueError) as ctx:
                await market.get_klines("BTCUSDT", "1d", limit=240)
            msg = str(ctx.exception)
            self.assertIn("Misaligned or missing 1d candles received for BTCUSDT", msg)
            self.assertIn("missing/gap: 1 gap(s) found", msg)

    async def test_one_week_gap_fails_closed_even_with_large_contiguous_suffix(self):
        # Weekly rows (Monday 00:00 UTC: ts % 604_800_000 == 345_600_000)
        interval_ms = 604_800_000
        offset_ms = 345_600_000
        base_ts = 1_700_000_000_000
        first_open = base_ts - (base_ts % interval_ms) + offset_ms
        if first_open > base_ts:
            first_open -= interval_ms
        disjoint_rows = [
            [first_open + i * interval_ms, "1", "2", "0.5", "1.5", "10",
             first_open + i * interval_ms + interval_ms - 1, "15", 1, "5", "7.5", "0"]
            for i in range(10)
        ]
        # Skip 2 weeks (gap)
        boundary_open = disjoint_rows[-1][0] + 2 * interval_ms
        contiguous_rows = [
            [boundary_open + i * interval_ms, "1", "2", "0.5", "1.5", "10",
             boundary_open + i * interval_ms + interval_ms - 1, "15", 1, "5", "7.5", "0"]
            for i in range(230)
        ]
        all_rows = disjoint_rows + contiguous_rows
        market = self.market(all_rows)
        last_close_dt = pd.Timestamp(contiguous_rows[-1][6], unit="ms", tz="UTC") + pd.Timedelta(hours=1)
        with patch("bot.binance_client.pd.Timestamp.now", return_value=last_close_dt):
            with self.assertRaises(ValueError) as ctx:
                await market.get_klines("BTCUSDT", "1w", limit=240)
            msg = str(ctx.exception)
            self.assertIn("Misaligned or missing 1w candles received for BTCUSDT", msg)
            self.assertIn("missing/gap: 1 gap(s) found", msg)

    async def test_cache_does_not_treat_trimmed_suffix_as_sufficient_for_larger_limit(self):
        # 3D cache entry with 230 bars and requested_limit=240
        now = pd.Timestamp.now(tz="UTC")
        frame_3d = pd.DataFrame({
            "open_time": pd.date_range(end=now, periods=230, freq="3D"),
            "close_time": pd.date_range(end=now, periods=230, freq="3D"),
        })
        frame_3d.attrs["requested_limit"] = 240
        frame_3d.attrs["is_contiguous_boundary_trimmed"] = True
        cache = CandleCache()
        cache.set("BTCUSDT", "3d", frame_3d)

        # limit <= 240 returns 230 bars (HIT)
        self.assertEqual(len(cache.get("BTCUSDT", "3d", limit=240)), 230)
        # limit=500 requires re-fetch because 240 < 500 (MISS)
        self.assertIsNone(cache.get("BTCUSDT", "3d", limit=500))

        # 1D cache entry with 230 bars (never boundary trimmed)
        frame_1d = pd.DataFrame({
            "open_time": pd.date_range(end=now, periods=230, freq="1D"),
            "close_time": pd.date_range(end=now, periods=230, freq="1D"),
        })
        frame_1d.attrs["requested_limit"] = 500
        cache.set("BTCUSDT", "1d", frame_1d)
        # 1D strictly requires len(entry.df) >= limit
        self.assertEqual(len(cache.get("BTCUSDT", "1d", limit=230)), 230)
        self.assertIsNone(cache.get("BTCUSDT", "1d", limit=500))

    async def test_candle_validation_surfaces_structured_bad_close_and_misaligned(self):
        open_ts = 1_791_504_000_100  # not a multiple of 86_400_000
        close_ts = open_ts + 86_300_000  # not open + 86_399_999
        rows = [[open_ts, "1", "2", "0.5", "1.5", "10", close_ts,
                 "15", 1, "5", "7.5", "0"]]
        market = self.market(rows)
        with patch("bot.binance_client.pd.Timestamp.now",
                   return_value=pd.Timestamp("2026-10-20T00:00:00Z")):
            with self.assertRaises(ValueError) as ctx:
                await market.get_klines("BTCUSDT", "1d", limit=1)
            msg = str(ctx.exception)
            self.assertIn("misaligned: 1 candle(s) with bad phase", msg)
            self.assertIn("bad_close: 1 candle(s)", msg)

    async def test_rejects_excessive_limit_that_cannot_request_extra_row(self):
        market = self.market([])
        with self.assertRaisesRegex(ValueError, "between 1 and 1499"):
            await market.get_klines("BTCUSDT", "1d", limit=1500)
        market._client.futures_klines.assert_not_awaited()

    async def test_request_weight_uses_limit_plus_extra_api_row(self):
        rows = FIXTURE["fixtures"]["BTCUSDT:1d"]
        market = self.market(rows)
        with patch("bot.binance_client.pd.Timestamp.now",
                   return_value=pd.Timestamp(FIXTURE["captured_at_utc"])):
            await market.get_klines("BTCUSDT", "1d", limit=99)
        market._client.futures_klines.assert_awaited_once_with(
            symbol="BTCUSDT", interval="1d", limit=100
        )
        market._rate_limiter.acquire.assert_awaited_once_with(weight=2)

    async def test_network_failure_is_not_relabelled_as_fresh_cache_data(self):
        market = self.market([])
        market._client.futures_klines.side_effect = asyncio.TimeoutError("timeout")
        market._rate_limiter.acquire = AsyncMock()
        with patch("bot.binance_client.asyncio.sleep", new=AsyncMock()), \
             self.assertRaises(asyncio.TimeoutError):
            await market.get_klines("BTCUSDT", "1d", limit=1, force_refresh=True)
        self.assertIsNone(market._candle_cache.get("BTCUSDT", "1d", 1))


class StructuralFractalConfigurationTests(unittest.TestCase):
    def test_application_configuration_rejects_non_30_period(self):
        with self.assertRaisesRegex(ValueError, "exactly 30"):
            Settings(structural_fractal_period=29)
        with self.assertRaisesRegex(ValueError, "exactly 30"):
            settings.structural_fractal_period = 29
        self.assertEqual(settings.structural_fractal_period, 30)
        self.assertEqual(settings.structural_min_span, 65)

    def test_structural_detector_rejects_period_and_confirmation_overrides(self):
        frame = pd.DataFrame({"high": [2.0] * 80, "low": [1.0] * 80})
        with self.assertRaisesRegex(ValueError, "fractal_period must be exactly 30"):
            find_structural_pivots(frame, fractal_period=29)
        with self.assertRaisesRegex(ValueError, "confirmation_bars must be exactly 20"):
            find_structural_pivots(frame, fractal_period=30, confirmation_bars=19)
        with self.assertRaisesRegex(ValueError, "major_lb must be exactly 30"):
            find_multiscale_pivots(frame, major_lb=25)

    def test_pivot_waits_for_twenty_confirmation_bars(self):
        highs = [1.0] * 70
        lows = [0.5] * 70
        highs[35] = 5.0
        lows[35] = 0.1
        frame = pd.DataFrame({"high": highs, "low": lows})
        highs_result, lows_result = find_structural_pivots(frame, fractal_period=30)
        self.assertEqual([p.index for p in highs_result], [35])
        self.assertEqual([p.index for p in lows_result], [35])
        short_highs, short_lows = find_structural_pivots(frame.iloc[:-15], fractal_period=30)
        self.assertEqual(short_highs, [])
        self.assertEqual(short_lows, [])

    def test_signal_and_chart_recalculate_identical_confirmed_pivots(self):
        candles = generate_macro_channel_df(n_bars=500)
        candles["close_time"] = candles["open_time"] + pd.Timedelta(days=1) - pd.Timedelta(milliseconds=1)
        expected = find_structural_pivots(
            candles, fractal_period=30,
            min_pivot_dist=settings.trendline_min_pivot_dist,
        )

        from bot import signal_engine
        from bot import chart_generator
        from matplotlib.axes import Axes
        captures = {"signal": {}, "chart": {}}
        chart_markers = []

        def capture_pivots(component):
            def capture(df, **kwargs):
                captures[component]["kwargs"] = kwargs
                captures[component]["data"] = df.copy(deep=True)
                captures[component]["pivots"] = find_structural_pivots(df, **kwargs)
                return captures[component]["pivots"]
            return capture

        original_scatter = Axes.scatter

        def capture_scatter(ax, x, y, *args, **kwargs):
            if kwargs.get("s") == 28:
                chart_markers.append((x, y, kwargs.get("marker")))
            return original_scatter(ax, x, y, *args, **kwargs)

        with patch.object(signal_engine, "find_structural_pivots", side_effect=capture_pivots("signal")), \
             patch.object(chart_generator, "find_structural_pivots", side_effect=capture_pivots("chart")), \
             patch.object(Axes, "scatter", new=capture_scatter):
            signal, pattern, _ = evaluate_symbol_timeframe("BTCUSDT", "1d", candles)
            self.assertIsNotNone(signal)
            self.assertIsNotNone(pattern)
            png = render_signal_chart(signal.symbol, signal.timeframe, candles, pattern, signal)

        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        for component in ("signal", "chart"):
            self.assertEqual(captures[component]["kwargs"]["fractal_period"], 30)
            self.assertEqual(captures[component]["kwargs"]["min_pivot_dist"],
                             settings.trendline_min_pivot_dist)
            self.assertEqual(captures[component]["pivots"], expected)
            pd.testing.assert_frame_equal(captures[component]["data"], candles)
        self.assertEqual(captures["signal"]["pivots"], captures["chart"]["pivots"])
        pattern_len = len(candles) - max(0, pattern.start_idx)
        plot_len = min(len(candles), max(150, pattern_len + 20))
        offset = len(candles) - plot_len
        expected_markers = [
            (max(0, min(plot_len - 1, pivot.index - offset)), pivot.price, marker)
            for pivots, marker in ((expected[0], "^"), (expected[1], "v"))
            for pivot in pivots
            if pattern.start_idx <= pivot.index <= pattern.breakout_idx
        ]
        self.assertEqual(chart_markers, expected_markers)


class SignalValidationDiagnosticsTests(unittest.TestCase):
    def test_validate_signal_surfaces_invalid_field_and_pattern_context(self):
        from bot.models import Signal, Direction, SignalStatus, PatternResult
        from bot.signal_engine import _validate_signal

        pattern = PatternResult(
            name="Descending Channel",
            is_bullish=True,
            start_idx=0,
            breakout_idx=10,
            entry_price=1.95,
            stop_price=-0.57525,
            target_price=5.317,
        )
        signal = Signal(
            id="test-id",
            symbol="ATOMUSDT",
            timeframe="1d",
            pattern="Descending Channel",
            direction=Direction.LONG,
            entry_price=1.95,
            stop_loss=-0.57525,
            tp1=4.47525,
            tp2=7.0005,
            tp3=9.52575,
            price_at_signal=1.95,
            signal_time="2026-10-10T00:00:00Z",
            status=SignalStatus.ACTIVE,
        )

        with self.assertRaises(ValueError) as ctx:
            _validate_signal(signal, pattern)

        msg = str(ctx.exception)
        self.assertIn("Signal for ATOMUSDT 1d (Descending Channel)", msg)
        self.assertIn("non-positive or non-finite prices", msg)
        self.assertIn("stop_loss=-0.57525", msg)
        self.assertIn("context:", msg)
        self.assertIn("entry_price=1.95", msg)
        self.assertNotIn("target_price", msg)  # only signal prices in map

    def test_rr_ok_rejects_non_positive_and_non_finite_stop_prices(self):
        from bot.patterns import _rr_ok

        # Negative stop loss (like ATOMUSDT descending channel case)
        self.assertFalse(_rr_ok(entry=1.95, stop=-0.57525, target=5.317, is_bull=True))
        # Zero stop loss
        self.assertFalse(_rr_ok(entry=1.95, stop=0.0, target=5.317, is_bull=True))
        # Non-finite prices
        self.assertFalse(_rr_ok(entry=1.95, stop=float("nan"), target=5.317, is_bull=True))
        self.assertFalse(_rr_ok(entry=float("inf"), stop=1.0, target=5.317, is_bull=True))
        # Valid positive risk-reward
        self.assertTrue(_rr_ok(entry=1.95, stop=1.5, target=5.317, is_bull=True))


if __name__ == "__main__":
    unittest.main()
