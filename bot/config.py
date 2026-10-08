"""
Central configuration. All values are overridable via environment variables
(see .env.example). Keep secrets out of source control.
"""
from __future__ import annotations
import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


def _bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    v = os.getenv(name)
    return default if v is None else int(v)


def _float(name: str, default: float) -> float:
    v = os.getenv(name)
    return default if v is None else float(v)


@dataclass
class Settings:
    # --- Telegram ---
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")

    # --- Binance ---
    binance_api_key: str = os.getenv("BINANCE_API_KEY", "")
    binance_api_secret: str = os.getenv("BINANCE_API_SECRET", "")
    use_testnet: bool = _bool("BINANCE_TESTNET", False)

    # --- Symbol universe ---
    # "ALL" scans every active USDT-M perpetual future on Binance.
    # For local testing this is heavy (300+ symbols x 4 timeframes), so
    # MAX_SYMBOLS caps the universe by 24h quote volume unless set to 0.
    symbol_mode: str = os.getenv("SYMBOL_MODE", "ALL")  # "ALL" or "CUSTOM"
    custom_symbols: tuple = tuple(
        s.strip().upper() for s in os.getenv("CUSTOM_SYMBOLS", "").split(",") if s.strip()
    )
    max_symbols: int = _int("MAX_SYMBOLS", 0)  # 0 = no cap (all coins on Binance Futures)


    # --- Timeframes to scan (Binance kline intervals) ---
    timeframes: tuple = ("1d", "3d", "1w")


    # --- Structural Fractal Period & Trendline Parameters ---
    structural_fractal_period: int = _int("STRUCTURAL_FRACTAL_PERIOD", 30)
    structural_min_span: int = _int("STRUCTURAL_MIN_SPAN", 65)
    trendline_min_pivot_dist: int = _int("TRENDLINE_MIN_PIVOT_DIST", 20)
    trendline_min_touches: int = _int("TRENDLINE_MIN_TOUCHES", 2)
    trendline_touch_tol_atr: float = _float("TRENDLINE_TOUCH_TOL_ATR", 0.35)
    trendline_max_violations: int = _int("TRENDLINE_MAX_VIOLATIONS", 1)
    major_pivot_lb: int = _int("MAJOR_PIVOT_LB", 30)
    medium_pivot_lb: int = _int("MEDIUM_PIVOT_LB", 25)

    # --- Pivot / pattern detection (mirrors the Pine Script inputs) ---
    lb_left: int = _int("LB_LEFT", 30)
    lb_right: int = _int("LB_RIGHT", 20)
    cooldown_bars: int = _int("COOLDOWN_BARS", 5)
    sym_tol: float = _float("SYM_TOL_PCT", 10.0) / 100
    lvl_tol: float = _float("LVL_TOL_PCT", 3.0) / 100
    min_size_pct: float = _float("MIN_SIZE_PCT", 0.5) / 100
    min_atr_mult: float = _float("MIN_ATR_MULT", 1.0)
    ema_period: int = _int("EMA_PERIOD", 200)
    atr_period: int = _int("ATR_PERIOD", 14)

    # --- Breakout confirmation ---
    use_atr_breakout: bool = _bool("USE_ATR_BRK", True)
    break_atr_mult: float = _float("BREAK_ATR_MULT", 1.0)
    break_pct: float = _float("BREAK_PCT", 0.3) / 100
    require_close_break: bool = _bool("REQ_CLOSE_BRK", True)  # always use closed candles

    # --- Risk management ---
    min_rr: float = _float("MIN_RR", 1.0)
    sl_pct_of_target_dist: float = _float("SL_PCT_TARGET_DIST", 50.0) / 100  # 50% of target distance for wide breathing room
    use_r_multiple_tp: bool = _bool("USE_R_MULTIPLE_TP", True)
    tp1_r_multiple: float = _float("TP1_R_MULT", 1.0)   # 1.0x Risk (1:1 R:R)
    tp2_r_multiple: float = _float("TP2_R_MULT", 2.0)   # 2.0x Risk (1:2 R:R)
    tp3_r_multiple: float = _float("TP3_R_MULT", 3.0)   # 3.0x Risk (1:3 R:R)
    tp1_fraction: float = _float("TP1_FRACTION", 0.5)   # fallback fraction of measured move
    tp2_fraction: float = _float("TP2_FRACTION", 1.0)
    tp3_fraction: float = _float("TP3_FRACTION", 1.5)
    taker_fee_pct: float = _float("TAKER_FEE_PCT", 0.04) / 100  # per side

    # --- History fetched per scan ---
    candles_lookback: int = _int("CANDLES_LOOKBACK", 500)

    # --- Scan cadence (seconds) ---
    scan_interval_sec: int = _int("SCAN_INTERVAL_SEC", 60)
    tracker_interval_sec: int = _int("TRACKER_INTERVAL_SEC", 30)

    # --- Concurrency, Caching & Rate Limiting ---
    request_timeout_sec: float = _float("REQUEST_TIMEOUT_SEC", 30.0)
    scanner_concurrency: int = _int("SCANNER_CONCURRENCY", 3)
    max_tracker_concurrency: int = _int("MAX_TRACKER_CONCURRENCY", 2)
    rate_limit_weight_per_min: int = _int("RATE_LIMIT_WEIGHT_PER_MIN", 1200)  # Conservative 50% of Binance 2400 limit
    rate_limit_min_interval_sec: float = _float("RATE_LIMIT_MIN_INTERVAL_SEC", 0.05)
    candle_cache_ttl_sec: int = _int("CANDLE_CACHE_TTL_SEC", 300)  # 5 minutes minimum cache
    notification_max_attempts: int = _int("NOTIFICATION_MAX_ATTEMPTS", 10)
    rate_limit_delay_sec: float = _float("RATE_LIMIT_DELAY_SEC", 0.05)

    # --- Storage ---
    db_path: str = os.getenv("DB_PATH", "bot/data/trading.db")
    log_path: str = os.getenv("LOG_PATH", "bot/data/bot.log")

    # --- Binance Square Add-on ---
    square_enabled: bool = _bool("SQUARE_ENABLED", False)
    square_dry_run: bool = _bool("SQUARE_DRY_RUN", True)
    square_openapi_key: str = os.getenv("BINANCE_SQUARE_OPENAPI_KEY", "")
    square_max_posts_per_day: int = _int("SQUARE_MAX_POSTS_PER_DAY", 50)
    square_max_text_length: int = _int("SQUARE_MAX_TEXT_LENGTH", 600)
    square_max_retries: int = _int("SQUARE_MAX_RETRIES", 3)
    square_failure_threshold: int = _int("SQUARE_FAILURE_THRESHOLD", 5)



    enabled_patterns: tuple = field(default_factory=lambda: (
        "Double Bottom", "Triple Bottom", "Inv Head & Shoulders",
        "Bullish Flag", "Bullish Pennant", "Falling Wedge",
        "Ascending Triangle", "Symmetrical Triangle", "Descending Channel",
        "Rectangle", "Cup & Handle",
    ))

    def __post_init__(self):
        if self.min_rr <= 0:
            raise ValueError("MIN_RR must be positive")
        if self.candles_lookback < 220:
            raise ValueError("CANDLES_LOOKBACK must be at least 220 for reliable 200 EMA and pivot calculation")
        if self.tp1_r_multiple <= 0 or self.tp2_r_multiple <= self.tp1_r_multiple or self.tp3_r_multiple <= self.tp2_r_multiple:
            raise ValueError("TP R-multiples must be strictly increasing: 0 < TP1 < TP2 < TP3")
        if self.sl_pct_of_target_dist <= 0:
            raise ValueError("SL_PCT_TARGET_DIST must be positive")
        if self.scan_interval_sec <= 0 or self.tracker_interval_sec <= 0:
            raise ValueError("Intervals must be positive")



settings = Settings()

