"""
Robust async wrapper around python-binance's Futures REST endpoints.
Features:
1. Token Bucket Rate Limiter with Binance weight tracking (safe margin below 2400/min).
2. Centralized In-Memory Candle Cache shared by scanner, tracker, and chart generation.
3. Closed-Candle Awareness: caches 1D, 3D, 1W closed candles until the next candle close.
4. Intelligent backoff for 429 / -1003 rate limit errors with global cooldown lock.
5. Cached symbol universe discovery to eliminate redundant exchange_info/ticker calls.
"""
from __future__ import annotations
import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Tuple
import numpy as np
import pandas as pd
from binance import AsyncClient
from binance.exceptions import BinanceAPIException, BinanceRequestException
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from bot.config import settings

log = logging.getLogger("binance")

COLUMNS = ["open_time", "open", "high", "low", "close", "volume", "close_time",
           "quote_volume", "trades", "taker_buy_base", "taker_buy_quote", "ignore"]

RETRYABLE_EXCEPTIONS = (
    BinanceRequestException,
    asyncio.TimeoutError,
    TimeoutError,
    ConnectionError,
    OSError,
)


class BinanceRateLimiter:
    """Async token-bucket rate limiter enforcing Binance IP request weight boundaries."""

    def __init__(self, max_weight_per_min: int = 1200, min_interval_sec: float = 0.05):
        self.capacity = float(max_weight_per_min)
        self.tokens = float(max_weight_per_min)
        self.fill_rate = float(max_weight_per_min) / 60.0  # weight tokens added per second
        self.min_interval = min_interval_sec
        self.last_update = time.monotonic()
        self.last_request_time = 0.0
        self._cooldown_until = 0.0
        self._lock = asyncio.Lock()

    def trigger_cooldown(self, seconds: float):
        """Immediately pauses all subsequent requests across all coroutines."""
        now = time.time()
        self._cooldown_until = max(self._cooldown_until, now + seconds)
        log.warning("[BINANCE RATE LIMIT] Global cooldown active until %s (%.1fs)",
                    datetime.fromtimestamp(self._cooldown_until).strftime('%H:%M:%S'), seconds)

    async def acquire(self, weight: int = 2):
        """Asynchronously wait until enough tokens are available to consume `weight`."""
        async with self._lock:
            # 1. Wait out any active 429/-1003 cooldown
            now_wall = time.time()
            if now_wall < self._cooldown_until:
                wait_sec = self._cooldown_until - now_wall
                log.info("Rate limiter paused for active cooldown: waiting %.1fs...", wait_sec)
                await asyncio.sleep(wait_sec)

            # 2. Refill tokens
            now_mono = time.monotonic()
            elapsed = now_mono - self.last_update
            self.tokens = min(self.capacity, self.tokens + elapsed * self.fill_rate)
            self.last_update = now_mono

            # 3. If tokens insufficient, sleep until filled
            if self.tokens < weight:
                deficit = weight - self.tokens
                wait_time = deficit / self.fill_rate
                await asyncio.sleep(wait_time)
                self.tokens = 0.0
                self.last_update = time.monotonic()
            else:
                self.tokens -= weight

            # 4. Enforce minimum spacing between consecutive HTTP calls
            spacing = self.min_interval - (time.monotonic() - self.last_request_time)
            if spacing > 0:
                await asyncio.sleep(spacing)
            self.last_request_time = time.monotonic()


@dataclass
class CandleCacheEntry:
    df: pd.DataFrame
    fetched_at: float
    latest_close_time: pd.Timestamp


