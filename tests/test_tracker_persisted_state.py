import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pandas as pd

from bot.database import Database
from bot.models import Direction, Signal, SignalStatus
from bot.tracker import run_tracker_once


class PersistedTrackerStateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = Database(":memory:")
        await self.db.init()
        self.notifier = AsyncMock()

    async def asyncTearDown(self):
        await self.db.close()

    def signal(self, status=SignalStatus.TP1_HIT):
        signal_time = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=2)
        signal = Signal(
            id="persisted-tracker-state", symbol="BTCUSDT", timeframe="1h",
            pattern="Test Pattern", direction=Direction.LONG,
            entry_price=100, stop_loss=90, tp1=110, tp2=120, tp3=130,
            price_at_signal=105, signal_time=signal_time.isoformat(), status=status,
        )
        return signal, signal_time

    async def test_tracker_uses_persisted_status_and_ignores_signal_candle(self):
        signal, confirmed_at = self.signal()
        self.assertTrue(await self.db.insert_signal(signal))
        following_close = confirmed_at + timedelta(hours=1)
        # The persisted signal candle touches TP3 and SL. Those extremes occurred
        # before the signal's close and must not alter its already-persisted state.
        candles = pd.DataFrame([
            {"close_time": confirmed_at, "high": 135.0, "low": 80.0, "close": 105.0},
            {"close_time": following_close, "high": 121.0, "low": 99.0, "close": 115.0},
        ])
        market = AsyncMock()
        market.get_klines.return_value = candles

        await run_tracker_once(market, self.db, self.notifier)

        stored = await self.db.get_signal(signal.id)
        self.assertEqual(stored.status, SignalStatus.TP2_HIT)
        self.assertEqual(stored.tp2_hit_time, following_close.isoformat())
        self.assertIsNone(stored.tp3_hit_time)
        self.assertIsNone(stored.sl_hit_time)
        self.assertEqual(await self.db.get_metadata("last_tracker_failures"), "0")
        self.assertIsNotNone(await self.db.get_metadata("last_tracker_heartbeat_at"))
        self.assertIsNotNone(await self.db.get_metadata("last_tracker_success_at"))

    async def test_tracker_failure_updates_heartbeat_but_not_success(self):
        signal, _ = self.signal(SignalStatus.ACTIVE)
        self.assertTrue(await self.db.insert_signal(signal))
        market = AsyncMock()
        market.get_klines.side_effect = TimeoutError("offline")

        await run_tracker_once(market, self.db, self.notifier)

        self.assertEqual(await self.db.get_metadata("last_tracker_failures"), "1")
        self.assertIsNotNone(await self.db.get_metadata("last_tracker_heartbeat_at"))
        self.assertIsNone(await self.db.get_metadata("last_tracker_success_at"))
        stored = await self.db.get_signal(signal.id)
        self.assertEqual(stored.status, SignalStatus.ACTIVE)


if __name__ == "__main__":
    unittest.main()
