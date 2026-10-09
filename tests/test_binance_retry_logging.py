import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from bot.binance_client import BinanceMarket
from bot.config import settings


def candle(open_time):
    return [open_time, "1", "2", "0.5", "1.5", "10", open_time + 60000,
            "15", 1, "5", "7.5", "0"]


class BinanceRetryLoggingTests(unittest.IsolatedAsyncioTestCase):
    def market(self, responses):
        market = BinanceMarket()
        market._client = AsyncMock()
        market._rate_limiter.acquire = AsyncMock()
        market._client.futures_klines.side_effect = responses
        return market

    async def test_transient_timeout_then_success_is_logged_as_recovered(self):
        market = self.market([asyncio.TimeoutError(), [candle(0), candle(60000)]])
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

    async def test_scanner_logs_propagated_request_failure(self):
        from bot import main

        market = AsyncMock()
        market.get_symbol_universe.return_value = ["BCHUSDT"]
        market.get_klines.side_effect = asyncio.TimeoutError("request timed out")
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


if __name__ == "__main__":
    unittest.main()
