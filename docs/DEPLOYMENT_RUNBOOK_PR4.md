# ChartPattern_Bot — Production Deployment Runbook (PR #4)

> **Document Type**: Production Deployment & Rollback Runbook (Final Safety Revision)<br>
> **Target Release Commit**: `c1b5ec410c67697b3003a17ff2f473ec77d52d59` (Merged `main` / PR #4)<br>
> **Previous Production Baseline Commit**: `2ed495935171b30fc3b9b78b76b7491a8b7ac291`<br>
> **Target Environment**: Alibaba Cloud VPS, Ubuntu Linux, Binance Futures MAINNET<br>
> **Target Session**: `tmux` session `chartbot`<br>
> **Protected Session**: `tmux` session `EMA-50-200` (**CRITICAL: NEVER touch, modify, or signal**)<br>

> [!IMPORTANT]
> **CRITICAL ENVIRONMENT NOTICE**:<br>
> The Antigravity IDE and assistant have **NO direct network connection or SSH access** to the Alibaba Cloud VPS.<br>
> **None** of the server conditions, paths, PIDs, or states have been verified remotely by the assistant.<br>
> All commands and inspection steps in this document are **instructions for the human operator** to execute manually in MobaXterm.<br>
> The operator **must manually verify and record each actual value on the server** before proceeding. Do not assume or guess server values.

---

## 1. Executive Summary & Change Scope

### 1.1 Root-Cause & Scope Summary
At rollback baseline commit `2ed495935171b30fc3b9b78b76b7491a8b7ac291`, the candle validator rejected non-contiguous candle sequences for all intervals, causing symbols with ancient historical 3D cadence gaps to be completely dropped even if recent history was contiguous. Additionally, signal validation rejected invalid price values without isolating the offending fields or enforcing strict positive finite price ordering. These are code-level behaviors; this runbook does not claim to have verified production symbols, events, logs, or database state on the VPS.

This release is strictly for **ChartPattern_Bot**, whose core trading strategy remains chart pattern recognition based on structural fractals with period **30** (`STRUCTURAL_FRACTAL_PERIOD=30`, `STRUCTURAL_MIN_SPAN=65`). PR #4 is an infrastructure bugfix for candle data validation and signal price sanity checking. It does **NOT** alter the fractal period, trading timeframes (`1d`, `3d`, `1w`), pattern detection logic, or trading parameters. The EMA strategy belongs to a separate protected bot (`EMA-50-200`) and must not be conflated with ChartPattern_Bot.

### 1.2 Remediations Delivered in PR #4 (`c1b5ec410c67697b3003a17ff2f473ec77d52d59`)

#### A. 3D Candle Discontinuity Recovery (`bot/binance_client.py:369-405`)
- **Cadence & Gap Detection**: The bot computes open-time differences `diffs = np.diff(open_ms)` using normalized `int64` millisecond timestamps and checks against `interval_ms = 259_200_000` (3 days).
- **Contiguous Suffix Determination**: When cadence gaps exist (`(diffs != interval_ms).any()`), the bot locates gap indices via `gap_indices = np.where(diffs != interval_ms)[0]`, finds the latest gap index `last_gap_idx = gap_indices[-1]`, and identifies the starting point of the contiguous tail: `contiguous_start = last_gap_idx + 1` and `contiguous_len = len(df) - contiguous_start`.
- **Sufficiency Threshold (220 Bars)**: The code enforces `min_contiguous_bars = 220`. This threshold is an implementation data-sufficiency guard for internal indicator calculations (such as internal warmup arrays before fractal confirmation). It is **not** a modification of the fractal period 30 strategy, nor does it convert this bot to an EMA strategy.
- **Tail Suffix Retention**: If `contiguous_len >= 220`, the bot discards disjoint historical bars prior to the latest gap (`df = df.iloc[contiguous_start:].reset_index(drop=True)`), attaches metadata attribute `df.attrs["is_contiguous_boundary_trimmed"] = True`, emits a structured neutral log (`[<SYMBOL> 3d] 3D candle discontinuity detected at historical index ...; discarding ... disjoint historical bars and retaining ... strictly contiguous bars`), and allows scanner evaluation to proceed.
- **Fail-Closed Behavior**: If `contiguous_len < 220`, the validator fails closed by raising `ValueError: Misaligned or missing 3d candles received for <symbol>...`, increments `_candle_metrics["validation_failures"]`, and drops the pair for the current scan cycle.
- **Recovery Boundary**: Suffix recovery does **not** guarantee that all symbols will recover. Symbols with less than 220 unbroken contiguous bars or ongoing cadence irregularities will continue to fail closed.

#### B. Strict 1D & 1W Candle Validation (`bot/binance_client.py:406-430`)
- **No Suffix Recovery**: The contiguous suffix recovery mechanism is strictly scoped to `3d`. Suffix recovery is never applied to `1d` or `1w`.
- **Strict Cadence**: Open-time delta must equal exact interval duration (`86_400_000` ms for `1d`, `604_800_000` ms for `1w`). Any missing bar triggers validation failure.
- **Phase Alignment**: Open times must satisfy epoch phase alignment:
  - `1d`: `open_ms % 86_400_000 == 0` (aligned to 00:00:00 UTC daily).
  - `1w`: `(open_ms - 345_600_000) % 604_800_000 == 0` (aligned to Thursday 00:00:00 UTC epoch offset, corresponding to Binance weekly kline opening at Monday 00:00:00 UTC).
- **Close Time Alignment**: Close times must strictly equal `open_ms + interval_ms - 1`. Any timestamp discrepancy causes immediate fail-closed rejection.

#### C. Cache Boundary Protection (`bot/binance_client.py:112-118`)
- In `CandleCache.get`, cached dataframes with `attrs["is_contiguous_boundary_trimmed"] = True` are only served if the cached limit meets or exceeds the requested limit (`cached_limit >= limit`). If a higher bar limit is requested later, the cache triggers a clean REST API fetch to prevent truncated data reuse.

#### D. Multi-Layer Signal Price Validation (`bot/patterns.py` & `bot/signal_engine.py`)
- **Early-Stage Pattern Guard (`bot/patterns.py:199-212`)**:
  - `_rr_ok` validates that `entry`, `stop`, and `target` are finite via `np.isfinite()`.
  - For bullish patterns (`is_bull=True`), it strictly requires positive, properly ordered prices: `0 < stop < entry < target`.
  - Non-bullish patterns immediately return `False` because the structural pattern detectors in `bot/patterns.py` are strictly **LONG ONLY**.
  - Enforces fee-adjusted risk/reward: `risk = entry - stop`, `reward = (target - entry) - (entry * settings.taker_fee_pct * 2)`. Requires `risk > 0`, `reward > 0`, and `(reward / risk) >= settings.min_rr`.
- **Engine-Stage Signal Guard (`bot/signal_engine.py:18-59`)**:
  - `_validate_signal()` checks all price fields (`entry_price`, `stop_loss`, `tp1`, `tp2`, `tp3`, `price_at_signal`) for finite and positive values (`np.isfinite(val) and val > 0`).
  - Enforces strict direction-specific price ordering:
    - `LONG`: `stop_loss < entry_price < tp1 < tp2 < tp3`.
    - `SHORT`: `stop_loss > entry_price > tp1 > tp2 > tp3`.
  - Verifies signal pattern matches detected pattern (`signal.pattern == pattern.name`) and direction matches pattern bias (`signal.direction == expected_dir`).
  - Diagnostic Logging: If any price is invalid or misordered, raises `ValueError` with explicit field names and formatted price context (`[context: entry_price=..., stop_loss=..., tp1=...]`).
- **Scanner Fail-Closed Handling (`bot/main.py:152-162`)**:
  - Any `ValueError` raised during candle validation or signal price validation is caught by the scanner task exception handler, increments `pair_failures += 1`, logs a warning with full stack trace (`exc_info=True`), and drops the pair without storing signals or emitting alerts.

### 1.3 Boundary of PR #4 Claims & Strategy Invariance
To prevent operational misunderstandings, the following boundaries are explicitly affirmed:
1. **No Trading Strategy Changes**: The structural fractal period 30 strategy, pivot lookbacks, trendline fitting, and confirmation rules are entirely untouched.
2. **No Profitability Claims**: PR #4 makes no claim or guarantee of improved trading performance, win rate, or profitability.
3. **No Guarantee of Zero Pair Failures**: The fix resolves validation crashes for symbols with sufficient 3D contiguous suffixes (>= 220 bars). It does not guarantee that all Binance symbols will scan successfully, nor that scanner pair failures will drop to zero. Symbols with ongoing API errors, illiquidity, or suffixes < 220 bars will continue to be dropped.
4. **CI vs. VPS Boundary**: Successful local test execution (42 passed) and GitHub Actions run [`tests #21`](https://github.com/roxy4798/ChartPattern_Bot/actions/runs/38148330891) prove repository unit/regression test health only. They do **not** verify VPS runtime health, existing process state, or database integrity.

---

## 2. Pre-Deployment Discovery & Read-Only Verification Checklist

> [!WARNING]
> All steps in this section are **strictly read-only**. Do not execute write operations, process termination, package installations, file permission modifications, or git checkouts in this section.

### 2.1 Operator Discovery Variables
The operator must inspect the VPS and manually record each variable below before continuing:

Shell variables are session-local and may disappear between MobaXterm shells or reconnects. Record actual non-secret values and evidence in a private operator note; after reconnect, reread and reverify them. Never paste credentials into the note or terminal output. Any missing/ambiguous critical value is a STOP condition.

| Variable | Description | How to Verify on VPS | Operator Recorded Value |
| :--- | :--- | :--- | :--- |
| `VPS_APP_DIR` | Absolute path to repo root | `tmux` pane path or `pwdx <PANE_PID>` | `[FILL BY OPERATOR]` |
| `VPS_VENV_PATH` | Absolute path to Python venv | `${VPS_APP_DIR}/venv` | `[FILL BY OPERATOR]` |
| `CHARTBOT_PANE_ID` | Tmux pane ID of chartbot | `tmux list-panes -t chartbot` | `[FILL BY OPERATOR]` |
| `CHARTBOT_PANE_PID` | Shell process PID inside pane | `tmux list-panes -t chartbot -F "#{pane_pid}"` | `[FILL BY OPERATOR]` |
| `CHARTBOT_BOT_PID` | Exact Python process PID of bot | Child of `CHARTBOT_PANE_PID` | `[FILL BY OPERATOR]` |
| `EFFECTIVE_DB_PATH` | Database actually open in the verified bot process | Exact descriptor target plus verified cwd/config evidence; if absent or ambiguous, STOP | `[FILL BY OPERATOR]` |
| `EFFECTIVE_LOG_PATH` | Log file actually open in the verified bot process | Exact descriptor target; if absent or ambiguous, STOP | `[FILL BY OPERATOR]` |
| `EFFECTIVE_TESTNET` | Binance mode loaded by the running bot | Runtime fingerprint emitted after this PID started, corroborated with startup configuration; otherwise STOP | `[FILL BY OPERATOR]` |

---

### 2.2 Read-Only Verification Commands (MobaXterm)

#### Step 2.2.1: Verify Tmux Sessions & Isolate `chartbot`
```bash
# 1. List all active tmux sessions
tmux ls

# EXPECTED OUTPUT:
# chartbot: ...
# EMA-50-200: ...
# CRITICAL RULE: Confirm that 'EMA-50-200' is present, but DO NOT ATTACH OR SEND COMMANDS TO IT.
# Under NO circumstances should any command target the 'EMA-50-200' session.

# 2. Inspect the panes strictly in the 'chartbot' session:
tmux list-panes -t chartbot -F "PaneID:#{pane_id} PanePID:#{pane_pid} Path:#{pane_current_path} Cmd:#{pane_current_command}"

# Record CHARTBOT_PANE_ID (e.g., %0), CHARTBOT_PANE_PID (e.g., 14201), and VPS_APP_DIR.
```

#### Step 2.2.2: Verify Strict Process Isolation Under `chartbot`
```bash
# 1. Inspect child processes running strictly under CHARTBOT_PANE_PID:
ps -o pid,ppid,stat,user,args --ppid "<CHARTBOT_PANE_PID>"

# 2. Verify working directory of the discovered child process:
# pwdx <CHILD_PID>

# STRICT STOP RULES:
# - The process command line must be running Python executing 'bot.main' or 'main.py'.
# - The working directory (pwdx) must match <VPS_APP_DIR>.
# - Its parent PID (PPID) must strictly be <CHARTBOT_PANE_PID>.
# - There must be EXACTLY ONE active bot process under this pane.
# - Record its PID as <CHARTBOT_BOT_PID>.
#
# IF NO PROCESS IS RUNNING, OR IF MORE THAN ONE CANDIDATE PROCESS IS FOUND,
# OR IF PROCESS IDENTITY IS UNCERTAIN:
# STOP IMMEDIATELY! Attach interactively to chartbot (tmux attach -t chartbot) to inspect manually.
# DO NOT send any kill signal. NEVER use generic pgrep or pkill!
```

#### Step 2.2.3: Read-Only Git State & Commit Verification
```bash
cd "<VPS_APP_DIR>"

# 1. Verify current production commit SHA
CURRENT_SHA=$(git rev-parse HEAD)
echo "Current production SHA: ${CURRENT_SHA}"

# STOP RULE: If CURRENT_SHA is NOT 2ed495935171b30fc3b9b78b76b7491a8b7ac291:
# STOP immediately. Report discrepancy to lead engineer. Do not deploy over an unexpected commit.

# 2. Check for unstaged modifications on tracked files:
git diff --quiet
if [ $? -ne 0 ]; then
    echo "ERROR: Unstaged modifications found in tracked files! Run 'git diff' to inspect."
    echo "STOP immediately. Do NOT proceed with deployment."
    exit 1
fi

# 3. Check for staged modifications on tracked files:
git diff --cached --quiet
if [ $? -ne 0 ]; then
    echo "ERROR: Staged modifications found! Run 'git diff --cached' to inspect."
    echo "STOP immediately. Do NOT proceed with deployment."
    exit 1
fi

# 4. Check working tree status:
git status --porcelain

# STOP RULES FOR WORKING TREE:
# - Tracked files must be 100% clean (no M, D, or A statuses).
# - Untracked research files (starting with ??) such as scratch/empirical_fractal_research.py
#   and scratch/rolling_fractal_scan.py are expected research files.
# - DO NOT delete untracked research files.
# - DO NOT run 'git reset --hard' or 'git clean' under any circumstances!
```

#### Step 2.2.4: Effective Path & Network Mode Verification (Hierarchy-Aware, No Secret Leakage)

##### A. Binance Network Mode Check (Fail Closed)
In `bot/config.py`, `load_dotenv(override=False)` runs before `Settings` reads `BINANCE_TESTNET`. An already present Python process environment value wins; otherwise dotenv may populate it; if it remains unset, the code default is `False`. `python-dotenv` also disables loading when `PYTHON_DOTENV_DISABLED` has a recognized truthy value. Since an unknown value for this control could make source precedence unclear, accept only an absent variable or an explicitly recognized false value (`0`, `false`, `f`, `no`, `n`, `off`, case-insensitive; no surrounding whitespace) and STOP for empty, unknown, unreadable, or truthy values. For `BINANCE_TESTNET`, accept only normalized `1`, `true`, `yes`, or `on` as Testnet, and `0`, `false`, `no`, or `off` as Mainnet. Matching is case-insensitive and surrounding whitespace is stripped. An absent variable, explicitly empty value, unknown value, and read failure are distinct states; empty and unknown values are invalid and MUST NOT be treated as false even though the application's permissive `_bool` implementation defaults non-truthy strings to false. Missing may use the code default only when both higher-priority sources are proven unset and dotenv loading is not disabled.

The current `.env` is not proof of what a running process loaded: it may have changed since startup. `/proc/<PID>/environ` reflects the initial process environment and may not include values subsequently loaded by `load_dotenv`; absence there does not prove the variable was unset when settings were constructed. Do not dump the process environment or print credentials.

```bash
cd "<VPS_APP_DIR>" || { echo "STOP: cannot enter verified application directory"; exit 1; }

# Parse and print only BINANCE_TESTNET (never any credential). This uses the
# python-dotenv parser used by the application; it rejects duplicate/malformed
# keys and interpolated values as ambiguous. If this parser/import is unavailable,
# inspect only this key manually in a trusted editor and STOP if ambiguous.
PYTHONDONTWRITEBYTECODE=1 "${VPS_VENV_PATH}/bin/python" - "${CHARTBOT_BOT_PID}" <<'PY'
import os, sys
from dotenv.parser import parse_stream

key = b"BINANCE_TESTNET"
disabled_key = b"PYTHON_DOTENV_DISABLED"
try:
    raw_env = open(f"/proc/{sys.argv[1]}/environ", "rb").read()
    proc = [part.partition(b"=")[2] for part in raw_env.split(b"\0") if part.partition(b"=")[0] == key]
    disabled = [part.partition(b"=")[2] for part in raw_env.split(b"\0") if part.partition(b"=")[0] == disabled_key]
except Exception:
    print("PROCESS_ENV=READ_ERROR")
    sys.exit(2)
if len(proc) > 1:
    print("PROCESS_ENV=DUPLICATE; STOP")
    sys.exit(2)
if len(disabled) > 1:
    print("PYTHON_DOTENV_DISABLED=DUPLICATE; STOP")
    sys.exit(2)
if disabled:
    flag = disabled[0].decode("utf-8", "strict")
    if flag.casefold() not in {"0", "false", "f", "no", "n", "off"}:
        print("PYTHON_DOTENV_DISABLED=SET_OR_UNKNOWN; STOP")
        sys.exit(2)
    print("PYTHON_DOTENV_DISABLED=EXPLICIT_FALSE")
else:
    print("PYTHON_DOTENV_DISABLED=ABSENT")
proc_value = proc[0].decode("utf-8", "strict") if proc else None
print("PROCESS_ENV=" + ("ABSENT" if proc_value is None else "EMPTY" if proc_value == "" else f"VALUE:{proc_value}"))

try:
    config_dir = os.path.dirname(os.path.abspath("bot/config.py"))
    dotenv_path = None
    while True:
        candidate = os.path.join(config_dir, ".env")
        if os.path.lexists(candidate):
            dotenv_path = candidate
            break
        parent = os.path.dirname(config_dir)
        if parent == config_dir:
            break
        config_dir = parent
    if dotenv_path is None:
        dotenv_value, dotenv_state = None, "ABSENT"
    else:
        with open(dotenv_path, encoding="utf-8") as stream:
            bindings = list(parse_stream(stream))
        if any(binding.error for binding in bindings):
            raise ValueError("dotenv syntax error")
        matches = [binding for binding in bindings if binding.key == "BINANCE_TESTNET"]
        if len(matches) > 1:
            raise ValueError("duplicate BINANCE_TESTNET")
        if not matches:
            dotenv_value, dotenv_state = None, "ABSENT"
        else:
            dotenv_value = matches[0].value
            dotenv_state = "BARE" if dotenv_value is None else "EMPTY" if dotenv_value == "" else "VALUE"
except Exception:
    print("DOTENV=READ_OR_PARSE_ERROR; STOP")
    sys.exit(2)
print("DOTENV=" + (dotenv_state if dotenv_state != "VALUE" else f"VALUE:{dotenv_value}"))

raw = proc_value if proc_value is not None else dotenv_value
if raw is None and dotenv_state == "ABSENT":
    print("CANDIDATE_EFFECTIVE=MAINNET (code default; both sources absent)")
    sys.exit(0)
if raw is None or "$" in raw:
    print("CANDIDATE_EFFECTIVE=UNKNOWN (bare/empty/interpolated); STOP")
    sys.exit(2)
normalized = raw.strip().lower()
if normalized in {"1", "true", "yes", "on"}:
    mode = "TESTNET"
elif normalized in {"0", "false", "no", "off"}:
    mode = "MAINNET"
else:
    print("CANDIDATE_EFFECTIVE=UNKNOWN; STOP")
    sys.exit(2)
print("CANDIDATE_EFFECTIVE=" + mode)
if mode != "MAINNET":
    print("STOP: production candidate must be MAINNET")
    sys.exit(2)
PY
if [ $? -ne 0 ]; then echo "STOP: BINANCE_TESTNET could not be resolved safely"; exit 1; fi

# Tie a startup record to this exact active PID. A stale or ambiguous log line
# cannot prove this process's effective network mode.
ps -p "${CHARTBOT_BOT_PID}" -o pid,lstart,args
FINGERPRINT_LINES=$(grep -F "Runtime fingerprint" "<verified log path>") || { echo "STOP: no readable runtime fingerprint"; exit 1; }
[ -n "${FINGERPRINT_LINES}" ] || { echo "STOP: runtime fingerprint is empty"; exit 1; }
printf '%s\n' "${FINGERPRINT_LINES}"
case "${FINGERPRINT_LINES}" in
    *"Binance=MAINNET"*) ;;
    *) echo "STOP: no MAINNET fingerprint is present"; exit 1 ;;
esac
# Manually identify exactly one fingerprint emitted after this PID's start.
# If absent, stale, or multiple/ambiguous, STOP.
```

Record the process-environment value only if positively known, the `.env` value at inspection, the code default only if both sources are proven unset, and the active process startup fingerprint separately. An empty shell variable does not establish “unset”. The fingerprint's Binance field is evidence only when its timestamp follows this exact PID's start. If reading configuration fails, precedence/value is ambiguous, any value is unknown, or the active process cannot be proven `Binance=MAINNET`, **STOP before backup, checkout, or restart**. Never change configuration automatically. Current `.env` alone is not sufficient evidence.

##### B. Effective Database & Log Path Verification
Do not infer the live database from a conventional filename. `DB_PATH` and `LOG_PATH` may be absolute or relative to the process working directory. Verify exact cwd with `pwdx <CHARTBOT_BOT_PID>`. Inspect only the `DB_PATH` and `LOG_PATH` entries in `.env` with a reliable dotenv parser that reports duplicates, parse/read failures, absent keys, and empty values without printing other settings; if unavailable, inspect these entries manually and STOP if syntax or precedence is ambiguous. Do not use `grep/cut/tr` for config parsing and do not assume repository defaults until both sources are proven unset. `/proc/<PID>/environ` cannot prove dotenv-loaded values or that the current `.env` matches startup configuration.

Resolve every descriptor to its actual filesystem target without filtering by a guessed name:
```bash
FD_DIR="/proc/${CHARTBOT_BOT_PID}/fd"
test -d "${FD_DIR}" || { echo "STOP: cannot inspect process descriptors"; exit 1; }
for fd in "${FD_DIR}"/*; do
    target=$(readlink -- "${fd}") || { printf '%s -> UNRESOLVED\n' "${fd##*/}"; continue; }
    case "${target}" in
        /*)
            case "${target}" in *" (deleted)") printf '%s -> %s (deleted; candidate is not usable)\n' "${fd##*/}" "${target}"; continue ;; esac
            resolved=$(realpath -e -- "${target}") || { printf '%s -> %s (unresolved)\n' "${fd##*/}" "${target}"; continue; }
            printf '%s -> %s\n' "${fd##*/}" "${resolved}"
            ;;
        *) printf '%s -> %s (non-filesystem descriptor)\n' "${fd##*/}" "${target}" ;;
    esac
done
```

The inventory may include pipes, sockets, and other non-filesystem descriptors; these are informational. The operator must identify the SQLite and Python log descriptors, resolve those exact filesystem targets, match them to manually reviewed configuration against the verified process cwd, and record exact absolute paths. If a DB/log candidate is missing, ambiguous, unresolved, deleted, cwd-mismatched, unreadable, or differs from config, **STOP**. A configured path is not proof the process uses it; a descriptor path is not proof it matches the startup setting. Require both to agree. Confirm the matched DB exists before backup; never substitute a guessed path. The log path is verified only by an exact matching open descriptor. Inspect `.env` metadata without displaying its contents: `stat -c '%U %G %a %n' .env`.

#### Step 2.2.5: Read-Only Disk Space & SQLite Integrity Check
```bash
# 1. Verify available disk space (require >= 2 GB free for backup):
df -h "<VPS_APP_DIR>"

# 2. Verify active database file and WAL presence:
: "${EFFECTIVE_DB_PATH:?STOP: exact descriptor-verified DB path is required}"
: "${EFFECTIVE_LOG_PATH:?STOP: exact descriptor-verified log path is required}"
test -f "${EFFECTIVE_DB_PATH}" && [ ! -L "${EFFECTIVE_DB_PATH}" ] || { echo "STOP: exact active database is not a regular file"; exit 1; }
ls -lh "${EFFECTIVE_DB_PATH}"*

# 3. Read-only SQLite integrity check on live database:
LIVE_CHECK=$(sqlite3 -readonly "${EFFECTIVE_DB_PATH}" "PRAGMA integrity_check;") || { echo "STOP: live DB integrity command failed; state is unknown"; exit 1; }
if [ "${LIVE_CHECK}" != "ok" ]; then
    echo "STOP: live DB integrity result is empty, ambiguous, or not exactly ok. Do not deploy."
    exit 1
fi
printf 'Live DB integrity: %s\n' "${LIVE_CHECK}"

# Technical Note on SQLite Integrity vs. Foreign Keys:
# Per SQLite documentation (sqlite.org/pragma.html#pragma_integrity_check), PRAGMA integrity_check
# validates database formatting, page linkages, freelists, and index/table consistency, but does NOT
# verify foreign key constraint violations (which requires PRAGMA foreign_key_check).
# Evaluation of the actual application database schema (bot/database.py:23-92) confirms that none
# of the active tables (signals, notification_outbox, system_metadata, square_posts) define
# FOREIGN KEY constraints. Consequently, foreign_key_check enforces no schema rules here, and
# PRAGMA integrity_check returning exactly 'ok' provides the required database structural guarantee.
# Never claim foreign keys have been verified if PRAGMA foreign_key_check is not explicitly run.
```

---

## 3. Safe Pre-Deployment Backup Procedure (Outside Repository)

> [!CRITICAL]
> **SQLite WAL Consistency**: `bot/database.py:19` sets `PRAGMA journal_mode = WAL`.
> Simply copying the database file with `cp` can result in a corrupted or incomplete snapshot because active transactions reside in its `-wal` sidecar.
> **Backup Location & Collision Safety**: Backups must reside in a private directory **outside** the repository tree. Never reuse or delete an existing backup directory. If a directory with the same name exists, creation must fail immediately. Never overwrite verified backup snapshots with raw file copies. Every command, permission check, and backup validation below is a mandatory gate.

### Step 3.1: Initialize Private Backup Directory (Collision-Protected)
```bash
umask 077
: "${HOME:?STOP: HOME is unavailable}"
BACKUPS_BASE="${HOME}/.chartbot_backups"
VPS_APP_REAL=$(realpath -e -- "${VPS_APP_DIR}") || { echo "STOP: cannot resolve application root"; exit 1; }
BACKUPS_BASE=$(realpath -m -- "${BACKUPS_BASE}") || { echo "STOP: cannot resolve backup base"; exit 1; }
case "${BACKUPS_BASE}/" in
    "${VPS_APP_REAL}/"*) echo "STOP: backup directory would be inside the repository"; exit 1 ;;
esac
if [ ! -e "${BACKUPS_BASE}" ]; then
    mkdir -m 700 -- "${BACKUPS_BASE}" || { echo "STOP: failed to create backup base"; exit 1; }
fi
if [ ! -d "${BACKUPS_BASE}" ] || [ -L "${BACKUPS_BASE}" ]; then
    echo "STOP: backup base is not a real directory"; exit 1
fi
BASE_OWNER=$(stat -c '%u' -- "${BACKUPS_BASE}") || { echo "STOP: cannot verify backup-base owner"; exit 1; }
[ "${BASE_OWNER}" = "$(id -u)" ] || { echo "STOP: backup base is not owned by current operator"; exit 1; }
chmod 700 -- "${BACKUPS_BASE}" || { echo "STOP: could not set private backup-base permissions"; exit 1; }
BASE_MODE=$(stat -c '%a' -- "${BACKUPS_BASE}") || { echo "STOP: cannot verify backup-base permissions"; exit 1; }
[ "${BASE_MODE}" = "700" ] || { echo "STOP: backup base is not mode 700"; exit 1; }

BACKUP_TS=$(date +%Y%m%d_%H%M%S)
BACKUP_DIR="${BACKUPS_BASE}/pre_pr4_${BACKUP_TS}"

# Strict collision check: fail if directory already exists
if [ -e "${BACKUP_DIR}" ]; then
    echo "ERROR: Backup directory '${BACKUP_DIR}' already exists! Aborting to prevent collision or overwrite."
    exit 1
fi

# Atomic create without -p: collision fails and existing backups are never removed.
mkdir -m 700 -- "${BACKUP_DIR}" || {
    echo "ERROR: Failed to create private backup directory '${BACKUP_DIR}'! Aborting."
    exit 1
}
chmod 700 -- "${BACKUP_DIR}" || { echo "STOP: could not set backup directory permissions"; exit 1; }
DIR_MODE=$(stat -c '%a' -- "${BACKUP_DIR}") || { echo "STOP: cannot verify backup directory"; exit 1; }
[ "${DIR_MODE}" = "700" ] || { echo "STOP: backup directory is not mode 700"; exit 1; }
DIR_OWNER=$(stat -c '%u' -- "${BACKUP_DIR}") || { echo "STOP: cannot verify backup directory owner"; exit 1; }
[ "${DIR_OWNER}" = "$(id -u)" ] || { echo "STOP: backup directory is not owned by current operator"; exit 1; }

echo "Private backup directory created: ${BACKUP_DIR}"
```

### Step 3.2: Create Consistent SQLite Online Backup
```bash
# 1. Use a generic snapshot name; active DB need not be trading.db.
# Never replace this snapshot with cp of the live DB or its WAL files.
DB_BACKUP="${BACKUP_DIR}/database.sqlite"
[ ! -e "${DB_BACKUP}" ] && [ ! -L "${DB_BACKUP}" ] || { echo "STOP: snapshot destination already exists"; exit 1; }
case "${DB_BACKUP}" in *"'"*) echo "STOP: SQLite CLI path contains an unsupported quote; do not substitute a raw copy"; exit 1 ;; esac
sqlite3 "${EFFECTIVE_DB_PATH}" ".backup '${DB_BACKUP}'" || { echo "STOP: SQLite online backup command failed"; exit 1; }

# 2. Passive WAL checkpoint to flush completed transactions safely to disk
sqlite3 "${EFFECTIVE_DB_PATH}" "PRAGMA wal_checkpoint(PASSIVE);" || { echo "STOP: passive checkpoint command failed; inspect before continuing"; exit 1; }

# 3. Verify backup file exists, is non-empty, and passes integrity check
test -f "${DB_BACKUP}" && [ ! -L "${DB_BACKUP}" ] || { echo "STOP: backup file missing or not regular"; exit 1; }
BACKUP_SIZE=$(stat -c '%s' -- "${DB_BACKUP}") || { echo "STOP: cannot read backup size"; exit 1; }
case "${BACKUP_SIZE}" in ''|*[!0-9]*) echo "STOP: invalid backup size"; exit 1 ;; esac
[ "${BACKUP_SIZE}" -gt 0 ] || { echo "STOP: backup is empty"; exit 1; }
printf 'Backup: %s (%s bytes)\n' "${DB_BACKUP}" "${BACKUP_SIZE}"
BACKUP_CHECK=$(sqlite3 "${DB_BACKUP}" "PRAGMA integrity_check;") || { echo "STOP: backup integrity command failed"; exit 1; }
[ "${BACKUP_CHECK}" = "ok" ] || { echo "STOP: backup integrity result is empty, ambiguous, or not exactly ok"; exit 1; }
printf 'Validated backup integrity: %s\n' "${BACKUP_CHECK}"
# Proceed only after exact output 'ok', nonzero file size, and all prior commands succeed.
```

### Step 3.3: Backup Production Configuration to Private Location
```bash
# Copy .env to private backup directory outside repository
cp -- "<VPS_APP_DIR>/.env" "${BACKUP_DIR}/.env.bak" || { echo "STOP: .env backup copy failed"; exit 1; }
chmod 600 -- "${BACKUP_DIR}/.env.bak" || { echo "STOP: could not restrict .env backup"; exit 1; }
ENV_BACKUP_MODE=$(stat -c '%a' -- "${BACKUP_DIR}/.env.bak") || { echo "STOP: cannot verify .env backup permissions"; exit 1; }
[ "${ENV_BACKUP_MODE}" = "600" ] || { echo "STOP: .env backup must be mode 600"; exit 1; }

# Record git state metadata
git -C "<VPS_APP_DIR>" rev-parse HEAD > "${BACKUP_DIR}/source_commit_sha.txt" || { echo "STOP: could not record source commit"; exit 1; }
git -C "<VPS_APP_DIR>" status --porcelain > "${BACKUP_DIR}/source_git_status.txt" || { echo "STOP: could not record git status"; exit 1; }

# Copy operational log file snapshot if present
if [ -f "${EFFECTIVE_LOG_PATH}" ]; then
    cp -- "${EFFECTIVE_LOG_PATH}" "${BACKUP_DIR}/bot_pre_deploy.log" || { echo "STOP: log snapshot copy failed"; exit 1; }
fi

# Shell variables (including BACKUP_DIR, DB_BACKUP and EFFECTIVE_DB_PATH) may
# disappear when the terminal/session closes. Before proceeding, create a
# mode-600 operator note outside the repository containing actual non-secret
# values: timestamp, VPS_APP_DIR, chartbot PID, verified cwd, current/target
# commits, EFFECTIVE_TESTNET evidence, exact EFFECTIVE_DB_PATH and LOG_PATH,
# BACKUP_DIR, DB_BACKUP, byte size, and exact integrity result. Do NOT record
# credentials, tokens, or the contents of .env. Check every write/permission
# command and reread the note in the next session before any rollback.
OPERATOR_NOTE="${BACKUP_DIR}/operator-notes.txt"
umask 077
install -m 600 /dev/null "${OPERATOR_NOTE}" || { echo "STOP: cannot create private operator note"; exit 1; }
${EDITOR:-vi} "${OPERATOR_NOTE}"
NOTE_MODE=$(stat -c '%a' -- "${OPERATOR_NOTE}") || { echo "STOP: cannot verify operator note permissions"; exit 1; }
[ "${NOTE_MODE}" = "600" ] || { echo "STOP: operator note must be mode 600"; exit 1; }
test -s "${OPERATOR_NOTE}" || { echo "STOP: operator note is empty"; exit 1; }
```

### Step 3.4: Remediation of `.env` Permissions (If Needed)
```bash
# If Step 2.2.4 revealed .env permissions more open than 600, tighten now:
chmod 600 -- "<VPS_APP_DIR>/.env" || { echo "STOP: could not restrict .env permissions"; exit 1; }
ENV_MODE=$(stat -c '%a' -- "<VPS_APP_DIR>/.env") || { echo "STOP: cannot verify .env permissions"; exit 1; }
[ "${ENV_MODE}" = "600" ] || { echo "STOP: .env is not mode 600"; exit 1; }
```

---

## 4. Controlled Deployment Procedure

Follow these steps sequentially in MobaXterm.

### Step 4.1: Fetch & Validate Target Remote Commit
```bash
cd "<VPS_APP_DIR>"

# 1. Fetch main branch from origin
git fetch origin main

# 2. Verify target release commit exists on origin/main
TARGET_SHA="c1b5ec410c67697b3003a17ff2f473ec77d52d59"
FETCHED_SHA=$(git rev-parse origin/main)

echo "Fetched origin/main SHA: ${FETCHED_SHA}"
if [ "${FETCHED_SHA}" != "${TARGET_SHA}" ]; then
    echo "ERROR: origin/main (${FETCHED_SHA}) does not match expected target (${TARGET_SHA})!"
    echo "Aborting deployment."
    exit 1
fi
```

### Step 4.2: Graceful Termination of Isolated `chartbot` Process
The bot implements graceful shutdown handlers for `SIGINT` and `SIGTERM` in `bot/main.py:260-296`.

```bash
# RE-VERIFY PROCESS IDENTITY RIGHT BEFORE SENDING SIGNAL:
ps -p "${CHARTBOT_BOT_PID}" -o pid,ppid,args
# Ensure PID matches the single verified bot child under CHARTBOT_PANE_PID.
# STOP RULE: If process identity is not verified, STOP! Do not guess.

# 1. Send SIGINT (graceful shutdown) strictly to the verified PID:
kill -INT "${CHARTBOT_BOT_PID}"

# 2. Wait up to 15 seconds for clean exit:
for i in {1..15}; do
    if ! kill -0 "${CHARTBOT_BOT_PID}" 2>/dev/null; then
        echo "ChartPattern_Bot stopped cleanly."
        break
    fi
    echo "Waiting for clean shutdown... (${i}/15)"
    sleep 1
done

# 3. If still running after 15s, escalate to SIGTERM:
if kill -0 "${CHARTBOT_BOT_PID}" 2>/dev/null; then
    echo "Escalating to SIGTERM for PID ${CHARTBOT_BOT_PID}..."
    kill -TERM "${CHARTBOT_BOT_PID}"
    sleep 3
fi

# 4. Verify process is terminated:
if kill -0 "${CHARTBOT_BOT_PID}" 2>/dev/null; then
    echo "CRITICAL: Process ${CHARTBOT_BOT_PID} did not terminate! STOP and inspect."
    exit 1
fi
```

### Step 4.3: Switch Working Tree to Target Commit
```bash
cd "<VPS_APP_DIR>"
TARGET_SHA="c1b5ec410c67697b3003a17ff2f473ec77d52d59"

# Reconfirm branch, HEAD, staged/unstaged state and untracked files immediately
# before checkout. The tracked/index state must be clean. Never use reset/clean.
git branch --show-current
git rev-parse HEAD
git status --short
git diff --quiet || { echo "STOP: tracked working-tree changes exist"; exit 1; }
git diff --cached --quiet || { echo "STOP: staged changes exist"; exit 1; }
# Compare untracked paths with files tracked by origin/main. If any overlap,
# stop for operator inspection. Do not remove/rewrite the research files.
python - "${TARGET_SHA}" <<'PY'
import subprocess, sys
target = sys.argv[1]
u = set(filter(None, subprocess.check_output(["git", "ls-files", "--others", "-z"]).split(b"\0")))
t = set(filter(None, subprocess.check_output(["git", "ls-tree", "-r", "-z", "--name-only", target]).split(b"\0")))
c = sorted((a, b) for a in u for b in t if a == b or a.startswith(b + b"/") or b.startswith(a + b"/"))
for untracked_path, tracked_path in c:
    print("CHECKOUT COLLISION:", untracked_path.decode(errors="replace"), "<->", tracked_path.decode(errors="replace"))
if c:
    sys.exit(1)
print("No untracked checkout path collisions.")
PY
if [ $? -ne 0 ]; then echo "STOP: inspect untracked checkout conflicts"; exit 1; fi

# Fast-forward merge to target SHA without creating merge commits:
git checkout --no-overwrite-ignore main
git merge --ff-only origin/main

# Verify new HEAD commit matches target SHA exactly:
NEW_HEAD=$(git rev-parse HEAD)
echo "Current HEAD: ${NEW_HEAD}"
if [ "${NEW_HEAD}" != "${TARGET_SHA}" ]; then
    echo "ERROR: Working tree is on ${NEW_HEAD}, expected ${TARGET_SHA}!"
    exit 1
fi

# Verify untracked research files remain untouched:
git status --porcelain
```

### Step 4.4: Dependency & Byte-Compile Verification (No Automated pip install)
> [!IMPORTANT]
> **Dependency Safety Policy**:<br>
> Do **NOT** run `pip install -r requirements.txt` automatically during deployment.<br>
> PR #4 introduces **zero new external dependencies**. Always inspect the existing virtual environment first.<br>
> Note that `python -m compileall -q bot tests` verifies **syntax compilation only**; it does not check all runtime imports or package compatibility.

```bash
cd "<VPS_APP_DIR>"
source "<VPS_VENV_PATH>/bin/activate"

# 1. Verify existing installed packages and environment consistency:
python -m pip check

# 2. Verify that critical application packages are present and importable:
python -c "import binance, pandas, numpy, mplfinance, matplotlib, telegram, aiosqlite, tenacity; print('Core dependencies OK')"

# STOP RULE:
# If 'pip check' reports broken dependencies or if core imports fail:
# STOP IMMEDIATELY and request operator decision before installing or upgrading any packages!
# Do not run unverified package installations.

# 3. Execute byte-compile verification across codebase (checks syntax only):
python -m compileall -q bot tests
# Exit code must be 0 (no syntax errors).
```

### Step 4.5: Restart Application inside `chartbot` Tmux Session

#### Method 1: Interactive Pane Startup (PRIMARY & RECOMMENDED)
This method allows the operator to observe the shell prompt, verify working directory, and see immediate startup logs.

1. **Attach to the `chartbot` session**:
   ```bash
   tmux attach -t chartbot
   ```
2. **Verify environment inside the pane**:
   ```bash
   pwd
   # Ensure output matches <VPS_APP_DIR>
   ```
3. **Set the descriptor-verified paths from the reread private operator note, then resolve candidate configuration in this exact pane before starting.** This check must run after venv activation, from the verified cwd, using the same pane environment that will launch the bot. It prints no credentials. Any read/parser failure, empty/unknown/duplicate value, interpolation, path mismatch, or result other than MAINNET means STOP; do not change `.env` automatically.
   ```bash
   export EXPECTED_DB_PATH='<recorded EFFECTIVE_DB_PATH>'
   export EXPECTED_LOG_PATH='<recorded EFFECTIVE_LOG_PATH>'
   ```
   ```bash
   source "<VPS_VENV_PATH>/bin/activate"
   python - <<'PY'
import os
from dotenv.parser import parse_stream
from pathlib import Path

sentinel = object()
inherited = os.environ.get("BINANCE_TESTNET", sentinel)
dotenv_disabled = os.environ.get("PYTHON_DOTENV_DISABLED", sentinel)
if dotenv_disabled is not sentinel and dotenv_disabled.casefold() not in {"0", "false", "f", "no", "n", "off"}:
    raise SystemExit("STOP: PYTHON_DOTENV_DISABLED is empty, unknown, or disables dotenv")
try:
    config_dir = os.path.dirname(os.path.abspath("bot/config.py"))
    dotenv_path = None
    while True:
        candidate = os.path.join(config_dir, ".env")
        if os.path.lexists(candidate):
            dotenv_path = candidate
            break
        parent = os.path.dirname(config_dir)
        if parent == config_dir:
            break
        config_dir = parent
    value = None
    state = "ABSENT"
    if dotenv_path is not None:
        with open(dotenv_path, encoding="utf-8") as stream:
            bindings = list(parse_stream(stream))
        if any(binding.error for binding in bindings):
            raise ValueError("dotenv parse error")
        matches = [binding for binding in bindings if binding.key == "BINANCE_TESTNET"]
        if len(matches) > 1:
            raise ValueError("duplicate BINANCE_TESTNET")
        if matches:
            value = matches[0].value
            state = "BARE" if value is None else "EMPTY" if value == "" else "VALUE"
    raw = inherited if inherited is not sentinel else value
    if raw is None and inherited is sentinel and state == "ABSENT":
        mode = "MAINNET"  # proven code default: both sources unset
    else:
        if raw is None or "$" in raw:
            raise ValueError("empty, bare, or interpolated value")
        normalized = raw.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            mode = "TESTNET"
        elif normalized in {"0", "false", "no", "off"}:
            mode = "MAINNET"
        else:
            raise ValueError("unrecognized boolean")
    from bot.config import settings
    if settings.use_testnet != (mode == "TESTNET"):
        raise ValueError("candidate resolution disagrees with application settings")
    expected_db = os.environ.get("EXPECTED_DB_PATH")
    expected_log = os.environ.get("EXPECTED_LOG_PATH")
    if not expected_db or not expected_log:
        raise ValueError("operator-recorded effective paths are required")
    db_path = Path(settings.db_path)
    log_path = Path(settings.log_path)
    if not db_path.is_absolute():
        db_path = Path.cwd() / db_path
    if not log_path.is_absolute():
        log_path = Path.cwd() / log_path
    if db_path.resolve() != Path(expected_db).resolve():
        raise ValueError("candidate DB_PATH does not match active descriptor path")
    if log_path.resolve() != Path(expected_log).resolve():
        raise ValueError("candidate LOG_PATH does not match active descriptor path")
    print("Candidate Binance mode:", mode)
    if mode != "MAINNET":
        raise SystemExit("STOP: candidate is not MAINNET")
except Exception as exc:
    print("STOP: Binance configuration could not be proven:", type(exc).__name__)
    raise SystemExit(1)
PY
   ```
4. **Execute startup command manually only after candidate mode is proven MAINNET**:
   ```bash
   cd "<VPS_APP_DIR>"
   source "<VPS_VENV_PATH>/bin/activate"
   python -m bot.main
   ```
4. **Observe startup banner**:
   Confirm that the NeoElla Trade banner and runtime fingerprint appear without traceback errors.
5. **Detach cleanly from tmux**:
   Press `Ctrl+B`, release, then press `D`.

#### Method 2: Non-Interactive Pane Dispatch (Alternative)
**Disabled unless the operator can run and inspect the complete Method 1 preflight inside the verified pane using the same shell environment before dispatching startup as a separate action.** Never combine preflight and startup in one `tmux send-keys` command. If the candidate mode and both paths cannot be visibly verified in that pane, STOP; use Method 1 or request operator assistance.

---

## 5. Post-Deployment Validation & Health Checks

Perform systematic verification over a **15 to 30 minute observation window**.

### 5.1 Process & Runtime Fingerprint Verification
```bash
# 1. Identify new bot PID under the chartbot pane:
ps -o pid,ppid,stat,args --ppid "<CHARTBOT_PANE_PID>"

# 2. Check startup logs for runtime fingerprint:
tail -n 60 "${EFFECTIVE_LOG_PATH}"
```

**Expected Runtime Fingerprint Pattern**:
```text
Runtime fingerprint | commit=c1b5ec410c67697b3003a17ff2f473ec77d52d59 | source=dirty | Binance=MAINNET | STRUCTURAL_FRACTAL_PERIOD=30 | STRUCTURAL_MIN_SPAN=65 | timeframes=1d,3d,1w
```
> [!NOTE]
> **Source Fingerprint Note**: In `bot/main.py:48-64`, `_source_fingerprint()` marks the source as `dirty` whenever `git status --porcelain` returns any output. Because untracked research files (such as `scratch/empirical_fractal_research.py`) reside in the working tree, **`source=dirty` is expected and normal**. Do NOT treat `source=dirty` as an error if `git diff --quiet` and `git diff --cached --quiet` are clean.

### 5.2 Telegram Diagnostic Health Commands
Send the following commands directly to the Telegram bot:

1. **/health**
   - **Database**: Must display `Connected (WAL mode)`.
   - **Pending Outbox Notifications**: `0` (or steadily flushing).
   - **Last Scanner Cycle**: Timestamp advances across cycles and is recent under the configured health threshold (the current code marks scanner heartbeat recent up to two configured scan intervals).
   - **Scanner Pair Failures**: Observe the count over consecutive cycles and correlate each failure with its pair-level log cause. This release does not guarantee zero failures from unrelated symbols, network errors, or other validation conditions.
   - **Binance Requests**: Incrementing normally; `exhausted` must be `0`.

2. **/status**
   - Scanner status: `🟢 Recent heartbeat`.
   - Tracker status: `🟢 Recent heartbeat`.
   - Storage: `SQLite WAL mode (persistent)`.

### 5.3 Scanner Cycle & Candle Recovery Monitoring
Inspect live logs through at least one full scan cycle:
```bash
tail -f "${EFFECTIVE_LOG_PATH}"
```

**Verification Targets**:
1. **3D Discontinuity Recovery**:
   If a `3d` cadence gap is encountered and the latest suffix meets the implementation threshold, the bot should log a line such as:
   ```text
   [<SYMBOL> 3d] 3D candle discontinuity detected at historical index ...; discarding ... disjoint historical bars and retaining ... strictly contiguous bars
   ```
   and continue evaluation using that suffix. A suffix below 220 bars must still fail closed with `ValueError: Misaligned or missing 3d candles`; do not treat every such error as a regression.
2. **Scanner Summary**:
   Review the end-of-scan line and investigate each reported pair failure using its associated log context. Do not treat zero failures as a release guarantee:
   ```text
   Scan completed: N symbols, M pairs checked, P pair failure(s), K new signal(s)
   ```
3. **Signal Price Validation**:
   Verify logs do not show rejected non-finite/non-positive price fields or invalid direction ordering. For the LONG ONLY structural detectors, accepted prices must satisfy finite positive values and $0 < SL < Entry < TP1 < TP2 < TP3$. A rejection is a validation event, not evidence that an invalid signal was accepted.
4. **Database Metadata Health**:
   ```bash
   sqlite3 "${EFFECTIVE_DB_PATH}" "SELECT key, value, updated_at FROM system_metadata WHERE key IN ('last_scan_pair_failures', 'last_successful_scan_at', 'binance_validation_failures');"
   ```

### 5.4 Explicit Pass / Fail Criteria
| Metric | Pass Condition | Fail Condition |
| :--- | :--- | :--- |
| **Process Health** | Runs continuously $>15$ mins in `chartbot` | Process exits, crashes, or loops |
| **Commit Fingerprint** | `commit=c1b5ec410c67697b3003a17ff2f473ec77d52d59` | Unknown SHA or old baseline |
| **Scanner Failures** | Scanner cycles continue; each pair failure is attributable and investigated | Stale cycles, unexplained pair failures, or recurring validation errors |
| **Binance API** | Normal HTTP 200, 0 429 backoff exhaustion | Connection severed or rate-limited |
| **Telegram /health** | Returns valid stats, outbox flushes | No response or crash |
| **Signal Prices** | Finite positive values and LONG ordering $0 < SL < Entry < TP1 < TP2 < TP3$ are enforced | Accepted non-finite/non-positive values or invalid target ordering |

---

## 6. Emergency Rollback Procedure

If critical failures occur (repeated unhandled exceptions, scanner regression, or connection collapse), follow this rollback procedure.

### Step 6.1: Isolate & Stop the Bot Process
```bash
# 1. Inspect child processes running strictly under CHARTBOT_PANE_PID:
ps -o pid,ppid,stat,user,args --ppid "<CHARTBOT_PANE_PID>"

# 2. Re-verify working directory (pwdx) and confirm EXACTLY ONE process matches:
# pwdx <CHILD_PID>

# STOP RULE: If 0 or >1 processes match, or if identity is uncertain:
# STOP IMMEDIATELY! Attach interactively to chartbot (tmux attach -t chartbot) to inspect manually.
# DO NOT send any kill signal. NEVER use generic pgrep or pkill!

# 3. Record verified PID as ROLLBACK_BOT_PID and re-verify identity immediately prior to signalling:
ps -p "<ROLLBACK_BOT_PID>" -o pid,ppid,args

# 4. Send graceful SIGINT to verified PID:
kill -INT "<ROLLBACK_BOT_PID>"
for i in {1..15}; do
    if ! kill -0 "<ROLLBACK_BOT_PID>" 2>/dev/null; then
        echo "Bot process stopped cleanly."
        break
    fi
    echo "Waiting for clean shutdown... (${i}/15)"
    sleep 1
done

# 5. If still running after 15s, escalate to SIGTERM:
if kill -0 "<ROLLBACK_BOT_PID>" 2>/dev/null; then
    echo "Escalating to SIGTERM for PID ${ROLLBACK_BOT_PID}..."
    kill -TERM "<ROLLBACK_BOT_PID>"
    sleep 3
fi

# 6. Verify process is absent by PID; do not infer stopped from kill -0 failure
# alone (it can also mean permission/error). Any row, inability to inspect, or
# PID identity ambiguity means STOP before source or database changes.
ROLLBACK_PID="<verified ROLLBACK_BOT_PID>"
PID_ROW=$(ps -p "${ROLLBACK_PID}" -o pid=,ppid=,stat=,args=) || { echo "STOP: cannot verify process state"; exit 1; }
if [ -n "${PID_ROW}" ]; then
    echo "STOP: process/PID still present; inspect identity before continuing"
    exit 1
fi
echo "Verified: no process currently owns recorded PID ${ROLLBACK_PID}."
```

### Step 6.2: Revert Source Code to Rollback Baseline
```bash
cd "<VPS_APP_DIR>"

# Re-read the private operator note (never trust variables from an earlier
# shell). Verify the recorded VPS_APP_DIR, backup path, DB path, and backup
# validation result against current read-only evidence before proceeding.
# If the note is missing, unreadable, incomplete, or inconsistent: STOP.
${EDITOR:-vi} "<recorded private operator note path>"

ROLLBACK_TARGET="2ed495935171b30fc3b9b78b76b7491a8b7ac291"
git branch --show-current
git rev-parse HEAD
git status --short

# Rollback requires a completely clean tracked/index state. Do not stash, reset,
# clean, or discard anything as a shortcut. Any tracked/index change: STOP for
# operator inspection and explicit resolution outside this procedure.
CURRENT_BRANCH=$(git branch --show-current)
CURRENT_HEAD=$(git rev-parse HEAD)
printf 'Rollback starting branch=%s HEAD=%s\n' "${CURRENT_BRANCH}" "${CURRENT_HEAD}"
# For the PR #4 rollback, expected starting state is main at the target release.
# Any other branch or HEAD is a discrepancy: STOP and ask the lead engineer.
if [ "${CURRENT_BRANCH}" != "main" ] || [ "${CURRENT_HEAD}" != "c1b5ec410c67697b3003a17ff2f473ec77d52d59" ]; then
    echo "STOP: unexpected rollback starting branch/HEAD"; exit 1
fi
git diff --quiet || { echo "STOP: tracked working-tree changes exist"; exit 1; }
git diff --cached --quiet || { echo "STOP: staged changes exist"; exit 1; }
git cat-file -e "${ROLLBACK_TARGET}^{commit}" || { echo "STOP: rollback commit is unavailable"; exit 1; }

# Ensure no untracked path would be overwritten by checkout. This check handles
# spaces and reports every collision; it does not modify files.
python - "${ROLLBACK_TARGET}" <<'PY'
import subprocess, sys
target = sys.argv[1]
untracked = subprocess.check_output(["git", "ls-files", "--others", "-z"]).split(b"\0")
tree = subprocess.check_output(["git", "ls-tree", "-r", "-z", "--name-only", target]).split(b"\0")
untracked = {p for p in untracked if p}
tree = {p for p in tree if p}
collisions = sorted((a, b) for a in untracked for b in tree if a == b or a.startswith(b + b"/") or b.startswith(a + b"/"))
if collisions:
    for untracked_path, tracked_path in collisions:
        print("CHECKOUT COLLISION:", untracked_path.decode(errors="replace"), "<->", tracked_path.decode(errors="replace"))
    sys.exit(1)
print("No untracked checkout path collisions.")
PY
if [ $? -ne 0 ]; then
    echo "STOP: untracked files may be affected; inspect manually. Do not checkout."
    exit 1
fi

# Preserve the named research files. If they are present, confirm they remain
# untracked and are not listed as checkout collisions above. Never delete them.
git status --short -- scratch/empirical_fractal_research.py scratch/rolling_fractal_scan.py

# Only after all preconditions pass, switch to the pinned rollback baseline.
git checkout --no-overwrite-ignore --detach "${ROLLBACK_TARGET}" || { echo "STOP: rollback checkout failed; inspect repository state"; exit 1; }

# Confirm current HEAD matches baseline:
ROLLBACK_SHA=$(git rev-parse HEAD)
echo "Rollback HEAD: ${ROLLBACK_SHA}"
if [ "${ROLLBACK_SHA}" != "${ROLLBACK_TARGET}" ]; then
    echo "ERROR: Failed to revert to 2ed495935171b30fc3b9b78b76b7491a8b7ac291!"
    exit 1
fi

# Run compile check:
source "<VPS_VENV_PATH>/bin/activate"
python -m compileall -q bot tests
```

### Step 6.3: Database Decision Matrix (DO NOT Blindly Overwrite)
> [!CAUTION]
> **Data Loss Prevention**: Overwriting a live database with an older backup will erase legitimate signals and target milestones tracked during the deployment window.

- **Condition 1 (Database is Healthy)**:
  Run integrity check against the exact operator-recorded database path. Capture command status and output:
  ```bash
  RESTORE_DB="<exact EFFECTIVE_DB_PATH from reread private operator note>"
  DB_CHECK=$(sqlite3 "${RESTORE_DB}" "PRAGMA integrity_check;")
  DB_CHECK_RC=$?
  if [ "${DB_CHECK_RC}" -ne 0 ] || [ -z "${DB_CHECK}" ]; then
      echo "STOP: DB integrity result unavailable; corruption is not confirmed. Do not move or restore files."
      exit 1
  fi
  ```
  If output is exactly `ok`, **LEAVE THE CURRENT DATABASE UNTOUCHED**. Do not restore the backup. If output is non-empty and not exactly `ok`, record the exact integrity errors as corruption evidence, then continue only through Condition 2. Never interpret a failed command or empty output as confirmed corruption.
- **Condition 2 (Database is Corrupted)**:
  Continue only after the integrity command succeeded and returned non-empty error output, the database path and validated backup path have been reread from the private operator note and reverified, and the bot is confirmed stopped. Verify that the bot PID is absent (not merely that a signal was sent), and that no process identity ambiguity remains:
  ```bash
  BOT_PID="<recorded CHARTBOT_BOT_PID>"
  PID_ROW=$(ps -p "${BOT_PID}" -o pid=,ppid=,stat=,args=) || { echo "STOP: cannot inspect bot PID"; exit 1; }
  [ -z "${PID_ROW}" ] || { echo "STOP: bot PID/process is still present or ambiguous"; exit 1; }

  RESTORE_DB="<exact EFFECTIVE_DB_PATH from reread private operator note>"
  DB_BACKUP="<exact DB_BACKUP path from reread private operator note>"
  test -f "${RESTORE_DB}" && [ ! -L "${RESTORE_DB}" ] || { echo "STOP: DB path is not the verified regular file"; exit 1; }
  test -f "${DB_BACKUP}" && [ ! -L "${DB_BACKUP}" ] || { echo "STOP: validated backup is missing"; exit 1; }
  BACKUP_CHECK=$(sqlite3 "${DB_BACKUP}" "PRAGMA integrity_check;") || { echo "STOP: backup validation command failed"; exit 1; }
  [ "${BACKUP_CHECK}" = "ok" ] || { echo "STOP: backup is not exactly integrity=ok"; exit 1; }
  BACKUP_SIZE=$(stat -c '%s' -- "${DB_BACKUP}") || { echo "STOP: cannot verify backup size"; exit 1; }
  [ "${BACKUP_SIZE}" -gt 0 ] || { echo "STOP: backup is empty"; exit 1; }

  CORRUPT_TS=$(date +%Y%m%d_%H%M%S)
  for suffix in '' '-wal' '-shm'; do
      source_path="${RESTORE_DB}${suffix}"
      { [ -e "${source_path}" ] || [ -L "${source_path}" ]; } || continue
      target_path="${source_path}.corrupted_${CORRUPT_TS}"
      { [ ! -e "${target_path}" ] && [ ! -L "${target_path}" ]; } || { echo "STOP: quarantine name collision: ${target_path}"; exit 1; }
  done
  # Recheck bot absence and exact database path immediately before moving files.
  PID_ROW=$(ps -p "${BOT_PID}" -o pid=,ppid=,stat=,args=) || { echo "STOP: cannot recheck bot PID"; exit 1; }
  [ -z "${PID_ROW}" ] || { echo "STOP: bot PID/process appeared; do not move database"; exit 1; }
  # Move DB and any sidecars individually; any failure is a hard stop.
  for suffix in '' '-wal' '-shm'; do
      source_path="${RESTORE_DB}${suffix}"
      { [ -e "${source_path}" ] || [ -L "${source_path}" ]; } || continue
      target_path="${source_path}.corrupted_${CORRUPT_TS}"
      { [ ! -e "${target_path}" ] && [ ! -L "${target_path}" ]; } || { echo "STOP: quarantine name collision: ${target_path}"; exit 1; }
      mv -n -- "${source_path}" "${target_path}" || { echo "STOP: failed to quarantine ${source_path}; do not restore"; exit 1; }
      { [ ! -e "${source_path}" ] && [ -e "${target_path}" ]; } || { echo "STOP: quarantine move did not complete: ${source_path}"; exit 1; }
  done
  { [ ! -e "${RESTORE_DB}" ] && [ ! -L "${RESTORE_DB}" ]; } || { echo "STOP: destination DB still exists; do not overwrite"; exit 1; }
  cp --no-clobber -- "${DB_BACKUP}" "${RESTORE_DB}" || { echo "STOP: database restore copy failed"; exit 1; }
  cmp -s -- "${DB_BACKUP}" "${RESTORE_DB}" || { echo "STOP: restored file does not match validated snapshot"; exit 1; }
  RESTORED_CHECK=$(sqlite3 "${RESTORE_DB}" "PRAGMA integrity_check;") || { echo "STOP: restored database integrity command failed; do not restart"; exit 1; }
  [ "${RESTORED_CHECK}" = "ok" ] || { echo "STOP: restored database integrity is not exactly ok; do not restart"; exit 1; }
  echo "Restored database integrity verified: ${RESTORED_CHECK}"
  ```

### Step 6.4: Restart Bot on Baseline Commit
**Interactive method (Recommended)**:
1. `tmux attach -t chartbot`
2. Reread the private operator note and repeat the candidate mode/DB path/log path preflight from Step 4.5 in this pane. Proceed only on exact `MAINNET` and matching recorded paths; otherwise STOP.
3. Run: `cd "<VPS_APP_DIR>" && source "<VPS_VENV_PATH>/bin/activate" && python -m bot.main`
4. Detach: `Ctrl+B`, `D`.

**Non-interactive fallback**:
Disabled unless the operator can perform and inspect the same preflight in the verified pane before a separate startup dispatch. If not, STOP.

### Step 6.5: Verify Rollback Health
1. Verify startup log reports `commit=2ed495935171b30fc3b9b78b76b7491a8b7ac291`.
2. Confirm Telegram `/health` and `/status` respond.

---

## 7. Troubleshooting & Diagnostics Reference

### 7.1 "database is locked"
- Never use blanket `pkill`. Instead, identify the specific process holding the file lock:
  ```bash
  fuser "${EFFECTIVE_DB_PATH}"
  ```
- Inspect the PID returned before sending any signal.

### 7.2 Stale Heartbeat in `/status`
- If `/status` reports `⚠️ Stale heartbeat`, check `${EFFECTIVE_LOG_PATH}` for active unhandled exceptions in `scan_loop` or `tracker_loop`.

---

## 8. Safety Verification & Sign-Off

This checklist describes safeguards required by the procedure; it is not evidence that an operator has completed them or that production state has been verified. Each item remains a mandatory gate during execution:

- **Process isolation**: Discover only the verified child under `CHARTBOT_PANE_PID`; never use generic process matching.
- **Protected workloads**: `EMA-50-200` and local research files are expressly excluded from changes.
- **Dependencies and syntax**: No automated pip install; `pip check`, imports, and `compileall` are separate checks, and compileall proves syntax only.
- **Runtime mode and paths**: Active `BINANCE_TESTNET`, DB path, log path, cwd, and process identity require operator evidence. `/proc` and current `.env` alone do not establish effective settings. Unknown or ambiguous values mean STOP.
- **Backup gate**: Collision-safe private directory, checked permissions, nonempty SQLite `.backup` snapshot, and exact backup `integrity_check` output `ok` are all required before source changes.
- **Git transitions**: Inspect branch, HEAD, tracked/index state, and untracked checkout collisions first. Preserve research files. Never run `git reset --hard` or `git clean`.
- **Database rollback**: Never overwrite a healthy database. Quarantine and restore only after corruption is confirmed, the bot is verified stopped, paths are reread/reverified, the snapshot is revalidated, and restored integrity is exactly `ok`.
- **Environment boundary**: The assistant has not accessed or verified the VPS. All runtime, process, configuration, and database checks in this runbook are for the human operator; no deployment or restart is authorized by this document alone.

### 8.1 Source-Verified Technical Reference & Claim Mapping Table

The table below provides an auditable mapping for all 11 technical references utilized in this runbook. Each entry documents the exact direct URL, the specific technical claim supported, the corresponding runbook section, and the explicit limits of what the source cannot prove.

> [!IMPORTANT]
> **Separation of Truth Domains**:
> - **External Official Documentation (Binance, SQLite, Git, Linux, python-dotenv)**: Verifies only public API specifications, protocol contracts, database engine semantics, and operating system tooling behavior. External documentation **CANNOT** prove that ChartPattern_Bot's trading strategy or fractal period 30 parameters are correct or profitable.
> - **Repository Source Code, PR #4 Diff, and Regression Tests**: Serves as the sole authoritative truth for trading logic, fractal period 30 parameters, candle validation rules, signal pricing constraints, and the 220-bar suffix recovery guard.
> - **Human Operator Manual Inspection (MobaXterm)**: Serves as the sole authoritative truth for VPS process identity, active PIDs, effective configuration, open file descriptors, and database health. Antigravity IDE has no remote access.

| # | Source Name & Direct URL | Specific Technical Claim Supported | Runbook Section | Claim Boundaries & What Source CANNOT Prove |
| :- | :--- | :--- | :--- | :--- |
| **1** | [Binance USDⓈ-M Futures — Kline/Candlestick REST API](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/market-data/rest-api/Kline-Candlestick-Data) | Documents the `GET /fapi/v1/klines` REST endpoint, response array structure (`[0]` open time ms, `[1-4]` OHLC, `[6]` close time ms), supported intervals (`1d`, `3d`, `1w`), and maximum limit (1500 bars). | § 1.2 (A, B, C)<br>§ 4.5<br>§ 5.3 | Documents Binance public API specifications. Does **not** prove strategy validity, does **not** prove bot code correctness, and does **not** prove absence of historical cadence gaps in live VPS data feeds. |
| **2** | [Binance USDⓈ-M Futures — Kline/Candlestick WebSocket Streams](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/websocket-market-streams/Kline-Candlestick-Streams) | Documents WebSocket kline stream event schema (`k` payload: `t` start time, `T` close time, `i` interval, `x` candle close boolean) and continuous bar delivery cadence. | § 1.2 (B)<br>§ 5.3 | Documents real-time streaming protocol. Does **not** prove bot pattern detection logic and does **not** prove live socket health or network latency on the VPS. |
| **3** | [Binance Futures Connector Python (`um_futures/market.py`)](https://github.com/binance/binance-futures-connector-python/blob/main/binance/um_futures/market.py) | Reference implementation for Binance USDⓈ-M client methods, parameter mapping (`symbol`, `interval`, `limit`, `startTime`, `endTime`), and endpoint URL routing. | § 1.2 (A, C)<br>§ 4.4<br>§ 5.3 | Validates Python library call conventions. Does **not** prove that the custom validator in `bot/binance_client.py` is bug-free and does **not** verify connector behavior on the VPS. |
| **4** | [SQLite — Online Backup API](https://www.sqlite.org/backup.html) | Verifies that SQLite online backup (`sqlite3_backup_*` / `.backup`) creates a transactionally consistent, uncorrupted database copy without long exclusive table locks, integrating live transactions from WAL mode safely. | § 3 (Callout)<br>§ 3.2<br>§ 6.3 | Documents SQLite storage engine backup mechanics. Does **not** prove data semantic correctness if the source database was corrupted prior to backup, and does **not** identify database paths on the VPS. |
| **5** | [SQLite — PRAGMA integrity_check](https://www.sqlite.org/pragma.html#pragma_integrity_check) | Documents that `PRAGMA integrity_check` performs an out-of-band verification of database B-trees, freelists, and table/index linkages, returning exactly `ok` on success. It specifically does **not** check foreign key constraints (which require `PRAGMA foreign_key_check`). | § 2.2.5<br>§ 3.2<br>§ 6.3 | Documents database integrity checking mechanics. Does **not** verify database health on the live server without interactive execution by the human operator. |
| **6** | [SQLite — Command-Line Shell (CLI)](https://www.sqlite.org/cli.html) | Documents `sqlite3` CLI usage, the dot-command `.backup <destination>` for online backup, and invocation flag `-readonly` to open databases without creating shared locks or modifying files. | § 2.2.5<br>§ 3.2<br>§ 6.3 | Documents binary command-line flags. Does **not** prove that `sqlite3` CLI is installed, on PATH, or executable in the VPS environment. |
| **7** | [Git — git checkout & Ignore Protection](https://git-scm.com/docs/git-checkout) | Documents that `git checkout` switches branches/commits and that `--no-overwrite-ignore` prevents checkout operations from silently overwriting or destroying local untracked/ignored files. | § 2.2.3<br>§ 4.3<br>§ 6.2 | Documents Git command safety switches. Does **not** prove current working tree status, branch pointer, or index clean state on the VPS. |
| **8** | [python-dotenv — Precedence & Disabled Loading](https://github.com/theskumar/python-dotenv) | Documents that `load_dotenv(override=False)` prioritizes existing OS process environment variables over `.env` entries, and that `PYTHON_DOTENV_DISABLED` disables file loading when set to a truthy value. | § 2.2.4 (A)<br>§ 4.5 | Documents dotenv library resolution rules. Does **not** prove which environment variables are actively loaded in the running Python memory space on the VPS. |
| **9** | [Linux Man Page — /proc/[pid]/environ](https://www.man7.org/linux/man-pages/man5/proc_pid_environ.5.html) | Documents that `/proc/[pid]/environ` reflects the initial environment block established at `execve(2)` invocation. It does **not** reflect subsequent in-memory modifications made by `os.environ` or `load_dotenv()`. | § 2.2.4 (A)<br>§ 4.5 | Documents Linux kernel `/proc` filesystem semantics. Does **not** prove current runtime settings of a running process without corroboration from runtime logs. |
| **10** | [Diff Commit PR #4 (`c1b5ec410c67697b3003a17ff2f473ec77d52d59`)](https://github.com/roxy4798/ChartPattern_Bot/commit/c1b5ec410c67697b3003a17ff2f473ec77d52d59) | Primary repository truth for remediations: restricts contiguous suffix recovery to `3d` with a 220-bar guard, preserves strict 1D/1W checks, protects cache boundary reuse, enforces finite positive price ordering in `_rr_ok`, and adds signal diagnostics. | § 1.1<br>§ 1.2<br>§ 4.1<br>§ 4.3 | Proves repository source code diff. Does **not** prove that the target commit has been fetched, checked out, or deployed on the Alibaba Cloud VPS. |
| **11** | [GitHub Actions Run 38148330891 (Tests #21)](https://github.com/roxy4798/ChartPattern_Bot/actions/runs/38148330891) | Primary CI truth confirming all 42 unit and regression tests passed (`Success`) on Linux for target release commit `c1b5ec410c67697b3003a17ff2f473ec77d52d59`. | § 1.1<br>§ 1.3<br>§ 4.4 | Proves remote CI regression test suite status. Does **not** prove production environment health, API key validity, or live system stability on the VPS. |

---
