from __future__ import annotations
import asyncio
import io
import logging
from html import escape
from datetime import datetime, timezone
from telegram import Update, InputFile
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from bot.config import settings
from bot.models import Signal, SignalStatus
from bot.database import Database
from bot.stats import build_report, Bucket

log = logging.getLogger("telegram")

DIR_EMOJI = {"LONG": "🟢", "SHORT": "🔴"}


def _fmt_price(p: float) -> str:
    return f"{p:.6g}"


def _safe(value: object) -> str:
    return escape(str(value))


def _fmt_signal_time(value: str | None) -> str:
    if not value:
        return "—"
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc).strftime("%d %b %Y, %H:%M UTC")
    except (TypeError, ValueError):
        return _safe(value)


def _age_label(value: str | None) -> str:
    if not value:
        return "—"
    try:
        seconds = max(0, int((datetime.now(timezone.utc) -
                              datetime.fromisoformat(value.replace("Z", "+00:00"))).total_seconds()))
        days, rem = divmod(seconds, 86400)
        hours, rem = divmod(rem, 3600)
        minutes = rem // 60
        if days:
            return f"{days}d {hours}h"
        if hours:
            return f"{hours}h {minutes}m"
        return f"{minutes}m"
    except (TypeError, ValueError):
        return "—"


def _direction_icon(direction: str) -> str:
    return DIR_EMOJI.get(direction, "⚪")


def build_signal_message(s: Signal, reason: str, dist_to_ema_pct: Optional[float] = None) -> str:
    side = _direction_icon(s.direction.value)
    rr = abs(s.tp2 - s.entry_price) / abs(s.entry_price - s.stop_loss) if s.entry_price != s.stop_loss else 0
    return (
        f"{side} <b>{s.direction.value} SIGNAL</b>\n"
        f"<b>NEOELLA TRADE</b>  ·  {s.symbol}\n"
        f"<b>TIMEFRAME:</b> {s.timeframe.upper()}\n"
        f"<b>Pattern:</b> {_safe(s.pattern)}\n\n"
        f"<b>PATTERN ANALYSIS</b>\n"
        f"• Name: {_safe(s.pattern)}\n"
        f"• Status: Confirmed (closed-candle breakout)\n"
        f"• Breakout level: {_fmt_price(s.entry_price)}\n"
        f"• Confirmation: Closed candle\n\n"
        f"<b>ENTRY</b>\n"
        f"• Entry: {_fmt_price(s.entry_price)}\n\n"
        f"<b>RISK MANAGEMENT</b>\n"
        f"• Stop Loss: {_fmt_price(s.stop_loss)}\n"
        f"• TP1: {_fmt_price(s.tp1)}\n"
        f"• TP2: {_fmt_price(s.tp2)}\n"
        f"• TP3: {_fmt_price(s.tp3)}\n"
        f"• R:R (to TP2): {rr:.2f}\n\n"
        f"<b>SIGNAL REASON</b>\n{reason}\n\n"
        f"<i>ID: {s.id[:8]} · Time: {_fmt_signal_time(s.signal_time)}</i>"
    )


def build_update_message(s: Signal, event: str, price: float) -> str:
    side = _direction_icon(s.direction.value)
    pnl = s.pnl_pct
    if event == "SL_HIT":
        header = f"{side} <b>NEOELLA TRADE · {s.direction.value} UPDATE</b>\n{s.symbol} • {s.timeframe.upper()}\nPattern: {_safe(s.pattern)}\n\n🔴 <b>STOP LOSS HIT</b>\nPrice: {_fmt_price(price)}\n"
        if pnl is not None:
            header += f"PnL: {pnl:+.2f}%\n"
        header += "\nStatus: <b>CLOSED</b>"
        return header
    header = (f"{side} <b>NEOELLA TRADE · {s.direction.value} UPDATE</b>\n{s.symbol} • {s.timeframe.upper()}\n"
              f"Pattern: {_safe(s.pattern)}\n\n✅ <b>{event.replace('_', ' ')}</b>\nPrice: {_fmt_price(price)}\n")
    if pnl is not None:
        header += f"PnL: {pnl:+.2f}%\n"
    remaining = {"TP1_HIT": "TP2, TP3", "TP2_HIT": "TP3", "TP3_HIT": "none"}.get(event, "")
    status = "CLOSED" if event == "TP3_HIT" else "ACTIVE"
    header += f"\nStatus: <b>{status}</b>"
    if remaining and status == "ACTIVE":
        header += f"\nRemaining Targets: {remaining}"
    return header


