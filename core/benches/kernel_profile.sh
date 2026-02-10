#!/usr/bin/env bash
# kernel_profile.sh — Platform-abstracted kernel profiling dispatcher.
#
# Detects the platform and available tools, then delegates to a backend
# script that runs a benchmark under a profiling tool and produces
# common JSON output.
#
# Usage:
#   bash core/benches/kernel_profile.sh \
#     --bench-filter "Single Row Increment/turso_mvcc/12" \
#     --output-dir target/bench_traces \
#     --json-dir target/bench_retries \
#     --backend auto \
#     --sudo \
#     --time-limit 60
#
# Backends:
#   auto     — auto-detect (Darwin: xctrace > dtrace; Linux: perf)
#   xctrace  — macOS Instruments System Trace (requires Xcode)
#   dtrace   — macOS/Solaris DTrace (syscall + profile probes)
#   perf     — Linux perf_events (perf stat + perf record)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Defaults ──────────────────────────────────────────────────────────
BENCH_FILTER="${BENCH_FILTER:-Single Row Increment/turso_mvcc/12}"
OUTPUT_DIR="target/bench_traces"
JSON_DIR="target/bench_retries"
BACKEND="auto"
ALLOW_SUDO=false
TIME_LIMIT=60
FEATURES="--features lock_metrics"

# ── Argument parsing ──────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --bench-filter) BENCH_FILTER="$2"; shift 2 ;;
        --output-dir)   OUTPUT_DIR="$2"; shift 2 ;;
        --json-dir)     JSON_DIR="$2"; shift 2 ;;
        --backend)      BACKEND="$2"; shift 2 ;;
        --sudo)         ALLOW_SUDO=true; shift ;;
        --time-limit)   TIME_LIMIT="$2"; shift 2 ;;
        *) echo "kernel_profile.sh: unknown argument: $1"; exit 1 ;;
    esac
done

# ── Build benchmark binary ────────────────────────────────────────────
echo "=== Kernel Profiling ==="
echo "  Filter:  $BENCH_FILTER"
echo "  Backend: $BACKEND"
echo ""

echo "  Building benchmark binary..."
BENCH_BIN=$(cargo bench --bench parallel_write_benchmark $FEATURES --no-run --message-format=json 2>/dev/null \
    | jq -r 'select(.executable != null) | .executable' | head -1)

if [ -z "$BENCH_BIN" ]; then
    echo "Error: could not determine benchmark binary path"
    exit 1
fi
echo "  Binary: $BENCH_BIN"

# ── Export environment for backends ───────────────────────────────────
export KPROF_BENCH_BIN="$BENCH_BIN"
export KPROF_BENCH_FILTER="$BENCH_FILTER"
export KPROF_OUTPUT_DIR="$OUTPUT_DIR"
export KPROF_JSON_DIR="$JSON_DIR"
export KPROF_ALLOW_SUDO="$ALLOW_SUDO"
export KPROF_TIME_LIMIT="$TIME_LIMIT"

mkdir -p "$KPROF_OUTPUT_DIR" "$KPROF_JSON_DIR"

# ── Auto-detect backend ──────────────────────────────────────────────
detect_backend() {
    local platform
    platform="$(uname -s)"

    case "$platform" in
        Darwin)
            # Prefer xctrace (richer data) if Xcode is installed
            if command -v xcrun &>/dev/null && xcrun xctrace version &>/dev/null 2>&1; then
                echo "xctrace"
            elif command -v dtrace &>/dev/null; then
                echo "dtrace"
            else
                echo "none"
            fi
            ;;
        Linux)
            if command -v perf &>/dev/null; then
                echo "perf"
            else
                echo "none"
            fi
            ;;
        *)
            echo "none"
            ;;
    esac
}

if [ "$BACKEND" = "auto" ]; then
    BACKEND=$(detect_backend)
    echo "  Auto-detected backend: $BACKEND"
fi

export KPROF_BACKEND="$BACKEND"
export KPROF_PLATFORM="$(uname -s | tr '[:upper:]' '[:lower:]')"

# ── Dispatch to backend ──────────────────────────────────────────────
BACKEND_SCRIPT="$SCRIPT_DIR/kernel_profile_${BACKEND}.sh"

if [ "$BACKEND" = "none" ]; then
    echo "  Warning: no profiling tools available on this platform."
    echo "  Skipping kernel profiling (exit 0)."
    exit 0
fi

if [ ! -f "$BACKEND_SCRIPT" ]; then
    echo "  Error: backend script not found: $BACKEND_SCRIPT"
    exit 1
fi

echo ""
# Source backend and call its entry point
# shellcheck source=/dev/null
source "$BACKEND_SCRIPT"
kprof_run

echo ""
echo "  JSON output: $KPROF_JSON_DIR/kernel_profile.json"
echo "=== Kernel Profiling Complete ==="
