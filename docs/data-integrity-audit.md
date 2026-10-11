# Candle and fractal integrity audit

## Current verification state

| Area | Result | Evidence and limits |
|---|---|---|
| USDⓈ-M Futures source | PASS | The application uses `python-binance` `futures_klines`, which calls `GET https://fapi.binance.com/fapi/v1/klines`; it filters `TRADING` `PERPETUAL` USDT contracts. No Spot kline fallback is present. |
| 1d raw/API parity | PASS (sample) | Independent raw REST and application SDK requests matched all 12 response fields for BTCUSDT, ETHUSDT, SEIUSDT, CYBERUSDT, XRPUSDT. Ten closed rows requested for comparison per pair. |
| 1w raw/API parity | PASS (sample) | Same sample and method as 1d. Weekly opens follow Monday 00:00 UTC in the checked rows. This does not verify every active symbol or the rendered chart. |
| 3d raw/API parity | PASS (sample); chart boundary parity NOT VERIFIED | The same independent sample matched raw closed rows, but observed three-day opens have different phases across symbols. The Futures chart was not independently inspected at matching symbol, interval, and timezone; official documentation found does not explain this phase. Release remains blocked on this item. |
| Structural Fractal period | PASS (workspace code) | Configuration, signal, chart and structural detector require period 30; detector also requires 20 confirmation bars. `STRUCTURAL_MIN_SPAN` remains independent (65 in the saved fixture test). Production runtime configuration is not verified. |
| Production deployment | NOT VERIFIED | No VPS connection or runtime evidence was obtained. The read-only operator procedure below does not change the server. |

## Live comparison method

The 15-pair sample (five USDT-M perpetuals × three intervals) used two independent public GET routes: Python `urllib` against `https://fapi.binance.com/fapi/v1/klines` and the application's `BinanceMarket.get_klines` through `python-binance`. The fixture side was filtered using `https://fapi.binance.com/fapi/v1/time` so both sides compared the same closed rows. Each compared raw row was the exact 12-field tuple, including exact decimal strings, timestamps, trade count, taker volumes, and ignore field. Result: 15/15 exact. Requests were sequential and rate-limited with a short pause; no credentials or trading endpoints were used.

For the latest sampled 3d rows, three consecutive opens (UTC milliseconds) were:

| Symbol | Last three opens (UTC ms) |
|---|---|
| BTCUSDT | 1790899200000, 1791158400000, 1791417600000 |
| SEIUSDT | 1790726400000, 1790985600000, 1791244800000 |
| CYBERUSDT | 1790812800000, 1791072000000, 1791331200000 |

Each series advances by exactly 259,200,000 ms, but the phases differ. This proves the API returns symbol-dependent phases in the sampled window. It does not establish the cause or prove that the Binance Futures chart uses those same boundaries. Do not deploy a claim of full 3d parity on this evidence.

The earlier reported 1,575 pair run compared the API response already used by the application with its parsed representation; it was not 1,575 independent pairs of requests, and it did not inspect chart UI or persisted production data. Do not describe it as an independent exchange parity run.

## Data and calculation behavior

- `BinanceMarket.get_klines` fetches `limit + 1`, filters by candle close time, returns up to the requested count, and retains original raw Binance rows in `DataFrame.attrs["binance_raw_klines"]` for exact audit comparisons. OHLC are floats for indicators and plotting; volume fields use `Decimal`.
- The scanner passes the same returned DataFrame first to `evaluate_symbol_timeframe` and then to `render_signal_chart` for a new signal. Tracker independently requests through the same `BinanceMarket` instance/cache and consumes closed OHLC rows; it does not recalculate fractals.
- The structural detector uses 30 preceding bars and 20 following bars including the candidate in its uniqueness comparison; a pivot is only emitted after those 20 subsequent closed rows are present. Chart and signal evaluation recalculate the same detector on the same scanner DataFrame.
- Scan heartbeat and fully successful scan metadata are now distinct. Tracker heartbeat and successful update metadata are distinct. Binance request counters are persisted by the scanner and shown by `/health`; these counters are process-local since startup.
- Closed-candle eligibility currently uses the host UTC clock. The live comparison used Binance server time and agreed, but production host clock synchronization has not been measured.

## Read-only production inspection (MobaXterm)

Run from an SSH shell. These commands inspect the process, checkout and effective non-secret settings only. They do not stop/restart services, write files, alter the database or contact trading endpoints.

```bash
cd /home/bot/bots/ChartPattern_Bot || exit 1
printf 'checkout=%s\n' "$PWD"
printf 'commit='; git rev-parse HEAD
printf 'working_tree='; if git diff --quiet && git diff --cached --quiet; then echo clean; else echo has_changes; fi
git status --short
printf '\nPython processes rooted at this checkout:\n'
for proc in /proc/[0-9]*; do
  pid=${proc##*/}
  cwd=$(readlink "$proc/cwd" 2>/dev/null || true)
  exe=$(readlink "$proc/exe" 2>/dev/null || true)
  comm=$(cat "$proc/comm" 2>/dev/null || true)
  case "$comm" in
    python*)
      if [ "$cwd" = "$PWD" ]; then
        ps -p "$pid" -o pid=,comm=,lstart=
        printf '  cwd=%s\n  executable=%s\n' "$cwd" "$exe"
      fi
      ;;
  esac
done
printf '\nEffective safe configuration fields:\n'
RUN_PYTHON=python
if [ -x .venv/bin/python ]; then RUN_PYTHON=.venv/bin/python; fi
if [ -x venv/bin/python ]; then RUN_PYTHON=venv/bin/python; fi
PYTHONPATH="$PWD" "$RUN_PYTHON" - <<'PY'
from bot.config import settings
market = "UNKNOWN" if not isinstance(settings.use_testnet, bool) else ("TESTNET" if settings.use_testnet else "MAINNET")
print(f"BINANCE_ENV={market}")
print(f"STRUCTURAL_FRACTAL_PERIOD={settings.structural_fractal_period}")
print(f"STRUCTURAL_MIN_SPAN={settings.structural_min_span}")
print(f"TIMEFRAMES={','.join(settings.timeframes)}")
print(f"SYMBOL_MODE={settings.symbol_mode}")
PY
printf '\nDifferences in checkout from its current commit:\n'
git diff --stat HEAD
git diff --cached --stat
```

The running process working directory/executable and checkout commit do not prove that its already-imported Python modules equal current disk files. A process-level source fingerprint needs a deployment-specific build/version value recorded at process start; the current app has no such fingerprint. The effective configuration command starts a separate interpreter and reads its configuration from the checkout environment, not the already-running process. To verify that process exactly, add a safe startup diagnostic (commit hash, code version and only the three listed non-secret settings) and inspect its existing logs after a later controlled deployment. Do not expose the environment file or dump process command lines.

## Rollout gate

Keep production on the existing version until (1) 3d candle opens and OHLCV are matched to the official Futures chart for several symbols with different observed phases and matching timezone, (2) the production process/config/version are verified, (3) CI passes on the PR branch, and (4) a controlled rollback and post-deploy data-health check are prepared. No deployment or PR merge is authorized by this document.
