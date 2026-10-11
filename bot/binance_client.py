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
import aiohttp
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
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


class CandleCache:
    """In-memory cache for OHLCV closed candles with closed-candle awareness."""

    def __init__(self):
        self._cache: Dict[Tuple[str, str], CandleCacheEntry] = {}

    def get(self, symbol: str, interval: str, limit: Optional[int] = None,
            max_age_sec: float = 300.0) -> Optional[pd.DataFrame]:
        key = (symbol.upper(), interval.lower())
        entry = self._cache.get(key)
        if entry is None or entry.df.empty:
            return None
        cached_limit = entry.df.attrs.get("requested_limit")
        has_sufficient_bars = len(entry.df) >= limit if limit is not None else True
        was_requested_at_or_above_limit = (
            cached_limit is not None and limit is not None and cached_limit >= limit
        )
        if not (has_sufficient_bars or was_requested_at_or_above_limit):
            return None

        now_utc = datetime.now(timezone.utc)
        now_ts = time.time()

        # A cached row set must not be treated as current across a candle boundary.
        interval_ms = {"1d": 86_400_000, "3d": 259_200_000, "1w": 604_800_000}.get(interval.lower())
        if interval_ms is not None and interval.lower() != "3d":
            now_ms = int(now_utc.timestamp() * 1000)
            latest_open_ms = int(entry.df["open_time"].iloc[-1].timestamp() * 1000)
            current_open_ms = (now_ms // interval_ms) * interval_ms
            if interval.lower() == "1w":
                monday = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
                current_open_ms = int((monday.timestamp() - monday.weekday() * 86400) * 1000)
            expected_last_open_ms = current_open_ms - interval_ms
            if latest_open_ms < expected_last_open_ms:
                return None

        effective_ttl = max(max_age_sec, 900.0) if interval.lower() == "3d" else max_age_sec
        if (now_ts - entry.fetched_at) < effective_ttl:
            result = entry.df.tail(limit).copy() if limit is not None else entry.df.copy()
            raw_rows = entry.df.attrs.get("binance_raw_klines")
            if raw_rows is not None and limit is not None:
                result.attrs["binance_raw_klines"] = tuple(raw_rows[-len(result):])
            return result
        return None

    def set(self, symbol: str, interval: str, df: pd.DataFrame):
        key = (symbol.upper(), interval.lower())
        self._cache[key] = CandleCacheEntry(
            df=df.copy(),
            fetched_at=time.time(),
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
        self.last_successful_fetch_at: str | None = None
        self._candle_metrics = {
            "requests": 0,
            "failures": 0,
            "recovered": 0,
            "exhausted": 0,
            "validation_failures": 0,
            "cache_hits": 0,
        }

    def candle_health(self) -> dict[str, int | str | None]:
        return {**self._candle_metrics, "last_successful_fetch_at": self.last_successful_fetch_at}

    async def connect(self):
        connector = aiohttp.TCPConnector(keepalive_timeout=30.0)
        self._client = await AsyncClient.create(
            api_key=settings.binance_api_key or None,
            api_secret=settings.binance_api_secret or None,
            testnet=settings.use_testnet,
            session_params={"connector": connector},
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
        if not 1 <= limit <= 1499:
            raise ValueError("limit must be between 1 and 1499 because one extra row is requested to identify open candles")

        # 1. Check shared in-memory cache
        if not force_refresh:
            cached_df = self._candle_cache.get(
                symbol, interval, limit, max_age_sec=settings.candle_cache_ttl_sec
            )
            if cached_df is not None and not cached_df.empty:
                cached_df.attrs["cache_hit"] = True
                self._candle_metrics["cache_hits"] += 1
                return cached_df

        # 2. Calculate weight for the actual API limit (including the extra row).
        api_limit = limit + 1
        req_weight = 1 if api_limit < 100 else (2 if api_limit < 500 else 5)

        # 3. Resilient fetch with rate-limiting & backoff
        max_attempts = 4
        request_started = time.monotonic()
        raw = None
        for attempt in range(1, max_attempts + 1):
            timeout_scope = asyncio.timeout(settings.request_timeout_sec)
            try:
                # Bound the limiter wait as well as network I/O. A stalled
                # limiter must not keep a scanner worker pending indefinitely.
                async with timeout_scope:
                    await self._rate_limiter.acquire(weight=req_weight)
                    self._candle_metrics["requests"] += 1
                    raw = await self._client.futures_klines(symbol=symbol, interval=interval, limit=api_limit)
                if attempt > 1:
                    self._candle_metrics["recovered"] += 1
                    log.info("Kline request recovered for %s %s after %d attempt(s), elapsed=%.2fs",
                             symbol, interval, attempt, time.monotonic() - request_started)
                break
            except BinanceAPIException as exc:
                self._candle_metrics["failures"] += 1
                if exc.code == -1003 or exc.status_code == 429:
                    retry_sec = 60.0
                    if hasattr(exc, "response") and exc.response is not None:
                        retry_sec = float(getattr(exc.response, "headers", {}).get("Retry-After", 60.0))
                    elapsed = time.monotonic() - request_started
                    level = logging.ERROR if attempt == max_attempts else logging.WARNING
                    log.log(level, "[BINANCE RATE LIMIT] %s %s code=%s status=%s elapsed=%.2fs attempt=%d/%d exception=%s%s",
                            symbol, interval, exc.code, exc.status_code, elapsed, attempt, max_attempts,
                            type(exc).__name__, "; exhausted retries" if attempt == max_attempts else f"; cooling down {retry_sec:.1f}s")
                    self._rate_limiter.trigger_cooldown(retry_sec)
                    if attempt == max_attempts:
                        self._candle_metrics["exhausted"] += 1
                        raise
                    await asyncio.sleep(retry_sec)
                else:
                    elapsed = time.monotonic() - request_started
                    level = logging.ERROR if attempt == max_attempts else logging.WARNING
                    log.log(level, "Binance API error on %s %s code=%s elapsed=%.2fs attempt=%d/%d exception=%s%s",
                            symbol, interval, exc.code, elapsed, attempt, max_attempts, type(exc).__name__,
                            "; exhausted retries" if attempt == max_attempts else "; retrying")
                    if attempt == max_attempts:
                        self._candle_metrics["exhausted"] += 1
                        raise
                    await asyncio.sleep(1.0 * attempt)
            except (BinanceRequestException, asyncio.TimeoutError, TimeoutError, ConnectionError, OSError) as exc:
                self._candle_metrics["failures"] += 1
                err_msg = str(exc).strip()
                err_desc = err_msg if err_msg.startswith(type(exc).__name__) else (f"{type(exc).__name__}: {err_msg}" if err_msg else type(exc).__name__)
                elapsed = time.monotonic() - request_started
                level = logging.ERROR if attempt == max_attempts else logging.WARNING
                timeout_source = ("asyncio.timeout deadline" if isinstance(exc, TimeoutError) and timeout_scope.expired()
                                  else "client/transport")
                log.log(level, "Network failure on %s %s elapsed=%.2fs attempt=%d/%d exception=%s source=%s: %s%s",
                        symbol, interval, elapsed, attempt, max_attempts, type(exc).__name__, timeout_source, err_desc,
                        "; exhausted retries" if attempt == max_attempts else "; retrying")
                if attempt == max_attempts:
                    self._candle_metrics["exhausted"] += 1
                    raise
                await asyncio.sleep(1.0 * attempt)

        if not raw:
            return pd.DataFrame(columns=COLUMNS)

        raw_klines = [tuple(row) for row in raw]
        df = pd.DataFrame(raw_klines, columns=COLUMNS)
        df["_raw_row_index"] = np.arange(len(df))
        for c in ["open", "high", "low", "close"]:
            df[c] = df[c].astype(float)
        for c in ["volume", "quote_volume", "taker_buy_base", "taker_buy_quote"]:
            df[c] = df[c].map(Decimal)
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
        df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)

        # Keep exactly the requested number of closed candles. Do not assume the
        # final API row is unfinished: at a boundary Binance may return only
        # closed rows, or a short response may contain no extra row.
        now_utc = pd.Timestamp.now(tz="UTC")
        closed = df["close_time"] < now_utc
        raw_indices = df.loc[closed, "_raw_row_index"].tail(limit).astype(int).tolist()
        df = df.loc[closed].tail(limit).drop(columns=["_raw_row_index"]).reset_index(drop=True)
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
            self._candle_metrics["validation_failures"] += 1
            raise ValueError(f"Invalid or corrupted OHLC data received for {symbol} {interval}")

        # Fractal windows and tracker logic treat row offsets as elapsed bars.
        # Refuse gaps or inconsistent phases instead of silently compressing time.
        interval_ms = {"1d": 86_400_000, "3d": 259_200_000, "1w": 604_800_000}.get(interval.lower())
        if interval_ms is not None and not df.empty:
            # Pandas may preserve datetime64 resolution based on input units
            # (milliseconds in Binance rows). Normalize explicitly before
            # integer arithmetic; astype(int64) alone is resolution-dependent.
            open_ms = df["open_time"].dt.as_unit("ms").astype("int64").to_numpy()
            close_ms = df["close_time"].dt.as_unit("ms").astype("int64").to_numpy()
            if interval.lower() == "3d":
                # Binance documents 3d in UTC but does not specify its phase.
                # Validate only the 72-hour cadence; do not infer a phase.
                misaligned = False
            else:
                offset_ms = {"1d": 0, "1w": 345_600_000}[interval.lower()]
                misaligned = (open_ms % interval_ms != offset_ms).any()
            if len(df) > 1:
                diffs = np.diff(open_ms)
                gap_indices = np.where(diffs != interval_ms)[0]
                if len(gap_indices) > 0:
                    last_gap_idx = gap_indices[-1]
                    contiguous_start = last_gap_idx + 1
                    contiguous_len = len(df) - contiguous_start

                    # Fractal windows and indicators treat adjacent rows as discrete uniform bars.
                    # At least 220 contiguous bars are required (200 EMA warmup + 20 confirmation bars).
                    min_contiguous_bars = 220
                    if contiguous_len < min_contiguous_bars:
                        self._candle_metrics["validation_failures"] += 1
                        gap_samples = [
                            f"idx {i}->{i+1}: open {open_ms[i]} -> {open_ms[i+1]} (delta={diffs[i]}ms, expected={interval_ms}ms)"
                            for i in gap_indices[:3]
                        ]
                        raise ValueError(
                            f"Misaligned or missing {interval} candles received for {symbol}: "
                            f"total_candles={len(df)}, missing/gap: {len(gap_indices)} gap(s) found "
                            f"(latest contiguous suffix has only {contiguous_len} bars < {min_contiguous_bars}); "
                            f"samples: [{', '.join(gap_samples)}]"
                        )

                    # Upstream boundary gap detected (e.g. Binance Futures August 2023 3D realignment).
                    # Retain only the strictly contiguous unbroken suffix ending at the latest closed bar.
                    log.info(
                        "[%s %s] Upstream boundary gap detected at historical index %d; "
                        "discarding %d disjoint historical bars and retaining %d strictly contiguous bars",
                        symbol, interval, last_gap_idx, contiguous_start, contiguous_len
                    )
                    df = df.iloc[contiguous_start:].reset_index(drop=True)
                    raw_indices = raw_indices[contiguous_start:]
                    open_ms = open_ms[contiguous_start:]
                    close_ms = close_ms[contiguous_start:]

            if interval.lower() == "3d":
                misaligned = False
            else:
                offset_ms = {"1d": 0, "1w": 345_600_000}[interval.lower()]
                misaligned = (open_ms % interval_ms != offset_ms).any()

            bad_close = (close_ms != open_ms + interval_ms - 1).any()
            if misaligned or bad_close:
                self._candle_metrics["validation_failures"] += 1
                diag_parts = []
                if misaligned:
                    bad_phase = np.where(open_ms % interval_ms != offset_ms)[0]
                    phase_samples = [
                        f"idx {i}: open={open_ms[i]} (phase={open_ms[i] % interval_ms}ms, expected={offset_ms}ms)"
                        for i in bad_phase[:3]
                    ]
                    diag_parts.append(
                        f"misaligned: {len(bad_phase)} candle(s) with bad phase; samples: [{', '.join(phase_samples)}]"
                    )
                if bad_close:
                    bad_close_idx = np.where(close_ms != open_ms + interval_ms - 1)[0]
                    close_samples = [
                        f"idx {i}: open={open_ms[i]}, close={close_ms[i]} (expected {open_ms[i] + interval_ms - 1})"
                        for i in bad_close_idx[:3]
                    ]
                    diag_parts.append(
                        f"bad_close: {len(bad_close_idx)} candle(s); samples: [{', '.join(close_samples)}]"
                    )
                details = "; ".join(diag_parts)
                raise ValueError(
                    f"Misaligned or missing {interval} candles received for {symbol}: "
                    f"total_candles={len(df)}, {details}"
                )

        # Store validated closed candles in centralized cache
        # Keep the source rows untouched for exact audit comparisons. Numeric
        # columns above are calculation-friendly conversions of these values.
        df.attrs["requested_limit"] = limit
        df.attrs["binance_raw_klines"] = tuple(raw_klines[index] for index in raw_indices)
        df.attrs["fetched_at_utc"] = now_utc.isoformat()
        df.attrs["cache_hit"] = False
        self.last_successful_fetch_at = now_utc.isoformat()
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
