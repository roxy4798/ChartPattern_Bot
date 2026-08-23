"""
Async SQLite persistence. One row per signal, mutated in place as it moves
through ACTIVE -> TP1_HIT -> ... -> CLOSED. This is the single source of
truth for tracking state and statistics, so the bot can restart and resume
without losing active signals.
"""
from __future__ import annotations
import aiosqlite
import asyncio
import json
from contextlib import asynccontextmanager
from typing import Optional
from bot.models import Signal, SignalStatus, Direction

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id                      TEXT PRIMARY KEY,
    symbol                  TEXT NOT NULL,
    timeframe               TEXT NOT NULL,
    pattern                 TEXT NOT NULL,
    direction               TEXT NOT NULL,
    entry_price             REAL NOT NULL,
    stop_loss               REAL NOT NULL,
    tp1                     REAL NOT NULL,
    tp2                     REAL NOT NULL,
    tp3                     REAL NOT NULL,
    ema200_at_signal        REAL,
    price_at_signal         REAL,
    signal_time             TEXT NOT NULL,
    status                  TEXT NOT NULL,
    tp1_hit_time            TEXT,
    tp2_hit_time            TEXT,
    tp3_hit_time            TEXT,
    sl_hit_time             TEXT,
    exit_price              REAL,
    exit_time               TEXT,
    result                  TEXT,
    pnl_pct                 REAL,
    duration_sec            INTEGER,
    telegram_message_id     INTEGER,
    telegram_chart_message_id INTEGER,
    created_at              TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_signals_status ON signals(status);
CREATE INDEX IF NOT EXISTS idx_signals_symbol_tf ON signals(symbol, timeframe);
CREATE UNIQUE INDEX IF NOT EXISTS idx_signals_dedup
    ON signals(symbol, timeframe, pattern, signal_time);