class CandleCache:
    """In-memory cache for OHLCV closed candles with closed-candle awareness."""

    def __init__(self):
        self._cache: Dict[Tuple[str, str], CandleCacheEntry] = {}

    def get(self, symbol: str, interval: str, max_age_sec: float = 300.0) -> Optional[pd.DataFrame]:
        key = (symbol.upper(), interval.lower())
        entry = self._cache.get(key)
        if entry is None or entry.df.empty:
            return None

        now_utc = datetime.now(timezone.utc)
        now_ts = time.time()

        # Closed-candle boundary awareness:
        # A 1d candle only closes at 00:00:00 UTC.
        # If current time is still within the same day that the latest candle closed,
        # and cached data is less than max_age_sec, it is 100% current.
        if interval.lower() == "1d":
            # Start of current UTC day
            today_start = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
            # Binance close_time is 23:59:59.999. Adding 1 second brings it to next day 00:00:00.
            if (entry.latest_close_time + timedelta(seconds=1)) >= today_start:
                # Latest closed candle is yesterday's close (closed today at 00:00 UTC)
                return entry.df.copy()
            # If not yet closed or older, check age
            if (now_ts - entry.fetched_at) < max_age_sec:
                return entry.df.copy()
            return None

        elif interval.lower() in ("3d", "1w"):
            # 3D and 1W candles close only every 3 days or once a week.
            # Cache for at least max_age_sec (or up to 1800s for macro timeframes)
            effective_ttl = max(max_age_sec, 900.0)
            if (now_ts - entry.fetched_at) < effective_ttl:
                return entry.df.copy()
            return None

        # General fallback TTL for any other intervals
        if (now_ts - entry.fetched_at) < max_age_sec:
            return entry.df.copy()
        return None

    def set(self, symbol: str, interval: str, df: pd.DataFrame):
        key = (symbol.upper(), interval.lower())
        latest_close = df["close_time"].iloc[-1] if not df.empty and "close_time" in df.columns else pd.Timestamp.now(tz=timezone.utc)
        self._cache[key] = CandleCacheEntry(
            df=df.copy(),
            fetched_at=time.time(),
            latest_close_time=latest_close,
        )

    def clear(self):
        self._cache.clear()


