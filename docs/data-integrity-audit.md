# Candle and fractal integrity audit

## Current verification state

| Area | Result | Evidence and limits |
|---|---|---|
| USDⓈ-M Futures source | PASS | The application uses `python-binance` `futures_klines`, which calls `GET https://fapi.binance.com/fapi/v1/klines`; it filters `TRADING` `PERPETUAL` USDT contracts. No Spot kline fallback is present. |
| 1d raw/API parity | PASS (sample) | Independent raw REST and application SDK requests matched all 12 response fields for BTCUSDT, ETHUSDT, SEIUSDT, CYBERUSDT, XRPUSDT. Ten closed rows requested for comparison per pair. |
| 1w raw/API parity | PASS (sample) | Same sample and method as 1d. Weekly opens follow Monday 00:00 UTC in the checked rows. This does not verify every active symbol or the rendered chart. |
| 3d raw/API parity | PASS (6/6 symbols) | Independent REST and application SDK requests matched all 12 fields across BTCUSDT, SEIUSDT, CYBERUSDT, ETHUSDT, DOGEUSDT, XRPUSDT. Deterministic test fixture in `tests/fixtures/binance_usdm_klines_sample.json` now covers all three phases. |
| 3d live WebSocket parity | PASS (6/6 symbols) | Official USDⓈ-M stream `wss://fstream.binance.com/market/ws/<symbol>@kline_3d` frames matched REST open times (`k.t`), close times (`k.T`), open price (`k.o`), and exact 259,200,000 ms cadence across all 3 phases (BTC/ETH/DOGE/XRP offset 1, SEI offset 2, CYBER offset 0). |
| 3d chart-source parity | NOT VERIFIED | Desktop Chrome CDP network audit on `https://www.binance.com/en/futures/BTCUSDT` showed the web UI defaults to `continuousKlines` (`/fapi/v1/continuousKlines?contractType=PERPETUAL`) and continuous stream `btcusdt_perpetual@continuousKline_1d`, with default intervals `Time`, `1s`, `15m`, `1D`, `1W`. 3D contract candles are not exposed on the default UI toolbar. |
| Structural Fractal period | PASS (workspace code) | Configuration, signal, chart and structural detector require period 30; detector also requires 20 confirmation bars. `STRUCTURAL_MIN_SPAN` remains independent (65 in the saved fixture test). Production runtime configuration is not verified. |
| Production deployment | NOT VERIFIED | No VPS connection or runtime evidence was obtained. The read-only operator procedure below does not change the server. |

## Live comparison and cross-check method

The reproducible audit utility (`scratch/audit_3d_parity.py`) cross-checks:
1. Public GET `https://fapi.binance.com/fapi/v1/klines?symbol=<symbol>&interval=3d`.
2. Application `BinanceMarket.get_klines` through `python-binance` with production endpoints.
3. Official live USDⓈ-M WebSocket stream `wss://fstream.binance.com/market/ws/<symbol>@kline_3d`.
4. Desktop Chrome DevTools Protocol network audit on `https://www.binance.com/en/futures/BTCUSDT`.

For the audit captured 2026-10-11, three consecutive closed 3d opens (UTC milliseconds) and mathematical phase offsets are:

| Symbol | Last three closed opens (UTC ms) | Modulo 72h offset (days) |
|---|---|---|
| CYBERUSDT | 1791072000000, 1791331200000, 1791590400000 | 0 days (0 ms) |
| BTCUSDT | 1790899200000, 1791158400000, 1791417600000 | 1 day (86,400,000 ms) |
| ETHUSDT | 1790899200000, 1791158400000, 1791417600000 | 1 day (86,400,000 ms) |
| DOGEUSDT | 1790899200000, 1791158400000, 1791417600000 | 1 day (86,400,000 ms) |
| XRPUSDT | 1790899200000, 1791158400000, 1791417600000 | 1 day (86,400,000 ms) |
| SEIUSDT | 1790726400000, 1790985600000, 1791244800000 | 2 days (172,800,000 ms) |

Each series advances by exactly 259,200,000 ms. The live WebSocket contract stream `kline_3d` emits the exact same open timestamps and cadences for each symbol. This proves that the symbol-specific phases originate directly from Binance's internal market data engine and are consistent across REST and WebSocket.

The evidence is saved in [binance_3d_parity_evidence.json](evidence/binance_3d_parity_evidence.json) and [binance_3d_sample.json](evidence/binance_3d_sample.json).

The earlier reported 1,575 pair run compared the API response already used by the application with its parsed representation; it was not 1,575 independent pairs of requests, and it did not inspect chart UI or persisted production data. Do not describe it as an independent exchange parity run.

## Data and calculation behavior