def build_closed_message(s: Signal, result: str) -> str:
    icon = "🏆" if result == "WIN" else "💥"
    reason = f"{s.status.value.replace('_', ' ')}" if s.status != SignalStatus.CLOSED else "Final target"
    duration = ""
    if s.duration_sec:
        h, rem = divmod(int(s.duration_sec), 3600)
        m = rem // 60
        duration = f"{h}h {m}m"
    return (
        f"{icon} <b>TRADE CLOSED — {reason}</b>\n"
        f"{s.symbol} • {s.timeframe.upper()}\n\n"
        f"Entry: {_fmt_price(s.entry_price)}\n"
        f"Exit: {_fmt_price(s.exit_price)}\n"
        f"Result: <b>{result}</b>\n"
        f"PnL: <b>{(s.pnl_pct or 0):+.2f}%</b>\n"
        f"Duration: {duration}\n\n"
        f"Pattern: {_safe(s.pattern)}"
    )


class Notifier:
    def __init__(self, app: Application, chat_id: str):
        self.app = app
        self.chat_id = str(chat_id).strip() if chat_id else ""

    async def send_signal(self, s: Signal, reason: str, chart_png: bytes) -> tuple[int, int]:
        if not self.chat_id:
            raise ValueError("TELEGRAM_CHAT_ID is not configured in .env")
        text = build_signal_message(s, reason)
        
        # Telegram photo caption supports up to 1024 characters
        if len(text) <= 1024:
            photo_msg = await self.app.bot.send_photo(
                chat_id=self.chat_id,
                photo=InputFile(io.BytesIO(chart_png), filename=f"{s.symbol}_{s.timeframe}.png"),
                caption=text,
                parse_mode=ParseMode.HTML,
            )
            return photo_msg.message_id, photo_msg.message_id
        else:
            # Fallback if text is unusually long
            photo_msg = await self.app.bot.send_photo(
                chat_id=self.chat_id,
                photo=InputFile(io.BytesIO(chart_png), filename=f"{s.symbol}_{s.timeframe}.png"),
            )
            text_msg = await self.app.bot.send_message(
                chat_id=self.chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_to_message_id=photo_msg.message_id,
            )
            return text_msg.message_id, photo_msg.message_id

    async def send_update(self, s: Signal, event: str, price: float):
        if not self.chat_id:
            return
        text = build_update_message(s, event, price)
        reply_id = s.telegram_message_id or s.telegram_chart_message_id
        try:
            await self.app.bot.send_message(
                chat_id=self.chat_id, text=text, parse_mode=ParseMode.HTML,
                reply_to_message_id=reply_id,
            )
        except Exception:
            # Fallback if reply_to_message_id no longer exists
            await self.app.bot.send_message(chat_id=self.chat_id, text=text, parse_mode=ParseMode.HTML)

    async def send_closed(self, s: Signal, result: str):
        if not self.chat_id:
            return
        text = build_closed_message(s, result)
        reply_id = s.telegram_message_id or s.telegram_chart_message_id
        try:
            await self.app.bot.send_message(
                chat_id=self.chat_id, text=text, parse_mode=ParseMode.HTML,
                reply_to_message_id=reply_id,
            )
        except Exception:
            await self.app.bot.send_message(chat_id=self.chat_id, text=text, parse_mode=ParseMode.HTML)

    async def flush_notifications(self, db: Database):
        """Deliver persisted state events in order; resilient with rate limiting and retry."""
        if not self.chat_id:
            return
        pending = await db.get_pending_notifications(limit=50, max_attempts=settings.notification_max_attempts)
        for item in pending:
            try:
                signal = await db.get_signal(item["signal_id"])
                if signal is None:
                    await db.mark_notification_delivered(item["id"])
                    continue

                payload = item["payload"]
                if item["kind"] == "update":
                    await self.send_update(signal, payload["event"], float(payload["price"]))
                elif item["kind"] == "closed":
                    await self.send_closed(signal, payload["result"])
                elif item["kind"] == "signal_text":
                    # Only send fallback text if telegram_message_id was not set by visual send
                    if not signal.telegram_message_id:
                        text = build_signal_message(signal, payload.get("reason", ""))
                        await self.app.bot.send_message(chat_id=self.chat_id, text=text, parse_mode=ParseMode.HTML)
                else:
                    log.warning("Unknown notification kind: %s", item["kind"])

                await db.mark_notification_delivered(item["id"])
                await asyncio.sleep(settings.rate_limit_delay_sec)
            except Exception as exc:
                await db.mark_notification_failed(item["id"], repr(exc))
                error_msg = str(exc)
                if "Chat not found" in error_msg:
                    log.warning(
                        "Telegram chat '%s' not found. Please verify TELEGRAM_CHAT_ID in .env and start the bot with /start in Telegram.",
                        self.chat_id,
                    )
                else:
                    log.exception("Notification delivery failed for outbox item %s (attempt %d)",
                                  item["id"], item.get("attempts", 0) + 1)
                await asyncio.sleep(0.5)




