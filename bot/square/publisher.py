"""
Publisher orchestrator for Binance Square.
Coordinates: Formatter -> Validator -> Deduplication -> Circuit Breaker -> Client -> DB State.
Guaranteed 100% fail-safe: All exceptions are caught and contained within this module.
"""
from __future__ import annotations
import asyncio
import logging
import os
from typing import Optional

from bot.config import settings
from bot.models import Signal, PatternResult
from bot.database import Database
from bot.square.models import SquarePostResult, SquareStatus
from bot.square.formatter import format_square_post
from bot.square.validator import validate_square_post, compute_signal_hash
from bot.square.circuit_breaker import SquareCircuitBreaker
from bot.square.client import BinanceSquareClient

log = logging.getLogger("square")


class SquarePublisher:
    def __init__(self, db: Database, notifier=None):
        self.db = db
        self.notifier = notifier
        self.client = BinanceSquareClient()
        self.circuit_breaker = SquareCircuitBreaker()

    async def publish_signal(
        self,
        signal: Signal,
        pattern: PatternResult,
        reason: str,
        chart_png: Optional[bytes] = None,
    ) -> SquarePostResult:
        """Process and publish a final signal to Binance Square (or dry-run).

        Guaranteed non-blocking and fully fail-safe.
        """
        # Outer safety boundary
        try:
            if not settings.square_enabled:
                return SquarePostResult(
                    success=False,
                    status=SquareStatus.SKIPPED_DISABLED,
                    error_message="Square is disabled (SQUARE_ENABLED=false)",
                )

            # 1. Circuit breaker check
            if self.circuit_breaker.is_open:
                log.warning("[SQUARE] SKIPPED: Circuit breaker is currently OPEN due to repeated failures.")
                return SquarePostResult(
                    success=False,
                    status=SquareStatus.SKIPPED_DISABLED,
                    error_code="CIRCUIT_BREAKER_OPEN",
                    error_message="Circuit breaker is open",
                )

            # 2. Duplicate check (Idempotency)
            signal_hash = compute_signal_hash(signal)
            existing = await self.db.get_square_post(signal.id)
            if existing and existing.get("status") in (
                SquareStatus.SUCCESS.value,
                SquareStatus.SKIPPED_DRY_RUN.value,
            ):
                log.info("[SQUARE] SKIPPED_DUPLICATE: Signal %s (%s) already posted.", signal.symbol, signal.id[:8])
                return SquarePostResult(
                    success=True,
                    status=SquareStatus.SKIPPED_DUPLICATE,
                    post_id=existing.get("post_id"),
                )

            # 3. Deterministic Formatting
            content = format_square_post(signal, pattern)

            # 4. Daily Limit Check
            daily_count = await self.db.get_square_posts_today_count()

            # 5. Strict Validation
            is_valid, val_reason = validate_square_post(signal, pattern, daily_count, content)
            if not is_valid:
                status = SquareStatus.SKIPPED_LIMIT if "daily_limit" in val_reason else SquareStatus.SKIPPED_INVALID
                log.warning("[SQUARE] %s: Signal %s (%s) failed validation: %s", status.value, signal.symbol, signal.id[:8], val_reason)
                await self.db.record_square_post(
                    signal_id=signal.id,
                    signal_hash=signal_hash,
                    status=status.value,
                    error_code="VALIDATION_FAILED",
                    error_message=val_reason,
                )
                return SquarePostResult(
                    success=False,
                    status=status,
                    error_code="VALIDATION_FAILED",
                    error_message=val_reason,
                    content=content,
                )

            # 6. Execute Publish (with official post-image.mjs if chart_png provided)
            temp_chart_path = None
            try:
                if chart_png:
                    temp_chart_path = os.path.abspath(f"bot/data/temp_chart_{signal.id[:8]}.png")
                    os.makedirs(os.path.dirname(temp_chart_path), exist_ok=True)
                    with open(temp_chart_path, "wb") as f:
                        f.write(chart_png)

                    result = await self.client.post_image(
                        content=content,
                        chart_path=temp_chart_path,
                        dry_run=settings.square_dry_run,
                    )
                else:
                    result = await self.client.post_content(
                        content=content,
                        dry_run=settings.square_dry_run,
                    )
            finally:
                if temp_chart_path and os.path.exists(temp_chart_path):
                    try:
                        os.remove(temp_chart_path)
                    except Exception:
                        pass

            # 7. Record State in SQLite
            await self.db.record_square_post(
                signal_id=signal.id,
                signal_hash=signal_hash,
                status=result.status.value,
                post_id=result.post_id,
                error_code=result.error_code,
                error_message=result.error_message,
            )

            # 8. Update Circuit Breaker
            if result.success:
                self.circuit_breaker.record_success()
                log.info(
                    "[SQUARE] %s: %s %s %s (post_id=%s)",
                    result.status.value,
                    signal.symbol,
                    signal.timeframe.upper(),
                    signal.direction.value,
                    result.post_id,
                )


            else:
                self.circuit_breaker.record_failure(f"{result.error_code}: {result.error_message}")
                if self.circuit_breaker.is_open and self.notifier and settings.telegram_chat_id:
                    try:
                        alert_text = (
                            "⚠️ <b>BINANCE SQUARE ALERT</b>\n\n"
                            f"Square circuit breaker tripped after {settings.square_failure_threshold} failures.\n"
                            f"Last error: {result.error_code} - {result.error_message}\n\n"
                            "Square posting has been temporarily paused. Existing bot and Telegram remain active."
                        )
                        await self.notifier.app.bot.send_message(
                            chat_id=settings.telegram_chat_id,
                            text=alert_text,
                            parse_mode="HTML",
                        )
                    except Exception:
                        pass

            return result

        except Exception as exc:
            # Absolute safety net: never allow Square exceptions to propagate to main loop
            log.exception("[SQUARE] Unexpected error in publish_signal: %s", exc)
            return SquarePostResult(
                success=False,
                status=SquareStatus.FAILED,
                error_code="UNEXPECTED_EXCEPTION",
                error_message=str(exc),
            )
