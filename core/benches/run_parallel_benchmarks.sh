#!/usr/bin/env bash
# Run parallel write benchmarks 5 times and generate summary plots.
#
# Usage:
#   bash core/benches/run_parallel_benchmarks.sh
#   bash core/benches/run_parallel_benchmarks.sh --trace
#   DISABLE_RUSQLITE_BENCHMARK=1 bash core/benches/run_parallel_benchmarks.sh
#
# Results are saved to target/criterion/run_N/ and plots to
# target/criterion/summary_plots/.
#
# Options:
#   --trace   After benchmark runs, record a System Trace of a single
#             benchmark using macOS Instruments (xctrace). The trace file
#             is saved to target/bench_traces/ and can be opened in
#             Instruments.app for thread-level blocking analysis.

set -euo pipefail

NUM_RUNS=${NUM_RUNS:-5}
FEATURES="--features lock_metrics"
DO_TRACE=false

for arg in "$@"; do
    case "$arg" in
        --trace) DO_TRACE=true ;;
        *) echo "Unknown argument: $arg"; exit 1 ;;
    esac
done

BENCH_GROUPS=(
    "Disjoint Key Scalability"
    "Hot Row Contention"
    "Overlapping Key Insert"
    "Single Row Increment"
    "Writer Heavy Mixed"
)

echo "=== Parallel Write Benchmarks: ${NUM_RUNS} runs (with lock_metrics) ==="
echo ""

for i in $(seq 1 "$NUM_RUNS"); do
    echo "--- Run ${i}/${NUM_RUNS} ---"
    cargo bench --bench parallel_write_benchmark $FEATURES

    # Copy Criterion results
    run_dir="target/criterion/run_${i}"
    mkdir -p "$run_dir"
    for group in "${BENCH_GROUPS[@]}"; do
        src="target/criterion/${group}"
        if [ -d "$src" ]; then
            cp -r "$src" "$run_dir/"
        fi
    done

    # Copy retry sidecar data (includes lock metrics JSONs)
    retries_run_dir="target/bench_retries/run_${i}"
    mkdir -p "$retries_run_dir"
    for group in "${BENCH_GROUPS[@]}"; do
        src="target/bench_retries/${group}"
        if [ -d "$src" ]; then
            cp -r "$src" "$retries_run_dir/"
        fi
    done

    echo "  Results saved to ${run_dir}/"
    echo ""
done

echo "=== Generating plots ==="
PYTHON="${BENCH_PYTHON:-python3}"
"$PYTHON" core/benches/plot_parallel_benchmarks.py --runs "$NUM_RUNS"

echo "=== Generating HTML report ==="
"$PYTHON" core/benches/generate_report.py

echo ""
echo "Done! Plots and report in target/criterion/summary_plots/"

# ---------------------------------------------------------------------------
# Optional: Kernel profiling (platform-abstracted)
# ---------------------------------------------------------------------------

if [ "$DO_TRACE" = true ]; then
    echo ""
    KPROF_ARGS=(--bench-filter "${BENCH_FILTER:-Single Row Increment/turso_mvcc/12}"
                --output-dir target/bench_traces --json-dir target/bench_retries)
    [ "${KPROF_SUDO:-false}" = true ] && KPROF_ARGS+=(--sudo)
    bash core/benches/kernel_profile.sh "${KPROF_ARGS[@]}"
fi
