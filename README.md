# Multi-Timeframe Pattern Signal Bot

Detects chart patterns (double/triple tops & bottoms, head & shoulders,
flags/pennants, wedges, triangles, rectangles, cup & handle) on Binance
USDT-M perpetual futures across 1D/4H/1H/15M, filters by EMA200 trend
direction, sends Telegram signals with a rendered chart, then tracks each
signal through TP1/TP2/TP3/SL to close, persisting everything to SQLite so
it survives restarts.

## 1. Setup

```bash
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env`:
- `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` — you said these are ready.
- `BINANCE_API_KEY` / `BINANCE_API_SECRET` — a **read-only** key is enough;
  the bot never places orders, only reads klines/prices.
- `SYMBOL_MODE=ALL` with `MAX_SYMBOLS=40` is the default — it scans the
  top 40 USDT-M perpetuals by 24h volume. Set `MAX_SYMBOLS=0` to scan the
  entire futures universe (300+ symbols), but expect a slower scan cycle
  and heavier API usage — raise it gradually once you've confirmed things
  work on your machine and network.

## 2. Run

```bash
python -m bot.main
```

On first run it creates `bot/data/trading.db` and starts:
- a **scan loop** (every `SCAN_INTERVAL_SEC`, default 60s) that pulls
  closed candles for every symbol/timeframe, runs pattern detection +
  the EMA200 filter, and sends a Telegram signal + chart on a fresh,
  confirmed breakout;
- a **tracker loop** (every `TRACKER_INTERVAL_SEC`, default 30s) that
  checks every open signal's OHLC since entry to see whether TP1/TP2/TP3
  or SL was touched, in chronological order;
- the **Telegram command handlers** (`/stats`, `/active`, `/history`,
  `/performance`) via long polling.

Stop with Ctrl+C — it shuts down the Telegram connection and Binance
client cleanly. On the next `python -m bot.main`, it reloads every signal
still `ACTIVE`/`TP1_HIT`/`TP2_HIT` from SQLite and resumes tracking them;
nothing is lost across restarts.

## 3. What I could not verify here

This sandbox has no network access, so I was not able to actually run
this against live Binance/Telegram — I've syntax-checked every file and
reasoned through the logic carefully, but you should treat the first
run as a test: start with a small `MAX_SYMBOLS` and a test Telegram
chat, watch a few scan/tracker cycles, and confirm the signal and
update messages look right before scaling up to the full symbol
universe or a channel your team relies on.

## 4. Design notes worth knowing

- **Breakout confirmation**: every pattern only fires if the *most
  recently closed* candle confirms the breakout (close beyond the
  level ± an ATR or % margin). This is what "confirmed/closed candle,
  no repaint" means in practice here — it will never signal on a
  still-forming candle.
- **TP1/TP2/TP3**: derived from the pattern's full measured-move target
  as fractions (`TP1_FRACTION`/`TP2_FRACTION`/`TP3_FRACTION` in
  `.env`, default 0.5x / 1.0x / 1.5x of the move). Tune these to taste.
- **Intra-candle SL/TP ordering**: OHLC data alone can't tell you
  whether price hit the target or the stop *first* within a single
  candle. If both are touched in the same candle, the tracker
  conservatively assumes the **stop was hit first** — this avoids
  inflating the win rate, but means a small number of real wins may be
  recorded as losses. If you later add access to lower-timeframe data
  or tick data for the ambiguous candles specifically, that's the
  place to make it exact.
- **PnL** is computed net of `TAKER_FEE_PCT` (round-trip, both sides)
  so displayed PnL is closer to what you'd actually realize.
- **Duplicate/overlap protection**: a DB unique index blocks the exact
  same (symbol, timeframe, pattern, timestamp) row twice, and the scan
  loop skips symbol/timeframe pairs that already have an active signal
  so you don't get two open signals stacked on the same pair.
- **Rate limits**: the scan loop sleeps briefly between requests and
  every Binance call is wrapped in retry-with-backoff (`tenacity`) for
  transient network errors.

## 5. Project layout

```
bot/
  config.py          settings, all overridable via .env
  models.py           Signal / PatternResult / enums
  database.py          async SQLite persistence
  binance_client.py    Binance Futures REST wrapper (universe + klines)
  indicators.py         EMA, ATR, pivot detection
  patterns.py            pattern detectors (ported from your Pine Script)
  signal_engine.py        EMA200 filter + TP1/2/3 construction
  chart_generator.py       mplfinance chart rendering
  telegram_bot.py           messages + /stats /active /history /performance
  tracker.py                 TP/SL monitoring state machine
  stats.py                    performance aggregation
  main.py                      orchestrator (scan loop + tracker loop + bot)
```

## 6. Extending scan coverage safely

If you want the full `MAX_SYMBOLS=0` universe from day one, the main
risk is Binance REST rate limits with 300+ symbols × 4 timeframes every
minute. A safe next step once this is stable: switch `binance_client.py`
from REST polling to Binance's combined WebSocket kline streams
(`<symbol>@kline_<interval>`) so you get pushed closes instead of
polling — the rest of the pipeline (pattern detection, EMA filter,
tracker, Telegram) doesn't need to change.