class BinanceMarket:
    def __init__(self):
        self._client: AsyncClient | None = None
        self._rate_limiter = BinanceRateLimiter(
            max_weight_per_min=settings.rate_limit_weight_per_min,
            min_interval_sec=settings.rate_limit_min_interval_sec,
        )
        self._candle_cache = CandleCache()
        self._symbol_universe_cache: list[str] = []
        self._symbol_universe_cache_time: float = 0.0

    async def connect(self):
        self._client = await AsyncClient.create(
            api_key=settings.binance_api_key or None,
            api_secret=settings.binance_api_secret or None,
            testnet=settings.use_testnet,
        )
        log.info("Connected to Binance Futures API (testnet=%s, rate_limit=%d weight/min)",
                 settings.use_testnet, settings.rate_limit_weight_per_min)

    async def close(self):
        if self._client:
            await self._client.close_connection()

    async def get_symbol_universe(self, force_refresh: bool = False) -> list[str]:
        """Discovers active USDT-M Perpetual futures, cached for 6 hours to eliminate
        wasteful repeated exchange_info calls."""
        if settings.symbol_mode.upper() == "CUSTOM":
            return list(settings.custom_symbols)

        now = time.time()
        # Return cached universe if fresh (within 6 hours)
        if not force_refresh and self._symbol_universe_cache and (now - self._symbol_universe_cache_time < 21600):
            return list(self._symbol_universe_cache)

        if not self._client:
            raise RuntimeError("Binance client is not connected")

        # Weight for exchange_info is 1
        await self._rate_limiter.acquire(weight=1)
        async with asyncio.timeout(settings.request_timeout_sec):
            info = await self._client.futures_exchange_info()
            symbols = [
                s["symbol"] for s in info.get("symbols", [])
                if s.get("contractType") == "PERPETUAL"
                and s.get("quoteAsset") == "USDT"
                and s.get("status") == "TRADING"
            ]
            if settings.max_symbols and settings.max_symbols > 0:
                await self._rate_limiter.acquire(weight=40)
                tickers = await self._client.futures_ticker()
                vol_by_symbol = {t["symbol"]: float(t.get("quoteVolume", 0)) for t in tickers if "symbol" in t}
                symbols.sort(key=lambda s: vol_by_symbol.get(s, 0), reverse=True)
                symbols = symbols[: settings.max_symbols]

            self._symbol_universe_cache = list(symbols)
            self._symbol_universe_cache_time = now
            log.info("Cached Binance symbol universe: %d USDT-M Perpetual contracts", len(symbols))
            return symbols

    async def get_klines(self, symbol: str, interval: str, limit: int, force_refresh: bool = False) -> pd.DataFrame:
        """Returns validated CLOSED candles with shared cache deduplication, rate limiting,
        and automatic 429/-1003 backoff."""
        if not self._client:
            raise RuntimeError("Binance client is not connected")

        # 1. Check shared in-memory cache
        if not force_refresh:
            cached_df = self._candle_cache.get(symbol, interval, max_age_sec=settings.candle_cache_ttl_sec)
            if cached_df is not None and not cached_df.empty:
                return cached_df

        # 2. Calculate request weight
        req_weight = 1 if limit < 100 else (2 if limit < 500 else 5)

        # 3. Resilient fetch with rate-limiting & backoff
        max_attempts = 4
        raw = None
        for attempt in range(1, max_attempts + 1):
            await self._rate_limiter.acquire(weight=req_weight)
            try:
                async with asyncio.timeout(settings.request_timeout_sec):
                    raw = await self._client.futures_klines(symbol=symbol, interval=interval, limit=limit + 1)
                break
            except BinanceAPIException as exc:
                if exc.code == -1003 or exc.status_code == 429:
                    retry_sec = 60.0
                    if hasattr(exc, "response") and exc.response is not None:
                        retry_sec = float(getattr(exc.response, "headers", {}).get("Retry-After", 60.0))
                    log.warning("[BINANCE RATE LIMIT] Hit code=%s status=%s on %s %s. Cooling down for %.1fs (attempt %d/%d)...",
                                exc.code, exc.status_code, symbol, interval, retry_sec, attempt, max_attempts)
                    self._rate_limiter.trigger_cooldown(retry_sec)
                    await asyncio.sleep(retry_sec)
                    if attempt == max_attempts:
                        raise
                else:
                    log.warning("Binance API error on %s %s (code=%s): %s", symbol, interval, exc.code, exc.message)
                    if attempt == max_attempts:
                        raise
                    await asyncio.sleep(1.0 * attempt)
            except (BinanceRequestException, asyncio.TimeoutError, TimeoutError, ConnectionError, OSError) as exc:
                log.warning("Network hiccup on %s %s: %s (attempt %d/%d)", symbol, interval, exc, attempt, max_attempts)
                if attempt == max_attempts:
                    raise
                await asyncio.sleep(1.0 * attempt)

        if not raw:
            return pd.DataFrame(columns=COLUMNS)

        df = pd.DataFrame(raw, columns=COLUMNS)
        for c in ["open", "high", "low", "close", "volume", "quote_volume"]:
            df[c] = df[c].astype(float)
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
        df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)

        # Drop the last (still-forming) candle.
        df = df.iloc[:-1].reset_index(drop=True)
        if df.empty:
            return df

        ohlc = df[["open", "high", "low", "close"]].to_numpy()
        volumes = df["volume"].to_numpy()

        # Strict validation of OHLC and timestamps
        is_invalid = (
            df["open_time"].duplicated().any()
            or not df["open_time"].is_monotonic_increasing
            or not df["close_time"].is_monotonic_increasing
            or not np.isfinite(ohlc).all()
            or (ohlc <= 0).any()
            or (volumes < 0).any()
            or (ohlc[:, 1] < ohlc[:, 0]).any()  # High < Open
            or (ohlc[:, 1] < ohlc[:, 3]).any()  # High < Close
            or (ohlc[:, 2] > ohlc[:, 0]).any()  # Low > Open
            or (ohlc[:, 2] > ohlc[:, 3]).any()  # Low > Close
        )
        if is_invalid:
            raise ValueError(f"Invalid or corrupted OHLC data received for {symbol} {interval}")

        # Store validated closed candles in centralized cache
        self._candle_cache.set(symbol, interval, df)
        return df

    async def get_latest_closed_candle(self, symbol: str, interval: str) -> pd.Series | None:
        df = await self.get_klines(symbol, interval, limit=2)
        if df.empty:
            return None
        return df.iloc[-1]

    async def get_mark_price(self, symbol: str) -> float:
        if not self._client:
            raise RuntimeError("Binance client is not connected")
        await self._rate_limiter.acquire(weight=1)
        async with asyncio.timeout(settings.request_timeout_sec):
            t = await self._client.futures_symbol_ticker(symbol=symbol)
            price = float(t["price"])
            if not np.isfinite(price) or price <= 0:
                raise ValueError(f"Received invalid mark price {price} for {symbol}")
            return price


