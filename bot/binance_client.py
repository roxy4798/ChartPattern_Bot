"""
Thin async wrapper around python-binance's Futures REST endpoints.
Handles: symbol universe discovery, closed-candle OHLCV fetch, and
resilient retries (network hiccups shouldn't kill the whole scan loop).
"""
from __future__ import annotations
import logging
import numpy as np
import pandas as pd
from binance import AsyncClient
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from bot.config import settings

log = logging.getLogger("binance")

COLUMNS = ["open_time", "open", "high", "low", "close", "volume", "close_time",
           "quote_volume", "trades", "taker_buy_base", "taker_buy_quote", "ignore"]


class BinanceMarket:
    def __init__(self):
        self._client: AsyncClient | None = None

    async def connect(self):
        self._client = await AsyncClient.create(
            api_key=settings.binance_api_key or None,
            api_secret=settings.binance_api_secret or None,
            testnet=settings.use_testnet,
        )
        log.info("Connected to Binance Futures API (testnet=%s)", settings.use_testnet)

    async def close(self):
        if self._client:
            await self._client.close_connection()

    @retry(stop=stop_after_attempt(4), wait=wait_exponential(multiplier=1, min=1, max=20),
           retry=retry_if_exception_type(Exception), reraise=True)
    async def get_symbol_universe(self) -> list[str]:
        """Every TRADING, USDT-margined PERPETUAL contract, optionally capped
        by 24h quote volume via settings.max_symbols."""
        if settings.symbol_mode.upper() == "CUSTOM":
            return list(settings.custom_symbols)

        info = await self._client.futures_exchange_info()
        symbols = [
            s["symbol"] for s in info["symbols"]
            if s.get("contractType") == "PERPETUAL"
            and s.get("quoteAsset") == "USDT"
            and s.get("status") == "TRADING"
        ]
        if settings.max_symbols and settings.max_symbols > 0:
            tickers = await self._client.futures_ticker()
            vol_by_symbol = {t["symbol"]: float(t.get("quoteVolume", 0)) for t in tickers}
            symbols.sort(key=lambda s: vol_by_symbol.get(s, 0), reverse=True)
            symbols = symbols[: settings.max_symbols]
        return symbols

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10),
           retry=retry_if_exception_type(Exception), reraise=True)
    async def get_klines(self, symbol: str, interval: str, limit: int) -> pd.DataFrame:
        """Returns only CLOSED candles (drops the currently-forming last row)
        so pattern/breakout logic never repaints on partial data."""
        raw = await self._client.futures_klines(symbol=symbol, interval=interval,
                                                  limit=limit + 1)
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
        malformed = (
            df["open_time"].duplicated().any()
            or not df["open_time"].is_monotonic_increasing
            or not df["close_time"].is_monotonic_increasing
            or not np.isfinite(ohlc).all()
        )
        if malformed or (ohlc[:, 1, None] < ohlc[:, [0, 3]]).any() or (ohlc[:, 2, None] > ohlc[:, [0, 3]]).any():
            raise ValueError(f"Invalid or duplicated OHLC data returned for {symbol} {interval}")
        return df

    async def get_latest_closed_candle(self, symbol: str, interval: str) -> pd.Series | None:
        df = await self.get_klines(symbol, interval, limit=2)
        if df.empty:
            return None
        return df.iloc[-1]

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10),
           retry=retry_if_exception_type(Exception), reraise=True)
    async def get_mark_price(self, symbol: str) -> float | None:
        try:
            t = await self._client.futures_symbol_ticker(symbol=symbol)
            return float(t["price"])
        except Exception:
            log.exception("Failed to fetch mark price for %s", symbol)
            return None
