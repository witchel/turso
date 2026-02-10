#!/usr/bin/env bash
# kernel_profile_dtrace.sh — DTrace backend for kernel_profile.sh
#
# Uses syscall:::entry/return probes for per-syscall timing and
# profile-997 for CPU sampling. Requires sudo (DTrace needs root).
#
# With SIP enabled (default macOS):
#   - syscall:::  WORKS  → per-syscall durations
#   - profile:::  WORKS  → CPU sampling
#   - sched:::    BLOCKED → no thread state data
#
# Environment (set by kernel_profile.sh):
#   KPROF_BENCH_BIN, KPROF_BENCH_FILTER, KPROF_OUTPUT_DIR,
#   KPROF_JSON_DIR, KPROF_ALLOW_SUDO, KPROF_TIME_LIMIT,
#   KPROF_BACKEND, KPROF_PLATFORM

kprof_run() {
    echo "  [dtrace] Starting DTrace profiling..."

    if [ "$KPROF_ALLOW_SUDO" != "true" ]; then
        echo "  [dtrace] Warning: --sudo not specified. DTrace requires root."
        echo "  [dtrace] Writing empty JSON with error."
        _dtrace_write_json "" "" "DTrace requires sudo (--sudo flag not provided)"
        return 0
    fi

    if ! command -v dtrace &>/dev/null; then
        echo "  [dtrace] Error: dtrace not found."
        _dtrace_write_json "" "" "dtrace command not found"
        return 0
    fi

    local d_script="$KPROF_OUTPUT_DIR/kprof_trace.d"
    local raw_output="$KPROF_OUTPUT_DIR/dtrace_raw.txt"
    local cpu_output="$KPROF_OUTPUT_DIR/dtrace_cpu.txt"

    # Generate DTrace script
    _dtrace_gen_script "$d_script"

    echo "  [dtrace] Running: sudo dtrace -s $d_script -c \"$KPROF_BENCH_BIN --bench '$KPROF_BENCH_FILTER'\""
    echo ""

    local start_time
    start_time=$(date +%s)

    # Run DTrace with the benchmark as the traced command
    sudo dtrace -s "$d_script" \
        -c "$KPROF_BENCH_BIN --bench $KPROF_BENCH_FILTER" \
        > "$raw_output" 2>&1 || true

    local end_time
    end_time=$(date +%s)
    local duration=$(( end_time - start_time ))

    echo ""
    echo "  [dtrace] Profiling completed in ${duration}s"
    echo "  [dtrace] Raw output: $raw_output"

    # Parse DTrace output into JSON
    _dtrace_parse_to_json "$raw_output" "$duration"
}

_dtrace_gen_script() {
    local output_file="$1"
    cat > "$output_file" << 'DTRACE_SCRIPT'
/* kernel_profile_dtrace.d — syscall timing + CPU sampling */

/* Track per-syscall entry timestamps (thread-local) */
syscall:::entry
/pid == $target/
{
    self->ts = timestamp;
}

syscall:::return
/pid == $target && self->ts != 0/
{
    @count[probefunc] = count();
    @total[probefunc] = sum(timestamp - self->ts);
    @min_t[probefunc] = min(timestamp - self->ts);
    @max_t[probefunc] = max(timestamp - self->ts);
    self->ts = 0;
}

/* CPU sampling at ~997 Hz (prime to avoid aliasing) */
profile-997
/pid == $target/
{
    @cpu_samples = count();
}

END
{
    printf("\n=== SYSCALL_COUNTS ===\n");
    printa("SC_COUNT %s %@d\n", @count);

    printf("\n=== SYSCALL_TOTAL_NS ===\n");
    printa("SC_TOTAL %s %@d\n", @total);

    printf("\n=== SYSCALL_MIN_NS ===\n");
    printa("SC_MIN %s %@d\n", @min_t);

    printf("\n=== SYSCALL_MAX_NS ===\n");
    printa("SC_MAX %s %@d\n", @max_t);

    printf("\n=== CPU_SAMPLES ===\n");
    printa("CPU_TOTAL %@d\n", @cpu_samples);
}
DTRACE_SCRIPT
    echo "  [dtrace] Generated D script: $output_file"
}

