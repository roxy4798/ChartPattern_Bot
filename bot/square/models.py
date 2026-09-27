"""
Data models and lifecycle enums for the Binance Square add-on distribution channel.
"""
from __future__ import annotations
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class SquareStatus(str, Enum):
    PENDING = "PENDING"
    SUCCESS = "SUCCESS"
    SKIPPED_DRY_RUN = "SKIPPED_DRY_RUN"
    SKIPPED_DUPLICATE = "SKIPPED_DUPLICATE"
    SKIPPED_INVALID = "SKIPPED_INVALID"
    SKIPPED_LIMIT = "SKIPPED_LIMIT"
    SKIPPED_DISABLED = "SKIPPED_DISABLED"
    FAILED = "FAILED"


@dataclass
class SquarePostResult:
    success: bool
    status: SquareStatus
    post_id: Optional[str] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    content: Optional[str] = None


@dataclass
class SquarePostRecord:
    signal_id: str
    signal_hash: str
    status: SquareStatus
    post_id: Optional[str] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    attempt_count: int = 0
    last_attempt_at: Optional[str] = None
    created_at: Optional[str] = None