CREATE TABLE IF NOT EXISTS notification_outbox (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id    TEXT NOT NULL,
    event_key    TEXT NOT NULL UNIQUE,
    kind         TEXT NOT NULL,
    payload      TEXT NOT NULL,
    attempts     INTEGER NOT NULL DEFAULT 0,
    last_error   TEXT,
    delivered_at TEXT,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_outbox_pending
    ON notification_outbox(delivered_at, id);
"""

_lock = asyncio.Lock()  # serialize writes; sqlite handles one writer well


class Database:
    def __init__(self, path: str):
        self.path = path

    async def init(self):
        async with aiosqlite.connect(self.path) as db:
            await db.executescript(SCHEMA)
            await db.commit()

    @asynccontextmanager
    async def _conn(self):
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            yield db

    async def insert_signal(self, s: Signal) -> bool:
        """Returns False if this exact signal (symbol/tf/pattern/time) already exists."""
        async with _lock, self._conn() as db:
            try:
                await db.execute(
                    """INSERT INTO signals (
                        id, symbol, timeframe, pattern, direction, entry_price,
                        stop_loss, tp1, tp2, tp3, ema200_at_signal, price_at_signal,
                        signal_time, status, created_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (s.id, s.symbol, s.timeframe, s.pattern, s.direction.value,
                     s.entry_price, s.stop_loss, s.tp1, s.tp2, s.tp3,
                     s.ema200_at_signal, s.price_at_signal, s.signal_time,
                     s.status.value, s.signal_time),
                )
                await db.commit()
                return True
            except aiosqlite.IntegrityError:
                return False

    async def set_telegram_ids(self, signal_id: str, message_id: Optional[int] = None,
                                chart_message_id: Optional[int] = None):
        async with _lock, self._conn() as db:
            if message_id is not None:
                await db.execute("UPDATE signals SET telegram_message_id=? WHERE id=?",
                                  (message_id, signal_id))
            if chart_message_id is not None:
                await db.execute("UPDATE signals SET telegram_chart_message_id=? WHERE id=?",
                                  (chart_message_id, signal_id))
            await db.commit()

    async def update_status(self, signal_id: str, status: SignalStatus, *,
                             expected_status: Optional[SignalStatus] = None,
                             notifications: Optional[list[dict]] = None,
                             hit_time: Optional[str] = None,
                             exit_price: Optional[float] = None,
                             exit_time: Optional[str] = None,
                             result: Optional[str] = None,
                             pnl_pct: Optional[float] = None,
                             duration_sec: Optional[int] = None):
        field_map = {
            SignalStatus.TP1_HIT: "tp1_hit_time",
            SignalStatus.TP2_HIT: "tp2_hit_time",
            SignalStatus.TP3_HIT: "tp3_hit_time",
            SignalStatus.SL_HIT: "sl_hit_time",
        }
        async with _lock, self._conn() as db:
            sets = ["status=?"]
            vals = [status.value]
            if status in field_map and hit_time:
                sets.append(f"{field_map[status]}=?")
                vals.append(hit_time)
            if exit_price is not None:
                sets.append("exit_price=?"); vals.append(exit_price)
            if exit_time is not None:
                sets.append("exit_time=?"); vals.append(exit_time)
            if result is not None:
                sets.append("result=?"); vals.append(result)
            if pnl_pct is not None:
                sets.append("pnl_pct=?"); vals.append(pnl_pct)
            if duration_sec is not None:
                sets.append("duration_sec=?"); vals.append(duration_sec)
            where = "WHERE id=?"
            vals.append(signal_id)
            if expected_status is not None:
                where += " AND status=?"
                vals.append(expected_status.value)
            cur = await db.execute(f"UPDATE signals SET {', '.join(sets)} {where}", vals)
            if cur.rowcount == 1 and notifications:
                for notification in notifications:
                    event_key = notification["event_key"]
                    await db.execute(
                        """INSERT OR IGNORE INTO notification_outbox
                           (signal_id, event_key, kind, payload, created_at)
                           VALUES (?, ?, ?, ?, datetime('now'))""",
                        (signal_id, event_key, notification["kind"],
                         json.dumps(notification["payload"])),
                    )
            await db.commit()
            return cur.rowcount == 1

    async def get_pending_notifications(self, limit: int = 50) -> list[dict]:
        async with self._conn() as db:
            cur = await db.execute(
                """SELECT id, signal_id, kind, payload, attempts
                   FROM notification_outbox
                   WHERE delivered_at IS NULL
                   ORDER BY id LIMIT ?""", (limit,)
            )
            rows = await cur.fetchall()
            return [
                {
                    "id": row["id"],
                    "signal_id": row["signal_id"],
                    "kind": row["kind"],
                    "payload": json.loads(row["payload"]),
                    "attempts": row["attempts"],
                }
                for row in rows
            ]

    async def mark_notification_delivered(self, notification_id: int) -> bool:
        async with _lock, self._conn() as db:
            cur = await db.execute(
                """UPDATE notification_outbox
                   SET delivered_at=datetime('now')
                   WHERE id=? AND delivered_at IS NULL""",
                (notification_id,),
            )
            await db.commit()
            return cur.rowcount == 1

    async def mark_notification_failed(self, notification_id: int, error: str):
        async with _lock, self._conn() as db:
            await db.execute(
                """UPDATE notification_outbox
                   SET attempts=attempts+1, last_error=?
                   WHERE id=? AND delivered_at IS NULL""",
                (error[:500], notification_id),
            )
            await db.commit()

    async def get_active_signals(self) -> list[Signal]:
        async with self._conn() as db:
            cur = await db.execute(
                "SELECT * FROM signals WHERE status NOT IN (?, ?)",
                (SignalStatus.CLOSED.value, SignalStatus.SL_HIT.value),
            )
            rows = await cur.fetchall()
            return [self._row_to_signal(r) for r in rows]

    async def get_signal(self, signal_id: str) -> Optional[Signal]:
        async with self._conn() as db:
            cur = await db.execute("SELECT * FROM signals WHERE id=?", (signal_id,))
            row = await cur.fetchone()
            return self._row_to_signal(row) if row else None

    async def get_recent_signals(self, limit: int = 20) -> list[Signal]:
        async with self._conn() as db:
            cur = await db.execute(
                "SELECT * FROM signals ORDER BY signal_time DESC LIMIT ?", (limit,)
            )
            rows = await cur.fetchall()
            return [self._row_to_signal(r) for r in rows]

    async def get_all_closed(self) -> list[Signal]:
        async with self._conn() as db:
            cur = await db.execute(
                "SELECT * FROM signals WHERE result IS NOT NULL ORDER BY signal_time"
            )
            rows = await cur.fetchall()
            return [self._row_to_signal(r) for r in rows]

    @staticmethod
    def _row_to_signal(r: aiosqlite.Row) -> Signal:
        return Signal(
            id=r["id"], symbol=r["symbol"], timeframe=r["timeframe"],
            pattern=r["pattern"], direction=Direction(r["direction"]),
            entry_price=r["entry_price"], stop_loss=r["stop_loss"],
            tp1=r["tp1"], tp2=r["tp2"], tp3=r["tp3"],
            ema200_at_signal=r["ema200_at_signal"], price_at_signal=r["price_at_signal"],
            signal_time=r["signal_time"], status=SignalStatus(r["status"]),
            tp1_hit_time=r["tp1_hit_time"], tp2_hit_time=r["tp2_hit_time"],
            tp3_hit_time=r["tp3_hit_time"], sl_hit_time=r["sl_hit_time"],
            exit_price=r["exit_price"], exit_time=r["exit_time"], result=r["result"],
            pnl_pct=r["pnl_pct"], duration_sec=r["duration_sec"],
            telegram_message_id=r["telegram_message_id"],
            telegram_chart_message_id=r["telegram_chart_message_id"],
            created_at=r["created_at"],
        )
