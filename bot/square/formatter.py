"""
Deterministic formatter for Binance Square posts.
Strictly formats signal data without modifying numbers or making exaggerated claims.
Enforces the SQUARE_MAX_TEXT_LENGTH safety limit (default 600 chars).
"""
from __future__ import annotations
from bot.models import Signal, PatternResult
from bot.config import settings


def extract_cashtag(symbol: str) -> str:
    """Extract primary base asset as cashtag, e.g. BTCUSDT -> $BTC, 1000PEPEUSDT -> $1000PEPE."""
    sym = symbol.upper()
    for quote in ("USDT", "USDC", "BUSD", "FDUSD"):
        if sym.endswith(quote):
            base = sym[: -len(quote)]
            return f"${base}" if base else f"${sym}"
    return f"${sym}"


def _fmt_price(p: float) -> str:
    return f"{p:.6g}"


def format_square_post(signal: Signal, pattern: PatternResult) -> str:
    """Deterministic post formatter for Binance Square."""
    cashtag = extract_cashtag(signal.symbol)
    side = signal.direction.value
    icon = "🟢" if side == "LONG" else "🔴"
    pos = "above" if signal.price_at_signal >= signal.ema200_at_signal else "below"
    rr = abs(signal.tp2 - signal.entry_price) / abs(signal.entry_price - signal.stop_loss) if signal.entry_price != signal.stop_loss else 0.0

    post = (
        f"🚨 {signal.symbol} {side} SIGNAL\n\n"
        f"{cashtag} | {signal.timeframe.upper()}\n"
        f"📊 Pattern: {pattern.name}\n"
        f"📈 Trend: Price {pos} EMA 200\n\n"
        f"🎯 Entry: {_fmt_price(signal.entry_price)}\n"
        f"🛑 SL: {_fmt_price(signal.stop_loss)}\n"
        f"✅ TP1: {_fmt_price(signal.tp1)}\n"
        f"✅ TP2: {_fmt_price(signal.tp2)}\n"
        f"✅ TP3: {_fmt_price(signal.tp3)}\n"
        f"⚖️ R:R (TP2): 1:{rr:.2f}\n\n"
        f"Confirmed on closed {signal.timeframe.upper()} candle.\n"
        f"Not financial advice.\n"
        f"#Crypto #{signal.symbol}"
    )

    if len(post) > settings.square_max_text_length:
        # Fallback to ultra-compact formatting if needed
        post = (
            f"🚨 {signal.symbol} {side}\n"
            f"{cashtag} · {signal.timeframe.upper()} · {pattern.name}\n\n"
            f"Entry: {_fmt_price(signal.entry_price)}\n"
            f"SL: {_fmt_price(signal.stop_loss)}\n"
            f"TP1: {_fmt_price(signal.tp1)}\n"
            f"TP2: {_fmt_price(signal.tp2)}\n"
            f"TP3: {_fmt_price(signal.tp3)}\n\n"
            f"Closed candle breakout confirmation.\n"
            f"Not financial advice."
        )

    return post
