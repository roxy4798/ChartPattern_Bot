import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd

from bot.binance_client import BinanceMarket
from bot.config import settings


BASE_WEEK = 1_704_067_200_000  # Monday 2024-01-01 00:00 UTC


def candle(open_time):
    return [open_time, "1", "2", "0.5", "1.5", "10", open_time + 604800000 - 1,
            "15", 1, "5", "7.5", "0"]


class BinanceRetryLoggingTests(unittest.IsolatedAsyncioTestCase):
    def market(self, responses):
        market = BinanceMarket()
        market._client = AsyncMock()
        market._rate_limiter.acquire = AsyncMock()
        market._client.futures_klines.side_effect = responses
        return market

    async def test_transient_timeout_then_success_is_logged_as_recovered(self):
        market = self.market([asyncio.TimeoutError(), [candle(BASE_WEEK), candle(BASE_WEEK + 604800000)]])
        with patch("bot.binance_client.asyncio.sleep", new=AsyncMock()), \
             self.assertLogs("binance", level="INFO") as logs:
            df = await market.get_klines("BCHUSDT", "1w", 1)

        self.assertEqual(len(df), 1)
        self.assertTrue(any("recovered for BCHUSDT 1w after 2 attempt(s)" in line for line in logs.output))
        self.assertEqual(market._client.futures_klines.await_count, 2)

    async def test_all_transient_attempts_exhausted_logs_final_error(self):
        market = self.market([asyncio.TimeoutError("slow") for _ in range(4)])
        with patch("bot.binance_client.asyncio.sleep", new=AsyncMock()), \
             self.assertLogs("binance", level="ERROR") as logs:
            with self.assertRaises(asyncio.TimeoutError):
                await market.get_klines("BCHUSDT", "1w", 1)

        final = logs.output[-1]
        self.assertIn("BCHUSDT 1w", final)
        self.assertIn("elapsed=", final)
        self.assertIn("attempt=4/4", final)
        self.assertIn("exception=TimeoutError", final)
        self.assertIn("source=client/transport", final)

    async def test_asyncio_timeout_deadline_is_identified(self):
        async def never_finishes(**kwargs):
            await asyncio.get_running_loop().create_future()

        market = self.market([])
        market._client.futures_klines.side_effect = never_finishes
        with patch.object(settings, "request_timeout_sec", 0.01), \
             patch("bot.binance_client.asyncio.sleep", new=AsyncMock()), \
             self.assertLogs("binance", level="ERROR") as logs:
            with self.assertRaises(TimeoutError):
                await market.get_klines("BCHUSDT", "1w", 1)

        self.assertIn("source=asyncio.timeout deadline", logs.output[-1])

    async def test_rate_limiter_wait_is_covered_by_request_timeout(self):
        market = self.market([])

        async def stalled_limiter(**kwargs):
            await asyncio.get_running_loop().create_future()

        market._rate_limiter.acquire.side_effect = stalled_limiter
        with patch.object(settings, "request_timeout_sec", 0.01), \
             patch("bot.binance_client.asyncio.sleep", new=AsyncMock()), \
             self.assertLogs("binance", level="ERROR") as logs:
            with self.assertRaises(TimeoutError):
                await market.get_klines("BCHUSDT", "1w", 1)

        self.assertEqual(market._client.futures_klines.await_count, 0)
        self.assertEqual(market.candle_health()["failures"], 4)
        self.assertIn("source=asyncio.timeout deadline", logs.output[-1])

    async def test_scanner_logs_propagated_request_failure(self):
        from bot import main

        market = AsyncMock()
        market.get_symbol_universe.return_value = ["BCHUSDT"]
        market.get_klines.side_effect = asyncio.TimeoutError("request timed out")
        market.candle_health = MagicMock(return_value={
            "requests": 1, "failures": 1, "recovered": 0, "exhausted": 1,
            "validation_failures": 0, "cache_hits": 0, "last_successful_fetch_at": None,
        })
        db = AsyncMock()
        db.get_active_signals.return_value = []

        async def finish_scan(awaitable, *args, **kwargs):
            awaitable.close()
            main._shutdown.set()
            raise asyncio.TimeoutError()

        with patch.object(main, "_shutdown", asyncio.Event()), \
             patch.object(settings, "timeframes", ["1w"]), \
             patch.object(settings, "scanner_concurrency", 1), \
             patch.object(main.asyncio, "wait_for", side_effect=finish_scan), \
             self.assertLogs("main", level="WARNING") as logs:
            await main.scan_loop(market, db, AsyncMock())

        self.assertTrue(any("Scan request failed for BCHUSDT 1w (TimeoutError" in line for line in logs.output))
        self.assertTrue(any(call.args[0] == "last_scan_pair_failures" and call.args[1] == "1"
                            for call in db.set_metadata.await_args_list))
        self.assertFalse(any(call.args[0] == "last_successful_scan_at"
                             for call in db.set_metadata.await_args_list))

    async def test_scanner_surfaces_non_binance_pair_exception(self):
        from bot import main

        market = AsyncMock()
        market.get_symbol_universe.return_value = ["BCHUSDT"]
        market.get_klines.return_value = pd.DataFrame({"close": [1.0] * 220})
        market.candle_health = MagicMock(return_value={
            "requests": 1, "failures": 0, "recovered": 0, "exhausted": 0,
            "validation_failures": 0, "cache_hits": 0, "last_successful_fetch_at": "now",
        })
        db = AsyncMock()
        db.get_active_signals.return_value = []

        async def finish_scan(awaitable, *args, **kwargs):
            awaitable.close()
            main._shutdown.set()
            raise asyncio.TimeoutError()

        with patch.object(main, "_shutdown", asyncio.Event()), \
             patch.object(settings, "timeframes", ["1w"]), \
             patch.object(settings, "scanner_concurrency", 1), \
             patch.object(main, "evaluate_symbol_timeframe", side_effect=ValueError("bad candle frame")), \
             patch.object(main.asyncio, "wait_for", side_effect=finish_scan), \
             self.assertLogs("main", level="WARNING") as logs:
            await main.scan_loop(market, db, AsyncMock())

        self.assertTrue(any("Scan pair failed for BCHUSDT 1w (ValueError: bad candle frame)" in line
                            for line in logs.output))
        self.assertTrue(any(call.args[0] == "last_scan_pair_failures" and call.args[1] == "1"
                            for call in db.set_metadata.await_args_list))
        self.assertTrue(any(call.args[0] == "binance_failures" and call.args[1] == "0"
                            for call in db.set_metadata.await_args_list))

    async def test_scanner_isolates_signal_with_non_positive_price(self):
        from bot import main

        market = AsyncMock()
        market.get_symbol_universe.return_value = ["ATOMUSDT", "BTCUSDT"]
        market.get_klines.return_value = pd.DataFrame({"close": [1.0] * 220})
        market.candle_health = MagicMock(return_value={
            "requests": 2, "failures": 0, "recovered": 0, "exhausted": 0,
            "validation_failures": 0, "cache_hits": 0, "last_successful_fetch_at": "now",
        })
        db = AsyncMock()
        db.get_active_signals.return_value = []

        evaluated_symbols = []

        def mock_eval(symbol, tf, df, scan_id=None):
            evaluated_symbols.append(symbol)
            if symbol == "ATOMUSDT":
                raise ValueError(
                    "Signal for ATOMUSDT 1d (Descending Channel) contains non-positive or "
                    "non-finite prices: stop_loss=-0.57525 [context: entry_price=1.95, stop_loss=-0.57525, tp1=4.47525]"
                )
            return None, None, None

        async def finish_scan(awaitable, *args, **kwargs):
            awaitable.close()
            main._shutdown.set()
            raise asyncio.TimeoutError()

        with patch.object(main, "_shutdown", asyncio.Event()), \
             patch.object(settings, "timeframes", ["1d"]), \
             patch.object(settings, "scanner_concurrency", 2), \
             patch.object(main, "evaluate_symbol_timeframe", side_effect=mock_eval), \
             patch.object(main.asyncio, "wait_for", side_effect=finish_scan), \
             self.assertLogs("main", level="WARNING") as logs:
            await main.scan_loop(market, db, AsyncMock())

        self.assertIn("ATOMUSDT", evaluated_symbols)
        self.assertIn("BTCUSDT", evaluated_symbols)
        self.assertTrue(any("Scan pair failed for ATOMUSDT 1d (ValueError: Signal for ATOMUSDT 1d (Descending Channel)" in line
                            for line in logs.output))
        self.assertTrue(any(call.args[0] == "last_scan_pair_failures" and call.args[1] == "1"
                            for call in db.set_metadata.await_args_list))


if __name__ == "__main__":
    unittest.main()
