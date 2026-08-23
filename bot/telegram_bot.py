from __future__ import annotations
import io
import logging
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


def build_signal_message(s: Signal, reason: str, dist_to_ema_pct: float) -> str:
    side = DIR_EMOJI[s.direction.value]
    pos = "above" if s.price_at_signal > s.ema200_at_signal else "below"
    rr = abs(s.tp2 - s.entry_price) / abs(s.entry_price - s.stop_loss) if s.entry_price != s.stop_loss else 0
    return (
        f"{side} <b>{s.direction.value} SIGNAL</b>\n"
        f"<b>{s.symbol}</b>\n"
        f"<b>TIMEFRAME:</b> {s.timeframe.upper()}\n"
        f"Pattern: {s.pattern}\n\n"
        f"<b>PATTERN ANALYSIS</b>\n"
        f"• Name: {s.pattern}\n"
        f"• Status: Confirmed (closed-candle breakout)\n"
        f"• Breakout level: {_fmt_price(s.entry_price)}\n"
        f"• Confirmation: Closed candle\n\n"
        f"<b>EMA 200 FILTER</b>\n"
        f"• Price at signal: {_fmt_price(s.price_at_signal)}\n"
        f"• EMA200: {_fmt_price(s.ema200_at_signal)}\n"
        f"• Position: Price {pos} EMA200\n"
        f"• Distance: {dist_to_ema_pct:+.2f}%\n\n"
        f"<b>ENTRY</b>\n"
        f"• Entry: {_fmt_price(s.entry_price)}\n\n"
        f"<b>RISK MANAGEMENT</b>\n"
        f"• Stop Loss: {_fmt_price(s.stop_loss)}\n"
        f"• TP1: {_fmt_price(s.tp1)}\n"
        f"• TP2: {_fmt_price(s.tp2)}\n"
        f"• TP3: {_fmt_price(s.tp3)}\n"
        f"• R:R (to TP2): {rr:.2f}\n\n"
        f"<b>SIGNAL REASON</b>\n{reason}\n\n"
        f"<i>ID: {s.id[:8]}</i>"
    )


def build_update_message(s: Signal, event: str, price: float) -> str:
    side = DIR_EMOJI[s.direction.value]
    pnl = s.pnl_pct
    if event == "SL_HIT":
        header = f"{side} <b>{s.direction.value} UPDATE</b>\n{s.symbol} • {s.timeframe.upper()}\nPattern: {s.pattern}\n\n🔴 <b>SL HIT</b>\nPrice: {_fmt_price(price)}\n"
        if pnl is not None:
            header += f"PnL: {pnl:+.2f}%\n"
        header += "\nStatus: <b>CLOSED</b>"
        return header
    header = (f"{side} <b>{s.direction.value} UPDATE</b>\n{s.symbol} • {s.timeframe.upper()}\n"
              f"Pattern: {s.pattern}\n\n✅ <b>{event.replace('_', ' ')}</b>\nPrice: {_fmt_price(price)}\n")
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
        f"Pattern: {s.pattern}"
    )


class Notifier:
    def __init__(self, app: Application, chat_id: str):
        self.app = app
        self.chat_id = chat_id

    async def send_signal(self, s: Signal, reason: str, chart_png: bytes) -> tuple[int, int]:
        dist = (s.price_at_signal - s.ema200_at_signal) / s.ema200_at_signal * 100
        text = build_signal_message(s, reason, dist)
        photo_msg = await self.app.bot.send_photo(
            chat_id=self.chat_id, photo=InputFile(io.BytesIO(chart_png), filename=f"{s.symbol}_{s.timeframe}.png"),
        )
        text_msg = await self.app.bot.send_message(
            chat_id=self.chat_id, text=text, parse_mode=ParseMode.HTML,
        )
        return text_msg.message_id, photo_msg.message_id

    async def send_update(self, s: Signal, event: str, price: float):
        text = build_update_message(s, event, price)
        await self.app.bot.send_message(chat_id=self.chat_id, text=text, parse_mode=ParseMode.HTML)

    async def send_closed(self, s: Signal, result: str):
        text = build_closed_message(s, result)
        await self.app.bot.send_message(chat_id=self.chat_id, text=text, parse_mode=ParseMode.HTML)

    async def flush_notifications(self, db: Database):
        """Deliver persisted state events in order; failures remain retryable."""
        for item in await db.get_pending_notifications():
            try:
                signal = await db.get_signal(item["signal_id"])
                if signal is None:
                    raise ValueError(f"Signal {item['signal_id']} no longer exists")
                payload = item["payload"]
                if item["kind"] == "update":
                    await self.send_update(signal, payload["event"], payload["price"])
                elif item["kind"] == "closed":
                    await self.send_closed(signal, payload["result"])
                else:
                    raise ValueError(f"Unknown notification kind: {item['kind']}")
                await db.mark_notification_delivered(item["id"])
            except Exception as exc:
                await db.mark_notification_failed(item["id"], repr(exc))
                log.exception("Notification delivery failed for outbox item %s", item["id"])
                break


