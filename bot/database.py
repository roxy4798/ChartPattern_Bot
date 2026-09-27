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
import logging
from datetime import datetime, timezone
from typing import Optional
from bot.models import Signal, SignalStatus, Direction

log = logging.getLogger("database")

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 5000;
PRAGMA synchronous = NORMAL;

CREATE TABLE IF NOT EXISTS signals (
    id                        TEXT PRIMARY KEY,
    symbol                    TEXT NOT NULL,
    timeframe                 TEXT NOT NULL,
    pattern                   TEXT NOT NULL,
    direction                 TEXT NOT NULL,
    entry_price               REAL NOT NULL,
    stop_loss                 REAL NOT NULL,
    tp1                       REAL NOT NULL,
    tp2                       REAL NOT NULL,
    tp3                       REAL NOT NULL,
    ema200_at_signal          REAL,
    price_at_signal           REAL,
    signal_time               TEXT NOT NULL,
    status                    TEXT NOT NULL,
    tp1_hit_time              TEXT,
    tp2_hit_time              TEXT,
    tp3_hit_time              TEXT,
    sl_hit_time               TEXT,
    exit_price                REAL,
    exit_time                 TEXT,
    result                    TEXT,
    pnl_pct                   REAL,
    duration_sec              INTEGER,
    telegram_message_id       INTEGER,
    telegram_chart_message_id INTEGER,
    created_at                TEXT NOT NULL
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
    ON notification_outbox(delivered_at, attempts, id);

CREATE TABLE IF NOT EXISTS system_metadata (
    key          TEXT PRIMARY KEY,
    value        TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS square_posts (
    signal_id       TEXT PRIMARY KEY,
    signal_hash     TEXT NOT NULL UNIQUE,
    status          TEXT NOT NULL,
    post_id         TEXT,
    error_code      TEXT,
    error_message   TEXT,
    attempt_count   INTEGER NOT NULL DEFAULT 0,
    last_attempt_at TEXT,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_square_posts_status ON square_posts(status);
CREATE INDEX IF NOT EXISTS idx_square_posts_created ON square_posts(created_at);
"""

_lock = asyncio.Lock()  # serialize writes across coroutines


class Database:
    def __init__(self, path: str):
        self.path = path
        self._db: aiosqlite.Connection | None = None

    async def init(self):
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA)
        await self._db.commit()
        log.info("Initialized SQLite database with WAL mode at %s", self.path)

    async def close(self):
        if self._db:
            await self._db.close()
            self._db = None

    def _ensure_connected(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Database connection is not initialized. Call db.init() first.")
        return self._db

    async def set_metadata(self, key: str, value: str):
        db = self._ensure_connected()
        now = datetime.now(timezone.utc).isoformat()
        async with _lock:
            await db.execute(
                """INSERT INTO system_metadata (key, value, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
                (key, value, now),
            )
            await db.commit()

    async def get_metadata(self, key: str) -> Optional[str]:
        db = self._ensure_connected()
        async with db.execute("SELECT value FROM system_metadata WHERE key=?", (key,)) as cur:
            row = await cur.fetchone()
            return row["value"] if row else None

    async def get_all_metadata(self) -> dict[str, dict[str, str]]:
        db = self._ensure_connected()
        async with db.execute("SELECT key, value, updated_at FROM system_metadata") as cur:
            rows = await cur.fetchall()
            return {r["key"]: {"value": r["value"], "updated_at": r["updated_at"]} for r in rows}

    async def insert_signal(self, s: Signal, notification: Optional[dict] = None) -> bool:
        """Returns False if this exact signal (symbol/tf/pattern/time) already exists.
        Atomically enqueues notification in outbox if provided."""
        db = self._ensure_connected()
        now = datetime.now(timezone.utc).isoformat()
        async with _lock:
            try:
                await db.execute("BEGIN IMMEDIATE")
                await db.execute(
                    """INSERT INTO signals (
                        id, symbol, timeframe, pattern, direction, entry_price,
                        stop_loss, tp1, tp2, tp3, ema200_at_signal, price_at_signal,
                        signal_time, status, created_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (s.id, s.symbol, s.timeframe, s.pattern, s.direction.value,
                     s.entry_price, s.stop_loss, s.tp1, s.tp2, s.tp3,
                     s.ema200_at_signal, s.price_at_signal, s.signal_time,
                     s.status.value, now),
                )
                if notification:
                    event_key = notification["event_key"]
                    await db.execute(
                        """INSERT OR IGNORE INTO notification_outbox
                           (signal_id, event_key, kind, payload, created_at)
                           VALUES (?, ?, ?, ?, ?)""",
                        (s.id, event_key, notification["kind"],
                         json.dumps(notification["payload"]), now),
                    )
                await db.commit()
                return True
            except aiosqlite.IntegrityError:
                await db.rollback()
                return False
            except Exception:
                await db.rollback()
                raise

    async def set_telegram_ids(self, signal_id: str, message_id: Optional[int] = None,
                                chart_message_id: Optional[int] = None):
        db = self._ensure_connected()
        async with _lock:
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
                             duration_sec: Optional[int] = None) -> bool:
        field_map = {
            SignalStatus.TP1_HIT: "tp1_hit_time",
            SignalStatus.TP2_HIT: "tp2_hit_time",
            SignalStatus.TP3_HIT: "tp3_hit_time",
            SignalStatus.SL_HIT: "sl_hit_time",
        }
        db = self._ensure_connected()
        now = datetime.now(timezone.utc).isoformat()
        async with _lock:
            try:
                await db.execute("BEGIN IMMEDIATE")
                sets = ["status=?"]
                vals: list[object] = [status.value]
                if status in field_map and hit_time:
                    sets.append(f"{field_map[status]}=?")
                    vals.append(hit_time)
                if exit_price is not None:
                    sets.append("exit_price=?")
                    vals.append(exit_price)
                if exit_time is not None:
                    sets.append("exit_time=?")
                    vals.append(exit_time)
                if result is not None:
                    sets.append("result=?")
                    vals.append(result)
                if pnl_pct is not None:
                    sets.append("pnl_pct=?")
                    vals.append(pnl_pct)
                if duration_sec is not None:
                    sets.append("duration_sec=?")
                    vals.append(duration_sec)
                
                where = "WHERE id=?"
                vals.append(signal_id)
                if expected_status is not None:
                    where += " AND status=?"
                    vals.append(expected_status.value)
                
                cur = await db.execute(f"UPDATE signals SET {', '.join(sets)} {where}", vals)
                updated = (cur.rowcount == 1)
                
                if updated and notifications:
                    for notification in notifications:
                        event_key = notification["event_key"]
                        await db.execute(
                            """INSERT OR IGNORE INTO notification_outbox
                               (signal_id, event_key, kind, payload, created_at)
                               VALUES (?, ?, ?, ?, ?)""",
                            (signal_id, event_key, notification["kind"],
                             json.dumps(notification["payload"]), now),
                        )
                await db.commit()
                return updated
            except Exception:
                await db.rollback()
                raise

    async def get_pending_notifications(self, limit: int = 50, max_attempts: int = 10) -> list[dict]:
        db = self._ensure_connected()
        async with db.execute(
            """SELECT id, signal_id, kind, payload, attempts
               FROM notification_outbox
               WHERE delivered_at IS NULL AND attempts < ?
               ORDER BY id LIMIT ?""", (max_attempts, limit)
        ) as cur:
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
        db = self._ensure_connected()
        async with _lock:
            cur = await db.execute(
                """UPDATE notification_outbox
                   SET delivered_at=datetime('now')
                   WHERE id=? AND delivered_at IS NULL""",
                (notification_id,),
            )
            await db.commit()
            return cur.rowcount == 1

    async def mark_event_delivered(self, event_key: str) -> bool:
        db = self._ensure_connected()
        async with _lock:
            cur = await db.execute(
                """UPDATE notification_outbox
                   SET delivered_at=datetime('now')
                   WHERE event_key=? AND delivered_at IS NULL""",
                (event_key,),
            )
            await db.commit()
            return cur.rowcount == 1


    async def mark_notification_failed(self, notification_id: int, error: str):
        db = self._ensure_connected()
        async with _lock:
            await db.execute(
                """UPDATE notification_outbox
                   SET attempts=attempts+1, last_error=?
                   WHERE id=? AND delivered_at IS NULL""",
                (error[:500], notification_id),
            )
            await db.commit()

    async def get_active_signals(self) -> list[Signal]:
        db = self._ensure_connected()
        async with db.execute(
            "SELECT * FROM signals WHERE status NOT IN (?, ?)",
            (SignalStatus.CLOSED.value, SignalStatus.SL_HIT.value),
        ) as cur:
            rows = await cur.fetchall()
            return [self._row_to_signal(r) for r in rows]

    async def get_signal(self, signal_id: str) -> Optional[Signal]:
        db = self._ensure_connected()
        async with db.execute("SELECT * FROM signals WHERE id=?", (signal_id,)) as cur:
            row = await cur.fetchone()
            return self._row_to_signal(row) if row else None

    async def get_recent_signals(self, limit: int = 20) -> list[Signal]:
        db = self._ensure_connected()
        async with db.execute(
            "SELECT * FROM signals ORDER BY signal_time DESC LIMIT ?", (limit,)
        ) as cur:
            rows = await cur.fetchall()
            return [self._row_to_signal(r) for r in rows]

    async def get_all_closed(self) -> list[Signal]:
        db = self._ensure_connected()
        async with db.execute(
            "SELECT * FROM signals WHERE result IS NOT NULL ORDER BY signal_time"
        ) as cur:
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

    # --- Binance Square helpers ---

    async def get_square_post(self, signal_id: str) -> Optional[dict]:
        db = self._ensure_connected()
        async with db.execute("SELECT * FROM square_posts WHERE signal_id = ?", (signal_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def record_square_post(
        self,
        signal_id: str,
        signal_hash: str,
        status: str,
        post_id: Optional[str] = None,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
    ):
        db = self._ensure_connected()
        now = datetime.now(timezone.utc).isoformat()
        async with _lock:
            await db.execute(
                """INSERT INTO square_posts (
                       signal_id, signal_hash, status, post_id,
                       error_code, error_message, attempt_count, last_attempt_at, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                   ON CONFLICT(signal_id) DO UPDATE SET
                       status = excluded.status,
                       post_id = COALESCE(excluded.post_id, square_posts.post_id),
                       error_code = excluded.error_code,
                       error_message = excluded.error_message,
                       attempt_count = square_posts.attempt_count + 1,
                       last_attempt_at = excluded.last_attempt_at""",
                (signal_id, signal_hash, status, post_id, error_code, error_message, now, now),
            )
            await db.commit()

    async def get_square_posts_today_count(self) -> int:
        db = self._ensure_connected()
        today_prefix = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        async with db.execute(
            """SELECT COUNT(*) FROM square_posts
               WHERE created_at LIKE ? AND status IN ('SUCCESS', 'SKIPPED_DRY_RUN')""",
            (f"{today_prefix}%",),
        ) as cur:
            row = await cur.fetchone()
            return int(row[0]) if row else 0

    async def get_square_successful_real_posts_count(self) -> int:
        db = self._ensure_connected()
        async with db.execute(
            "SELECT COUNT(*) FROM square_posts WHERE status = 'SUCCESS'"
        ) as cur:
            row = await cur.fetchone()
            return int(row[0]) if row else 0



