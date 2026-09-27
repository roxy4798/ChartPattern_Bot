"""
Strict validation rules before publishing to Binance Square.
"""
from __future__ import annotations
import math
from typing import Tuple
from bot.models import Signal, PatternResult, Direction
from bot.config import settings


def compute_signal_hash(signal: Signal) -> str:
    """Deterministic unique hash/key for a signal."""
    return f"{signal.symbol}|{signal.timeframe.upper()}|{signal.direction.value}|{signal.signal_time}|{signal.pattern}"


def validate_square_post(signal: Signal, pattern: PatternResult, daily_post_count: int, content: str) -> Tuple[bool, str]:
    """Validate that signal satisfies all requirements for Binance Square."""
    if not signal or not pattern:
        return False, "missing_signal_or_pattern"

    if not signal.symbol or not signal.timeframe or not signal.direction:
        return False, "incomplete_signal_identity"

    if signal.direction not in (Direction.LONG, Direction.SHORT):
        return False, "invalid_direction"

    # Numeric validation
    for name, val in [
        ("entry_price", signal.entry_price),
        ("stop_loss", signal.stop_loss),
        ("tp1", signal.tp1),
        ("tp2", signal.tp2),
        ("tp3", signal.tp3),
    ]:
        if val is None or not isinstance(val, (int, float)) or not math.isfinite(val) or val <= 0:
            return False, f"invalid_or_missing_{name}"

    # Directional integrity check
    if signal.direction == Direction.LONG:
        if not (signal.stop_loss < signal.entry_price < signal.tp1 <= signal.tp2 <= signal.tp3):
            return False, "invalid_long_price_ordering"
    else:
        if not (signal.stop_loss > signal.entry_price > signal.tp1 >= signal.tp2 >= signal.tp3):
            return False, "invalid_short_price_ordering"

    # Daily quota check
    if daily_post_count >= settings.square_max_posts_per_day:
        return False, f"daily_limit_reached_{daily_post_count}_{settings.square_max_posts_per_day}"

    # Content length check
    if len(content) > settings.square_max_text_length:
        return False, f"content_too_long_{len(content)}_{settings.square_max_text_length}"

    return True, "valid"