_dtrace_parse_to_json() {
    local raw_file="$1"
    local duration="$2"

    if [ ! -f "$raw_file" ]; then
        _dtrace_write_json "" "" "DTrace produced no output"
        return
    fi

    # Use awk to parse the structured DTrace output into JSON
    local json
    json=$(awk '
    BEGIN {
        n = 0
        cpu_total = 0
        in_section = ""
    }

    /=== SYSCALL_COUNTS ===/ { in_section = "count"; next }
    /=== SYSCALL_TOTAL_NS ===/ { in_section = "total"; next }
    /=== SYSCALL_MIN_NS ===/ { in_section = "min"; next }
    /=== SYSCALL_MAX_NS ===/ { in_section = "max"; next }
    /=== CPU_SAMPLES ===/ { in_section = "cpu"; next }

    in_section == "count" && /^SC_COUNT / {
        name = $2
        if (!(name in syscalls)) {
            syscalls[name] = 1
            order[n++] = name
        }
        sc_count[name] = $3
    }

    in_section == "total" && /^SC_TOTAL / {
        sc_total[$2] = $3
    }

    in_section == "min" && /^SC_MIN / {
        sc_min[$2] = $3
    }

    in_section == "max" && /^SC_MAX / {
        sc_max[$2] = $3
    }

    in_section == "cpu" && /^CPU_TOTAL / {
        cpu_total = $2
    }

    END {
        # Build syscalls JSON object
        syscalls_json = ""
        for (i = 0; i < n; i++) {
            name = order[i]
            count_v = sc_count[name] + 0
            total_ns = sc_total[name] + 0
            min_ns = sc_min[name] + 0
            max_ns = sc_max[name] + 0
            total_us = total_ns / 1000.0
            min_us = min_ns / 1000.0
            max_us = max_ns / 1000.0
            avg_us = (count_v > 0) ? total_us / count_v : 0

            if (i > 0) syscalls_json = syscalls_json ","
            syscalls_json = syscalls_json sprintf("\n      \"%s\": {\"count\": %d, \"total_us\": %.1f, \"min_us\": %.2f, \"max_us\": %.2f, \"avg_us\": %.2f}", \
                name, count_v, total_us, min_us, max_us, avg_us)
        }

        if (n == 0) {
            printf "SYSCALLS_EMPTY"
        } else {
            printf "{%s\n    }", syscalls_json
        }
        printf "\n"
        printf "CPU_TOTAL=%d\n", cpu_total
    }
    ' "$raw_file")

    local syscalls_block
    local cpu_samples

    syscalls_block=$(echo "$json" | head -1)
    cpu_samples=$(echo "$json" | grep '^CPU_TOTAL=' | cut -d= -f2)
    cpu_samples=${cpu_samples:-0}

    # Determine limitations
    local limitations='["thread_states unavailable (SIP blocks sched provider on macOS)"]'

    local errors="[]"
    if [ "$syscalls_block" = "SYSCALLS_EMPTY" ]; then
        syscalls_block="null"
        errors='["DTrace produced no syscall data — process may have exited too quickly"]'
    fi

    # Write final JSON
    local json_out="$KPROF_JSON_DIR/kernel_profile.json"
    cat > "$json_out" << EOF
{
  "schema_version": 1,
  "backend": "dtrace",
  "platform": "$KPROF_PLATFORM",
  "bench_filter": "$KPROF_BENCH_FILTER",
  "duration_s": $duration,
  "errors": $errors,
  "limitations": $limitations,
  "syscalls": $syscalls_block,
  "thread_states": null,
  "cpu_samples": {
    "total_samples": $cpu_samples,
    "sample_rate_hz": 997
  }
}
EOF
    echo "  [dtrace] JSON written to $json_out"
}

_dtrace_write_json() {
    local syscalls="${1:-null}"
    local cpu_samples="${2:-0}"
    local error_msg="${3:-}"

    local errors="[]"
    if [ -n "$error_msg" ]; then
        errors="[\"$error_msg\"]"
    fi

    local json_out="$KPROF_JSON_DIR/kernel_profile.json"
    cat > "$json_out" << EOF
{
  "schema_version": 1,
  "backend": "dtrace",
  "platform": "$KPROF_PLATFORM",
  "bench_filter": "$KPROF_BENCH_FILTER",
  "duration_s": 0,
  "errors": $errors,
  "limitations": ["DTrace requires sudo privileges"],
  "syscalls": null,
  "thread_states": null,
  "cpu_samples": null
}
EOF
    echo "  [dtrace] JSON written to $json_out"
}
