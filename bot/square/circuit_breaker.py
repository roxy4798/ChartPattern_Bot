"""
Circuit breaker for Binance Square API failures.
If 5 consecutive failures occur, the circuit opens to protect system stability.
"""
from __future__ import annotations
import logging
from bot.config import settings

log = logging.getLogger("square")


class SquareCircuitBreaker:
    def __init__(self):
        self._consecutive_failures = 0
        self._is_open = False

    @property
    def is_open(self) -> bool:
        return self._is_open

    def record_success(self):
        if self._consecutive_failures > 0 or self._is_open:
            log.info("[SQUARE] Circuit breaker reset after successful operation.")
        self._consecutive_failures = 0
        self._is_open = False

    def record_failure(self, reason: str = ""):
        self._consecutive_failures += 1
        log.warning(
            "[SQUARE] Failure recorded (%d/%d): %s",
            self._consecutive_failures,
            settings.square_failure_threshold,
            reason,
        )
        if self._consecutive_failures >= settings.square_failure_threshold:
            if not self._is_open:
                self._is_open = True
                log.error(
                    "[SQUARE] 🔴 CIRCUIT BREAKER TRIPPED! Binance Square posting temporarily disabled "
                    "after %d consecutive failures. Existing bot & Telegram remain 100%% active.",
                    self._consecutive_failures,
                )