def _bucket_line(name: str, b: Bucket) -> str:
    return f"{name} → {b.winrate:.0f}% WR | {b.total_pnl:+.2f}% PnL ({b.signals} signals)"


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db: Database = context.application.bot_data["db"]
    report = await build_report(db)
    o = report.overall
    lines = [
        "📊 <b>NEOELLA TRADE · PERFORMANCE SUMMARY</b>\n",
        f"<b>Total signals:</b> {o.signals}",
        f"<b>Wins / losses:</b> {o.wins} / {o.losses}",
        f"<b>Win rate:</b> {o.winrate:.2f}%",
        f"<b>Total PnL:</b> {o.total_pnl:+.2f}%\n",
        "⏱ <b>BY TIMEFRAME</b>",
    ]
    for tf in ("1d", "4h", "1h", "15m"):
        b = report.by_timeframe.get(tf)
        if b and b.signals:
            lines.append(f"• {_bucket_line(tf.upper(), b)}")
    if report.by_pattern:
        best_pat = max(report.by_pattern.items(), key=lambda kv: kv[1].winrate if kv[1].signals else -1, default=(None, None))
        worst_pat = min(report.by_pattern.items(), key=lambda kv: kv[1].winrate if kv[1].signals else 999, default=(None, None))
        if best_pat[0]:
            lines.append(f"\n🟢 <b>Best pattern:</b> {_safe(best_pat[0])}")
        if worst_pat[0]:
            lines.append(f"🔴 <b>Weakest pattern:</b> {_safe(worst_pat[0])}")
    if report.best_trade:
        lines.append(f"📈 <b>Best trade:</b> {report.best_trade.pnl_pct:+.2f}%")
    if report.worst_trade:
        lines.append(f"📉 <b>Weakest trade:</b> {report.worst_trade.pnl_pct:+.2f}%")
    lines.append(f"\n🔁 <b>Max consecutive wins:</b> {report.max_consecutive_wins}")
    lines.append(f"🔁 <b>Max consecutive losses:</b> {report.max_consecutive_losses}")
    await update.message.reply_html("\n".join(lines))