- `BinanceMarket.get_klines` fetches `limit + 1`, filters by candle close time, returns up to the requested count, and retains original raw Binance rows in `DataFrame.attrs["binance_raw_klines"]` for exact audit comparisons. OHLC are floats for indicators and plotting; volume fields use `Decimal`.
- The scanner passes the same returned DataFrame first to `evaluate_symbol_timeframe` and then to `render_signal_chart` for a new signal. Tracker independently requests through the same `BinanceMarket` instance/cache and consumes closed OHLC rows; it does not recalculate fractals.
- The structural detector uses 30 preceding bars and 20 following bars including the candidate in its uniqueness comparison; a pivot is only emitted after those 20 subsequent closed rows are present. Chart and signal evaluation recalculate the same detector on the same scanner DataFrame.
- Tracker processes only candles with `close_time > signal_time`. The confirmation candle is already reflected in the persisted signal and must not be replayed as post-signal price action. A regression uses a real persisted `TP1_HIT` SQLite row, proves the signal candle cannot retroactively trigger SL/TP3, and verifies a later candle advances stored state to TP2. A failure-path test verifies a heartbeat does not fabricate a successful tracker update.
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
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD" "$RUN_PYTHON" - <<'PY'
from bot.config import settings
market = "UNKNOWN" if not isinstance(settings.use_testnet, bool) else ("TESTNET" if settings.use_testnet else "MAINNET")
print(f"BINANCE_ENV={market}")
print(f"STRUCTURAL_FRACTAL_PERIOD={settings.structural_fractal_period}")
print(f"STRUCTURAL_MIN_SPAN={settings.structural_min_span}")
print(f"TIMEFRAMES={','.join(settings.timeframes)}")
print(f"SYMBOL_MODE={settings.symbol_mode}")
print(f"DB_PATH={settings.db_path}")
PY
printf '\nDifferences in checkout from its current commit:\n'
git diff --stat HEAD
git diff --cached --stat
```

The latest code logs a startup fingerprint containing Git commit, clean/dirty marker, Binance mode, period, minimum span, and timeframes. It does not print dirty filenames or any credentials. On a currently running old version this record may not exist; in that case its effective fractal settings remain unverified. After a later controlled deployment, match the fingerprint timestamp to the process start time and confirm the commit equals the intended deployed commit.

```bash
grep -E 'Runtime fingerprint \|' bot/data/bot.log | tail -n 5
grep -E 'Connected to Binance Futures API \(testnet=' bot/data/bot.log | tail -n 5
```

Read the persisted fetch and cycle metadata without opening the database for writes. The URI is explicitly read-only and query-only; if SQLite cannot open the live WAL read-only, stop and report that metadata as unavailable rather than switching to a writable connection.

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD" "$RUN_PYTHON" - <<'PY'
import sqlite3
from pathlib import Path
from bot.config import settings

keys = (
    "binance_last_successful_fetch_at",
    "last_scan_finished_at", "last_successful_scan_at",
    "last_scan_pairs_checked", "last_scan_pair_failures",
    "last_tracker_heartbeat_at", "last_tracker_success_at",
    "last_tracker_failures", "tracker_active_count",
)
uri = Path(settings.db_path).resolve().as_uri() + "?mode=ro"
db = sqlite3.connect(uri, uri=True, timeout=3)
try:
    db.execute("PRAGMA query_only=ON")
    marks = ",".join("?" for _ in keys)
    rows = db.execute(
        f"SELECT key, value, updated_at FROM system_metadata WHERE key IN ({marks}) ORDER BY key",
        keys,
    ).fetchall()
    for key, value, updated_at in rows:
        print(f"{key}={value} (updated_at={updated_at})")
    if not rows:
        print("metadata_rows=none")
finally:
    db.close()
PY
```

The `last_tracker_heartbeat_at` timestamp proves only that the loop ran. Use `last_tracker_success_at` and `last_tracker_failures` separately; the same distinction applies to scanner finish versus successful scanner cycle. A missing or stale successful-fetch timestamp is not evidence of current Binance data.

Check clock synchronization without changing it:

```bash
timedatectl show -p NTPSynchronized -p NTP -p TimeUSec
python - <<'PY'
import json, time, urllib.request
before = time.time_ns() // 1_000_000
with urllib.request.urlopen('https://fapi.binance.com/fapi/v1/time', timeout=10) as response:
    server = json.load(response)['serverTime']
after = time.time_ns() // 1_000_000
midpoint = (before + after) // 2
print(f'host_midpoint_ms={midpoint}')
print(f'binance_server_ms={server}')
print(f'host_minus_binance_ms={midpoint-server}')
print(f'round_trip_ms={after-before}')
PY
```

This is a read-only spot check. Compare host/server skew with normal NTP tolerance and repeat if round trip is high. A single response does not prove long-term clock health.

## GitHub Actions authorization and result

The repository owner authorized the GitHub CLI `workflow` OAuth scope through the browser while signed in as `roxy4798`. `gh auth status --hostname github.com` showed the `workflow` scope, and `gh api user --jq .login` returned `roxy4798`. No alternate account or permission bypass was used.

The regression workflow is committed at `.github/workflows/tests.yml`. The source/test commit `6958805875e04761375f31dfdbe62f4e23980a2c` passed both Actions triggers. The later documentation commit `fad95ca78c8c2a08a0a03cc6cd7de30b089d544f` also passed both triggers:

- Push run: https://github.com/roxy4798/ChartPattern_Bot/actions/runs/38122299235 (36 seconds).
- Pull request run: https://github.com/roxy4798/ChartPattern_Bot/actions/runs/38122302267 (26 seconds).

CI found two issues before passing: `colorama` was missing from runtime requirements, and timestamp validation assumed nanosecond storage although current pandas preserves millisecond resolution for Binance timestamps. Both defects were fixed and the tests normalize explicitly to milliseconds. These linked runs are historical commit-specific evidence; always verify that an Actions `headSha` equals the current remote PR head before treating CI as current.

## Rollout gate

Keep production on the existing version until (1) 3d candle opens and OHLCV are matched to the official Futures chart for several symbols with different observed phases and matching timezone, (2) production process/configuration and NTP synchronization are verified, and (3) a controlled rollback and post-deploy data-health check are prepared. PR CI passes, but that does not remove the remaining chart-parity and production-verification gates. No deployment or PR merge is authorized by this document.
