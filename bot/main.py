from __future__ import annotations
import asyncio
import logging
import signal as os_signal
import sys
from pathlib import Path

from bot.config import settings
from bot.database import Database
from bot.binance_client import BinanceMarket
from bot.signal_engine import evaluate_symbol_timeframe
from bot.chart_generator import render_signal_chart
from bot.telegram_bot import build_application, Notifier
from bot.tracker import run_tracker_once

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(settings.log_path)],
)
log = logging.getLogger("main")

_shutdown = asyncio.Event()


async def scan_loop(market: BinanceMarket, db: Database, notifier: Notifier):
    while not _shutdown.is_set():
        try:
            symbols = await market.get_symbol_universe()
            log.info("Scanning %d symbols x %d timeframes", len(symbols), len(settings.timeframes))
            active = await db.get_active_signals()
            busy = {(s.symbol, s.timeframe) for s in active}  # avoid overlapping signals

            for symbol in symbols:
                if _shutdown.is_set():
                    break
                for tf in settings.timeframes:
                    if (symbol, tf) in busy:
                        continue
                    try:
                        df = await market.get_klines(symbol, tf, settings.candles_lookback)
                        if df.empty or len(df) < 220:
                            continue
                        sig, pattern, reason = evaluate_symbol_timeframe(symbol, tf, df)
                        if sig is None or pattern is None:
                            continue
                        chart_png = render_signal_chart(symbol, tf, df, pattern, sig)
                        inserted = await db.insert_signal(sig)
                        if not inserted:
                            continue
                        text_id, chart_id = await notifier.send_signal(sig, reason, chart_png)
                        await db.set_telegram_ids(sig.id, message_id=text_id, chart_message_id=chart_id)
                        log.info("Signal: %s %s %s %s @ %.6g", symbol, tf, sig.direction.value, sig.pattern, sig.entry_price)
                    except Exception:
                        log.exception("Scan failed for %s %s", symbol, tf)
                    await asyncio.sleep(0.15)  # gentle on rate limits
        except Exception:
            log.exception("Scan loop iteration failed")
        await asyncio.wait([_shutdown.wait()], timeout=settings.scan_interval_sec)


async def tracker_loop(market: BinanceMarket, db: Database, notifier: Notifier):
    while not _shutdown.is_set():
        try:
            await run_tracker_once(market, db, notifier)
        except Exception:
            log.exception("Tracker loop iteration failed")
        await asyncio.wait([_shutdown.wait()], timeout=settings.tracker_interval_sec)


async def run():
    Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)
    db = Database(settings.db_path)
    await db.init()

    market = BinanceMarket()
    await market.connect()

    app = build_application(db)
    notifier = Notifier(app, settings.telegram_chat_id)

    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    await notifier.flush_notifications(db)
    log.info("Telegram bot started. Resuming any ACTIVE signals from previous run...")

    active = await db.get_active_signals()
    log.info("Resumed tracking %d active signal(s) from database.", len(active))

    loop = asyncio.get_event_loop()
    for sig_name in ("SIGINT", "SIGTERM"):
        if hasattr(os_signal, sig_name):
            loop.add_signal_handler(getattr(os_signal, sig_name), _shutdown.set)

    try:
        await asyncio.gather(
            scan_loop(market, db, notifier),
            tracker_loop(market, db, notifier),
        )
    finally:
        log.info("Shutting down gracefully...")
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
        await market.close()


if __name__ == "__main__":
    asyncio.run(run())
