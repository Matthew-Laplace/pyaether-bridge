#!/usr/bin/env bash
# smoke_cli.sh -- pyaether CLI smoke test.
#
# Only paths that need neither Docker nor the daemon are exercised: the script
# isolates PYAETHER_BRIDGE_HOME / PYAETHER_DAEMON_SOCK / PYAETHER_CATALOG_DB in
# a temp directory and sets PYAETHER_BRIDGE_NO_AUTOSTART=1, so it never starts
# Docker or the host daemon.
#
# Usage: bash tests/smoke_cli.sh

set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${PYTHON:-python3}"
PASS=0
FAIL=0

TMPDIR_SMOKE="$(mktemp -d "${TMPDIR:-/tmp}/pyaether-smoke.XXXXXX")"
cleanup() { rm -rf "$TMPDIR_SMOKE"; }
trap cleanup EXIT

export PYAETHER_BRIDGE_HOME="$TMPDIR_SMOKE/home"
export PYAETHER_DAEMON_SOCK="$TMPDIR_SMOKE/home/daemon.sock"
export PYAETHER_CATALOG_DB="$TMPDIR_SMOKE/missing-catalog.sqlite"   # deliberately points at a missing DB
export PYAETHER_BRIDGE_NO_AUTOSTART=1
mkdir -p "$PYAETHER_BRIDGE_HOME"

CLI="$ROOT/bin/pyaether"

ok()   { PASS=$((PASS + 1)); printf '  [ok]   %s\n' "$1"; }
bad()  { FAIL=$((FAIL + 1)); printf '  [FAIL] %s\n' "$1"; }

# run_case <name> <expected exit code> <timeout seconds> <command...>
# Output goes to $TMPDIR_SMOKE/<n>.out; exit code to LAST_RC; timeout to LAST_TIMEOUT.
run_case() {
    local name="$1" want="$2" timeout_s="$3"
    shift 3
    local out="$TMPDIR_SMOKE/out.$PASS.$FAIL"
    LAST_TIMEOUT=0
    "$@" >"$out" 2>&1 &
    local pid=$!
    local ticks=0
    local max_ticks=$((timeout_s * 5))
    while kill -0 "$pid" 2>/dev/null; do
        if [ "$ticks" -ge "$max_ticks" ]; then
            kill -9 "$pid" 2>/dev/null
            LAST_TIMEOUT=1
            break
        fi
        sleep 0.2
        ticks=$((ticks + 1))
    done
    wait "$pid" 2>/dev/null
    LAST_RC=$?
    LAST_OUT="$(cat "$out")"

    if [ "$LAST_TIMEOUT" -eq 1 ]; then
        bad "$name (timed out after ${timeout_s}s)"
        printf '        output: %s\n' "$(printf '%s' "$LAST_OUT" | head -3)"
        return 1
    fi
    if [ "$LAST_RC" -ne "$want" ]; then
        bad "$name (exit code $LAST_RC, expected $want)"
        printf '        output: %s\n' "$(printf '%s' "$LAST_OUT" | head -3)"
        return 1
    fi
    ok "$name"
    return 0
}

expect_contains() {
    local name="$1" needle="$2"
    case "$LAST_OUT" in
        *"$needle"*) ok "$name (matched \"$needle\")" ;;
        *) bad "$name (output does not contain \"$needle\")"
           printf '        actual output: %s\n' "$(printf '%s' "$LAST_OUT" | head -5)" ;;
    esac
}

echo "== pyaether CLI smoke test =="
echo "repository root: $ROOT"
echo "isolated home:   $PYAETHER_BRIDGE_HOME"
echo

echo "[1] basic entry points"
run_case "version" 0 10 "$CLI" version
run_case "version --json" 0 10 "$CLI" version --json
run_case "--help" 0 10 "$CLI" --help

echo "[2] subcommand help"
run_case "status --help" 0 10 "$CLI" status --help
run_case "exec --help" 0 10 "$CLI" exec --help
run_case "api --help" 0 10 "$CLI" api --help
run_case "daemon --help" 0 10 "$CLI" daemon --help

echo "[3] api search (catalog missing, expect exit code 1)"
run_case "api search foo" 1 20 "$CLI" api search foo
if [ "$LAST_RC" -eq 1 ]; then
    case "$LAST_OUT" in
        *"api build"*|*"module"*|*"catalog"*) ok "error message offers a next step" ;;
        *) bad "error message lacks an actionable hint" ;;
    esac
fi

echo "[4] status --json (PYAETHER_BRIDGE_NO_AUTOSTART=1; must not hang or start Docker)"
run_case "status --json" 0 30 "$CLI" status --json
if [ "$LAST_RC" -eq 0 ]; then
    case "$LAST_OUT" in
        "{"*) ok "status --json printed JSON" ;;
        *) bad "status --json did not print JSON" ;;
    esac
