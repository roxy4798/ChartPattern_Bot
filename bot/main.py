from __future__ import annotations
import asyncio
import logging
import signal as os_signal
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from bot.config import settings
from bot.database import Database
from bot.binance_client import BinanceMarket
from bot.signal_engine import evaluate_symbol_timeframe
from bot.chart_generator import render_signal_chart
from bot.telegram_bot import build_application, Notifier
from bot.tracker import run_tracker_once
from bot.ui import ProfessionalConsoleFormatter, print_banner, format_signal_card
from bot.square import SquarePublisher

# Silence noisy libraries in the terminal
for noisy in ("httpx", "httpcore", "telegram", "telegram.ext", "aiosqlite", "urllib3"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

Path(settings.log_path).parent.mkdir(parents=True, exist_ok=True)

# Root Logger Setup
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
root_logger.handlers.clear()

# 1. Console Handler (Clean, styled, professional)
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(ProfessionalConsoleFormatter())
root_logger.addHandler(console_handler)

# 2. File Handler (Full detailed logs)
file_handler = logging.FileHandler(settings.log_path, encoding="utf-8")
file_handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"))
root_logger.addHandler(file_handler)

log = logging.getLogger("main")
_shutdown = asyncio.Event()


async def scan_loop(market: BinanceMarket, db: Database, notifier: Notifier, square_publisher: Optional[SquarePublisher] = None):

    log.info("Scanner engine online (Interval: %ds)", settings.scan_interval_sec)
    sem = asyncio.Semaphore(8)

    while not _shutdown.is_set():
        scan_id = str(uuid.uuid4())[:8]
        scan_start = datetime.now(timezone.utc)
        signals_found = 0

        try:
            symbols = await market.get_symbol_universe()
            log.info("Scanning %d symbols across %d timeframes...", len(symbols), len(settings.timeframes))
            active = await db.get_active_signals()
            busy = {(s.symbol, s.timeframe) for s in active}  # avoid overlapping active signals
            busy_lock = asyncio.Lock()

            async def _check_symbol(symbol: str):
                nonlocal signals_found
                if _shutdown.is_set():
                    return
                for tf in settings.timeframes:
                    if _shutdown.is_set():
                        return
                    async with busy_lock:
                        if (symbol, tf) in busy:
                            continue
                    try:
                        async with sem:
                            df = await market.get_klines(symbol, tf, settings.candles_lookback)
                        if df.empty or len(df) < 220:
                            continue
                        sig, pattern, reason = evaluate_symbol_timeframe(symbol, tf, df, scan_id=scan_id)
                        if sig is None or pattern is None or reason is None:
                            continue

                        async with busy_lock:
                            if (symbol, tf) in busy:
                                continue
                            busy.add((symbol, tf))

                        chart_png = render_signal_chart(symbol, tf, df, pattern, sig)
                        
                        # Prepare outbox fallback notification atomically with signal insertion
                        fallback_outbox_item = {
                            "event_key": f"{sig.id}:SIGNAL:{sig.signal_time}",
                            "kind": "signal_text",
                            "payload": {"reason": reason},
                        }
                        inserted = await db.insert_signal(sig, notification=fallback_outbox_item)
                        if not inserted:
                            continue

                        signals_found += 1

                        # Print formatted signal card in CMD
                        print(format_signal_card(
                            symbol=sig.symbol,
                            tf=sig.timeframe,
                            direction=sig.direction.value,
                            pattern=sig.pattern,
                            entry=sig.entry_price,
                            sl=sig.stop_loss,
                            tp1=sig.tp1,
                            tp2=sig.tp2,
                            tp3=sig.tp3,
                        ))

                        # Send visual chart with caption to Telegram
                        try:
                            text_id, chart_id = await notifier.send_signal(sig, reason, chart_png)
                            await db.set_telegram_ids(sig.id, message_id=text_id, chart_message_id=chart_id)
                            await db.mark_event_delivered(f"{sig.id}:SIGNAL:{sig.signal_time}")
                        except Exception as exc:
                            log.debug("[%s] Direct Telegram chart send queued in outbox (%s)", scan_id, exc)

                        # Binance Square Add-on (Optional, Non-blocking, Fully Fail-safe)
                        if settings.square_enabled and square_publisher:
                            asyncio.create_task(
                                square_publisher.publish_signal(sig, pattern, reason, chart_png)
                            )

                    except Exception:
                        log.debug("[%s] Scan check skipped for %s %s", scan_id, symbol, tf)

            tasks = [_check_symbol(sym) for sym in symbols]
            await asyncio.gather(*tasks, return_exceptions=True)

            await db.set_metadata("last_scan_at", scan_start.isoformat())
            await db.set_metadata("scan_symbols_count", str(len(symbols)))
            await db.set_metadata("last_scan_signals_count", str(signals_found))
            log.info("Scan completed: %d symbols checked | %d new signal(s) found", len(symbols), signals_found)
        except Exception:
            log.exception("Scan loop iteration error")

        try:
            await asyncio.wait_for(_shutdown.wait(), timeout=settings.scan_interval_sec)
        except asyncio.TimeoutError:
            pass



async def tracker_loop(market: BinanceMarket, db: Database, notifier: Notifier):
    log.info("Tracker engine online (Interval: %ds)", settings.tracker_interval_sec)
    while not _shutdown.is_set():
        try:
            await run_tracker_once(market, db, notifier)
        except Exception:
            log.exception("Tracker loop iteration error")
        try:
            await asyncio.wait_for(_shutdown.wait(), timeout=settings.tracker_interval_sec)
        except asyncio.TimeoutError:
            pass


async def run():
    Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)
    db = Database(settings.db_path)
    await db.init()

    market = BinanceMarket()
    await market.connect()

    app = build_application(db)
    notifier = Notifier(app, settings.telegram_chat_id)
    square_publisher = SquarePublisher(db=db, notifier=notifier)

    await app.initialize()
    try:
        await app.bot.set_my_commands([
            ("start", "Open the NeoElla Trade welcome guide"),
            ("help", "Show all available commands"),
            ("status", "Show bot operational status and heartbeats"),
            ("health", "Show diagnostic health and outbox status"),
            ("active", "Show open signals and targets"),
            ("history", "Show recent signal history"),
            ("stats", "Show performance summary"),
            ("performance", "Show detailed performance report"),
        ])
    except Exception:
        pass

    await app.start()
    await app.updater.start_polling()
    await notifier.flush_notifications(db)

    active = await db.get_active_signals()
    symbols = await market.get_symbol_universe()

    # Display Modern Startup Banner
    print_banner(
        db_path=settings.db_path,
        symbols_count=len(symbols),
        timeframes=settings.timeframes,
        is_testnet=settings.use_testnet,
        active_count=len(active),
    )

    loop = asyncio.get_running_loop()
    for sig_name in ("SIGINT", "SIGTERM"):
        if hasattr(os_signal, sig_name):
            try:
                loop.add_signal_handler(getattr(os_signal, sig_name), _shutdown.set)
            except (NotImplementedError, RuntimeError):
                pass

    try:
        await asyncio.gather(
            scan_loop(market, db, notifier, square_publisher),
            tracker_loop(market, db, notifier),
        )

    except (asyncio.CancelledError, KeyboardInterrupt):
        log.info("Shutdown signal received...")
    finally:
        log.info("Shutting down bot services gracefully...")
        _shutdown.set()
        try:
            if app.updater and app.updater.running:
                await app.updater.stop()
            if app.running:
                await app.stop()
            await app.shutdown()
        except (Exception, asyncio.CancelledError):
            pass
        try:
            await market.close()
        except (Exception, asyncio.CancelledError):
            pass
        try:
            await db.close()
        except (Exception, asyncio.CancelledError):
            pass

        log.info("Shutdown complete.")


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        log.info("Terminated by KeyboardInterrupt.")