async def cmd_performance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db: Database = context.application.bot_data["db"]
    report = await build_report(db)
    lines = ["📈 <b>NEOELLA TRADE · DETAILED REPORT</b>\n", "🧩 <b>BY PATTERN</b>"]
    for name, b in sorted(report.by_pattern.items(), key=lambda kv: -kv[1].signals):
        lines.append(
            f"• <b>{_safe(name)}</b>\n  {b.signals} signals  ·  {b.winrate:.0f}% WR  ·  avg {b.avg_pnl:+.2f}%  ·  total {b.total_pnl:+.2f}%"
        )
    lines.append("\n⏱ <b>BY TIMEFRAME</b>")
    for tf, b in report.by_timeframe.items():
        lines.append(f"• <b>{tf.upper()}</b>: {b.signals} signals  ·  {b.winrate:.0f}% WR  ·  total {b.total_pnl:+.2f}%")
    lines.append("\n🎯 <b>TARGET HIT RATES</b>")
    o = report.overall
    total = max(o.signals, 1)
    lines.append(f"TP1: {o.tp1}/{total}  ·  TP2: {o.tp2}/{total}  ·  TP3: {o.tp3}/{total}  ·  SL: {o.sl}/{total}")
    await update.message.reply_html("\n".join(lines))


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html(
        "📚 <b>NEOELLA TRADE · COMMAND GUIDE</b>\n\n"
        "<b>Signal monitoring</b>\n"
        "• /active — open signals with live targets and status\n"
        "• /history — latest signals and outcomes\n"
        "• /status — bot operational status and heartbeats\n"
        "• /health — diagnostic health and outbox status\n\n"
        "<b>Analytics</b>\n"
        "• /stats — concise performance summary\n"
        "• /performance — full pattern, timeframe, and target report\n\n"
        "<b>General</b>\n"
        "• /start — welcome message and quick guide\n"
        "• /help — show this command guide\n\n"
        "<i>All prices and status updates derive from verified closed-candle single-source-of-truth state.</i>"
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db: Database = context.application.bot_data["db"]
    active = await db.get_active_signals()
    recent = await db.get_recent_signals(limit=1)
    meta = await db.get_all_metadata()

    now = datetime.now(timezone.utc)
    
    last_scan_raw = meta.get("last_scan_at", {}).get("value")
    last_tracker_raw = meta.get("last_tracker_at", {}).get("value")
    scan_count = meta.get("scan_symbols_count", {}).get("value", "0")
    
    scan_age = _age_label(last_scan_raw)
    tracker_age = _age_label(last_tracker_raw)
    
    scan_status = "🟢 Active" if last_scan_raw else "⚪ Initializing"
    tracker_status = "🟢 Active" if last_tracker_raw else "⚪ Initializing"

    lines = [
        "🟢 <b>NEOELLA TRADE · SYSTEM STATUS</b>\n",
        f"<b>Scanner:</b> {scan_status} (Last: {scan_age} ago, {scan_count} symbols)",
        f"<b>Tracker:</b> {tracker_status} (Last: {tracker_age} ago)",
        "<b>Storage:</b> SQLite WAL mode (persistent)",
        f"<b>Open signals:</b> {len(active)}",
        f"<b>Last signal:</b> {_fmt_signal_time(recent[0].signal_time) if recent else 'No signals yet'}",
        "\n<i>All status information reflects live persisted system heartbeat metadata.</i>",
    ]
    await update.message.reply_html("\n".join(lines))


async def cmd_health(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db: Database = context.application.bot_data["db"]
    pending = await db.get_pending_notifications(limit=100)
    meta = await db.get_all_metadata()
    
    lines = [
        "🩺 <b>NEOELLA TRADE · DIAGNOSTIC HEALTH</b>\n",
        "<b>Database:</b> Connected (WAL mode)",
        f"<b>Pending Outbox Notifications:</b> {len(pending)}",
        f"<b>Last Scanner Heartbeat:</b> {meta.get('last_scan_at', {}).get('value', 'None')}",
        f"<b>Last Tracker Heartbeat:</b> {meta.get('last_tracker_at', {}).get('value', 'None')}",
    ]
    if pending:
        lines.append(f"\n⚠️ <b>Outbox Warning:</b> {len(pending)} items awaiting delivery.")
    await update.message.reply_html("\n".join(lines))


async def cmd_active(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db: Database = context.application.bot_data["db"]
    active = await db.get_active_signals()
    if not active:
        await update.message.reply_text("No active signals right now.")
        return
    lines = ["🟢 <b>NEOELLA TRADE · ACTIVE SIGNALS</b>\n"]
    for s in active:
        risk = abs(s.entry_price - s.stop_loss)
        rr = abs(s.tp2 - s.entry_price) / risk if risk else 0
        lines.append(
            f"{_direction_icon(s.direction.value)} <b>{_safe(s.symbol)} · {s.timeframe.upper()}</b>\n"
            f"Pattern: {_safe(s.pattern)}  ·  <b>{s.direction.value}</b>\n"
            f"Entry: <code>{_fmt_price(s.entry_price)}</code>  |  SL: <code>{_fmt_price(s.stop_loss)}</code>\n"
            f"TP1: <code>{_fmt_price(s.tp1)}</code>  |  TP2: <code>{_fmt_price(s.tp2)}</code>  |  TP3: <code>{_fmt_price(s.tp3)}</code>\n"
            f"Status: <b>{s.status.value}</b>  ·  R:R TP2 {rr:.2f}  ·  Age {_age_label(s.signal_time)}\n"
            f"Signal time: {_fmt_signal_time(s.signal_time)}\n"
        )
    await update.message.reply_html("\n".join(lines))


async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db: Database = context.application.bot_data["db"]
    recent = await db.get_recent_signals(limit=15)
    if not recent:
        await update.message.reply_text("No signal history yet.")
        return
    lines = ["🗂 <b>NEOELLA TRADE · RECENT HISTORY</b>\n"]
    for s in recent:
        pnl = f" | PnL {s.pnl_pct:+.2f}%" if s.pnl_pct is not None else ""
        result = f" · <b>{s.result}</b>" if s.result else ""
        lines.append(
            f"{_direction_icon(s.direction.value)} <b>{_safe(s.symbol)} · {s.timeframe.upper()}</b>\n"
            f"{_safe(s.pattern)}  ·  {s.direction.value}  ·  {s.status.value}{result}{pnl}\n"
            f"Entry {_fmt_price(s.entry_price)}  →  "
            f"{_fmt_price(s.exit_price) if s.exit_price is not None else 'Open'}  ·  {_fmt_signal_time(s.signal_time)}"
        )
    await update.message.reply_html("\n".join(lines))


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "NeoElla Trade is online.\n\n"
        "Professional multi-timeframe market analysis with pattern detection, "
        "EMA 200 trend alignment, and structured TP/SL tracking.\n\n"
        "Use /help to see all available commands."
    )


def build_application(db: Database) -> Application:
    app = Application.builder().token(settings.telegram_bot_token).build()
    app.bot_data["db"] = db
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("health", cmd_health))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("performance", cmd_performance))
    app.add_handler(CommandHandler("active", cmd_active))
    app.add_handler(CommandHandler("history", cmd_history))
    return app