fi

echo "[5] exec with empty input (expect exit code 1, no daemon started)"
run_case "exec with no input" 1 10 "$CLI" exec </dev/null

echo "[6] --json wire format (compact by default, --debug restores the long form)"
run_case "status --json --debug" 0 30 "$CLI" status --json --debug
DEBUG_OUT="$LAST_OUT"
run_case "status --json (again)" 0 30 "$CLI" status --json
PLAIN_OUT="$LAST_OUT"
if [ -n "$PLAIN_OUT" ] && [ -n "$DEBUG_OUT" ]; then
    case "$PLAIN_OUT" in
        *$'\n'*) bad "default --json is not a single line" ;;
        *) ok "default --json is a single line" ;;
    esac
    if [ "${#PLAIN_OUT}" -lt "${#DEBUG_OUT}" ]; then
        ok "default --json is smaller than --debug"
    else
        bad "default --json is not smaller than --debug"
    fi
fi

echo "[7] protocol.brief() drops diagnostics on success, keeps them on failure"
if PYAETHER_REPO="$ROOT" "$PY" - <<'PYEOF'
import os
import sys

sys.path.insert(0, os.environ["PYAETHER_REPO"])
from pyaether_bridge import protocol

envelope = {
    "ok": True, "status": "SUCCESS", "operation": "info", "data": {"shapes": 10},
    "errors": [], "warnings": [],
    "metadata": {"artifact": "/tmp/a.gds", "work_dir": "/tmp/run-1",
                 "command": "klayout -b -r x.py", "target_reason": "found it"},
}
brief = protocol.brief(envelope)
assert brief["metadata"] == {"artifact": "/tmp/a.gds"}, brief["metadata"]
assert brief["data"] == {"shapes": 10}, brief["data"]

failed = dict(envelope, ok=False, status="FAILURE")
assert protocol.brief(failed)["metadata"] == envelope["metadata"], "failure lost diagnostics"

ready = {"session": {"ready": True, "import_log": "x" * 100, "symbols": 5}}
assert protocol.brief(ready)["session"] == {"ready": True, "symbols": 5}, protocol.brief(ready)

unready = {"session": {"ready": False, "import_log": "boom", "symbols": 0}}
assert protocol.brief(unready)["session"]["import_log"] == "boom", "unready session lost its log"

assert "\n" not in protocol.dumps(envelope), "default output is not compact"
assert "\n" in protocol.dumps(envelope, debug=True), "--debug output is not indented"
assert protocol.brief("not a dict") == "not a dict"
PYEOF
then ok "brief() keeps results and drops diagnostics"
else bad "brief() trimming is wrong"
fi

echo "[8] protocol.summarize() condenses traces without inventing numbers"
if PYAETHER_REPO="$ROOT" "$PY" - <<'PYEOF'
import json
import os
import sys

sys.path.insert(0, os.environ["PYAETHER_REPO"])
from pyaether_bridge import protocol

reply = {"ok": True, "status": "SUCCESS", "operation": "run",
         "data": {"time": [index * 1e-9 for index in range(10000)],
                  "v(out)": [1.0, 2.0, 0.5] + [1.5] * 9997},
         "errors": [], "warnings": [], "metadata": {"plots": [{"points": 10000}]}}

full = protocol.dumps(reply)
short = protocol.dumps(reply, summary=True)
assert len(short) < len(full) // 100, (len(full), len(short))

shaped = json.loads(short)
assert shaped["data"]["v(out)"] == {"n": 10000, "first": 1.0, "last": 1.5,
                                    "min": 0.5, "max": 2.0}, shaped["data"]["v(out)"]
# the plot inventory survives, so the run itself is still described
assert shaped["metadata"]["plots"] == [{"points": 10000}], shaped["metadata"]

# a trace no longer than its own summary keeps its samples
tiny = {"ok": True, "data": {"v(x)": [3.0]}, "metadata": {}}
assert protocol.summarize(tiny)["data"]["v(x)"] == [3.0], protocol.summarize(tiny)

# the simulator banner describes the run, not the result
banner = dict(reply, metadata={"plots": [], "stdout_tail": "ngspice-47 banner"})
assert "stdout_tail" not in protocol.brief(banner)["metadata"], protocol.brief(banner)
assert protocol.brief(dict(banner, ok=False))["metadata"]["stdout_tail"] == "ngspice-47 banner"
PYEOF
then ok "summarize() condenses traces and keeps the inventory"
else bad "summarize() is wrong"
fi

echo
echo "== result: $PASS passed, $FAIL failed =="
[ "$FAIL" -eq 0 ] || exit 1
exit 0
