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
    max_symbols: int = _int("MAX_SYMBOLS", 40)  # 0 = no cap; start small locally

    # --- Timeframes to scan (Binance kline intervals) ---
    timeframes: tuple = ("1d", "4h", "1h", "15m")

    # --- Pivot / pattern detection (mirrors the Pine Script inputs) ---
    lb_left: int = _int("LB_LEFT", 10)
    lb_right: int = _int("LB_RIGHT", 10)
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
    sl_pct_of_target_dist: float = _float("SL_PCT_TARGET_DIST", 25.0) / 100
    tp1_fraction: float = _float("TP1_FRACTION", 0.5)   # fraction of measured move
    tp2_fraction: float = _float("TP2_FRACTION", 1.0)
    tp3_fraction: float = _float("TP3_FRACTION", 1.5)
    taker_fee_pct: float = _float("TAKER_FEE_PCT", 0.04) / 100  # per side

    # --- History fetched per scan ---
    candles_lookback: int = _int("CANDLES_LOOKBACK", 500)

    # --- Scan cadence (seconds) ---
    scan_interval_sec: int = _int("SCAN_INTERVAL_SEC", 60)
    tracker_interval_sec: int = _int("TRACKER_INTERVAL_SEC", 30)

    # --- Storage ---
    db_path: str = os.getenv("DB_PATH", "bot/data/trading.db")
    log_path: str = os.getenv("LOG_PATH", "bot/data/bot.log")

    enabled_patterns: tuple = field(default_factory=lambda: (
        "Double Top", "Double Bottom", "Triple Top", "Triple Bottom",
        "Head & Shoulders", "Inv Head & Shoulders",
        "Bullish Flag", "Bearish Flag", "Bullish Pennant", "Bearish Pennant",
        "Rising Wedge", "Falling Wedge",
        "Ascending Triangle", "Descending Triangle", "Symmetrical Triangle",
        "Rectangle", "Cup & Handle", "Inv Cup & Handle",
    ))


settings = Settings()