def _bucket_line(name: str, b: Bucket) -> str:
    return f"{name} → {b.winrate:.0f}% WR | {b.total_pnl:+.2f}% PnL ({b.signals} signals)"


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db: Database = context.application.bot_data["db"]
    report = await build_report(db)
    o = report.overall
    lines = [
        "📊 <b>TRADING PERFORMANCE</b>\n",
        f"Total Signals: {o.signals}",
        f"Wins: {o.wins}",
        f"Losses: {o.losses}",
        f"Winrate: <b>{o.winrate:.2f}%</b>",
        f"Total PnL: <b>{o.total_pnl:+.2f}%</b>\n",
        "<b>TIMEFRAME PERFORMANCE</b>",
    ]
    for tf in ("1d", "4h", "1h", "15m"):
        b = report.by_timeframe.get(tf)
        if b and b.signals:
            lines.append(_bucket_line(tf.upper(), b))
    if report.by_pattern:
        best_pat = max(report.by_pattern.items(), key=lambda kv: kv[1].winrate if kv[1].signals else -1, default=(None, None))
        worst_pat = min(report.by_pattern.items(), key=lambda kv: kv[1].winrate if kv[1].signals else 999, default=(None, None))
        if best_pat[0]:
            lines.append(f"\n🟢 Best Pattern: {best_pat[0]}")
        if worst_pat[0]:
            lines.append(f"🔴 Worst Pattern: {worst_pat[0]}")
    if report.best_trade:
        lines.append(f"📈 Best Trade: {report.best_trade.pnl_pct:+.2f}%")
    if report.worst_trade:
        lines.append(f"📉 Worst Trade: {report.worst_trade.pnl_pct:+.2f}%")
    lines.append(f"\n🔁 Max Consecutive Wins: {report.max_consecutive_wins}")
    lines.append(f"🔁 Max Consecutive Losses: {report.max_consecutive_losses}")
    await update.message.reply_html("\n".join(lines))


async def cmd_performance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db: Database = context.application.bot_data["db"]
    report = await build_report(db)
    lines = ["📈 <b>FULL PERFORMANCE REPORT</b>\n", "<b>By Pattern</b>"]
    for name, b in sorted(report.by_pattern.items(), key=lambda kv: -kv[1].signals):
        lines.append(
            f"• {name}: {b.signals} signals | {b.winrate:.0f}% WR | avg {b.avg_pnl:+.2f}% | total {b.total_pnl:+.2f}%"
        )
    lines.append("\n<b>By Timeframe</b>")
    for tf, b in report.by_timeframe.items():
        lines.append(f"• {tf.upper()}: {b.signals} signals | {b.winrate:.0f}% WR | total {b.total_pnl:+.2f}%")
    lines.append("\n<b>Target hit-rates (overall)</b>")
    o = report.overall
    total = max(o.signals, 1)
    lines.append(f"TP1: {o.tp1}/{total} | TP2: {o.tp2}/{total} | TP3: {o.tp3}/{total} | SL: {o.sl}/{total}")
    await update.message.reply_html("\n".join(lines))


async def cmd_active(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db: Database = context.application.bot_data["db"]
    active = await db.get_active_signals()
    if not active:
        await update.message.reply_text("No active signals right now.")
        return
    lines = ["🟢 <b>ACTIVE SIGNALS</b>\n"]
    for s in active:
        lines.append(
            f"{DIR_EMOJI[s.direction.value]} {s.symbol} {s.timeframe.upper()} — {s.pattern}\n"
            f"   Entry {_fmt_price(s.entry_price)} | SL {_fmt_price(s.stop_loss)} | Status: {s.status.value}"
        )
    await update.message.reply_html("\n".join(lines))


async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db: Database = context.application.bot_data["db"]
    recent = await db.get_recent_signals(limit=15)
    if not recent:
        await update.message.reply_text("No signal history yet.")
        return
    lines = ["🗂 <b>RECENT SIGNALS</b>\n"]
    for s in recent:
        pnl = f" | PnL {s.pnl_pct:+.2f}%" if s.pnl_pct is not None else ""
        lines.append(f"{DIR_EMOJI[s.direction.value]} {s.symbol} {s.timeframe.upper()} {s.pattern} — {s.status.value}{pnl}")
    await update.message.reply_html("\n".join(lines))


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Multi-timeframe pattern signal bot online.\n"
        "/active — currently open signals\n"
        "/stats — performance summary\n"
        "/performance — full breakdown by pattern & timeframe\n"
        "/history — recent signals"
    )


def build_application(db: Database) -> Application:
    app = Application.builder().token(settings.telegram_bot_token).build()
    app.bot_data["db"] = db
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("performance", cmd_performance))
    app.add_handler(CommandHandler("active", cmd_active))
    app.add_handler(CommandHandler("history", cmd_history))
    return app
