#!/usr/bin/env bash
# kernel_profile_perf.sh — Linux perf backend for kernel_profile.sh
#
# Two-pass profiling:
#   1. perf stat   — aggregate counters (task-clock, context-switches, etc.)
#   2. perf record — detailed sched + syscall events → perf script → JSON
#
# Linux perf can provide thread_states via sched:sched_switch tracepoints
# (not blocked by SIP as on macOS).
#
# Environment (set by kernel_profile.sh):
#   KPROF_BENCH_BIN, KPROF_BENCH_FILTER, KPROF_OUTPUT_DIR,
#   KPROF_JSON_DIR, KPROF_ALLOW_SUDO, KPROF_TIME_LIMIT,
#   KPROF_BACKEND, KPROF_PLATFORM

kprof_run() {
    echo "  [perf] Starting perf profiling..."

    if ! command -v perf &>/dev/null; then
        echo "  [perf] Error: perf not found."
        _perf_write_json "perf command not found"
        return 0
    fi

    local stat_output="$KPROF_OUTPUT_DIR/perf_stat.txt"
    local record_output="$KPROF_OUTPUT_DIR/perf.data"
    local script_output="$KPROF_OUTPUT_DIR/perf_script.txt"

    local sudo_prefix=""
    if [ "$KPROF_ALLOW_SUDO" = "true" ]; then
        sudo_prefix="sudo"
    fi

    local start_time
    start_time=$(date +%s)

    # ── Pass 1: perf stat for aggregate counters ──
    echo "  [perf] Pass 1: perf stat..."
    $sudo_prefix perf stat \
        -e task-clock,context-switches,cpu-migrations,page-faults,cycles,instructions \
        -o "$stat_output" \
        -- "$KPROF_BENCH_BIN" --bench "$KPROF_BENCH_FILTER" 2>&1 || true

    # ── Pass 2: perf record for syscall + sched events ──
    echo "  [perf] Pass 2: perf record (syscalls + sched)..."
    local events="syscalls:sys_enter_write,syscalls:sys_exit_write"
    events="$events,syscalls:sys_enter_fsync,syscalls:sys_exit_fsync"
    events="$events,syscalls:sys_enter_pwrite64,syscalls:sys_exit_pwrite64"
    events="$events,syscalls:sys_enter_pwritev,syscalls:sys_exit_pwritev"
    events="$events,syscalls:sys_enter_fdatasync,syscalls:sys_exit_fdatasync"

    # Try adding sched events (may fail without permissions)
    local sched_available=false
    if $sudo_prefix perf record -e sched:sched_switch -a -- sleep 0.01 &>/dev/null 2>&1; then
        events="$events,sched:sched_switch"
        sched_available=true
        echo "  [perf] sched:sched_switch available"
    else
        echo "  [perf] sched:sched_switch not available (insufficient permissions)"
    fi

    $sudo_prefix perf record \
        -e "$events" \
        -o "$record_output" \
        -- "$KPROF_BENCH_BIN" --bench "$KPROF_BENCH_FILTER" 2>&1 || true

    # Convert to text
    if [ -f "$record_output" ]; then
        $sudo_prefix perf script -i "$record_output" > "$script_output" 2>/dev/null || true
    fi

    local end_time
    end_time=$(date +%s)
    local duration=$(( end_time - start_time ))

    echo "  [perf] Profiling completed in ${duration}s"

    # ── Parse into JSON ──
    _perf_parse_to_json "$stat_output" "$script_output" "$duration" "$sched_available"
}

_perf_parse_to_json() {
    local stat_file="$1"
    local script_file="$2"
    local duration="$3"
    local sched_available="$4"

    local errors="[]"
    local limitations="[]"

    if [ "$sched_available" != "true" ]; then
        limitations='["thread_states unavailable (sched:sched_switch requires root or perf_event_paranoid <= 1)"]'
    fi

    # Parse perf script output for syscall timing
    local syscalls_json="null"
    if [ -f "$script_file" ] && [ -s "$script_file" ]; then
        syscalls_json=$(_perf_parse_syscalls "$script_file")
    fi

    # Parse perf stat for CPU counters
    local context_switches=0
    local cpu_migrations=0
    local task_clock_ms=0
    if [ -f "$stat_file" ]; then
        context_switches=$(grep -oP '[\d,]+(?=\s+context-switches)' "$stat_file" | tr -d ',' || echo "0")
        cpu_migrations=$(grep -oP '[\d,]+(?=\s+cpu-migrations)' "$stat_file" | tr -d ',' || echo "0")
        task_clock_ms=$(grep -oP '[\d,.]+(?=\s+msec task-clock)' "$stat_file" | tr -d ',' || echo "0")
    fi

    # Thread states from sched:sched_switch (if available)
    local thread_states="null"
    if [ "$sched_available" = "true" ] && [ -f "$script_file" ]; then
        thread_states=$(_perf_parse_thread_states "$script_file")
    fi

    local json_out="$KPROF_JSON_DIR/kernel_profile.json"
    cat > "$json_out" << EOF
{
  "schema_version": 1,
  "backend": "perf",
  "platform": "$KPROF_PLATFORM",
  "bench_filter": "$KPROF_BENCH_FILTER",
  "duration_s": $duration,
  "errors": $errors,
  "limitations": $limitations,
  "syscalls": $syscalls_json,
  "thread_states": $thread_states,
  "cpu_samples": {
    "total_samples": 0,
    "sample_rate_hz": 0,
    "context_switches": $context_switches,
    "cpu_migrations": $cpu_migrations,
    "task_clock_ms": $task_clock_ms
  }
}
EOF
    echo "  [perf] JSON written to $json_out"
}

_perf_parse_syscalls() {
    local script_file="$1"

    # Parse paired sys_enter/sys_exit events from perf script output.
    # perf script format:
    #   bench_binary TID [CPU] timestamp: syscalls:sys_enter_pwrite64: ...
    #   bench_binary TID [CPU] timestamp: syscalls:sys_exit_pwrite64:  ...
    #
    # We match enter/exit pairs by TID and syscall name to compute durations.
    awk '
    /sys_enter_/ {
        # Extract TID, timestamp, and syscall name
        tid = $2
        ts = $4
        sub(/:$/, "", ts)
        match($0, /sys_enter_([a-z0-9_]+)/, m)
        if (m[1] != "") {
            name = m[1]
            enter_ts[tid ":" name] = ts + 0.0
        }
        next
    }

    /sys_exit_/ {
        tid = $2
        ts = $4
        sub(/:$/, "", ts)
        match($0, /sys_exit_([a-z0-9_]+)/, m)
        if (m[1] != "") {
            name = m[1]
            key = tid ":" name
            if (key in enter_ts) {
                dur_us = (ts - enter_ts[key]) * 1000000.0
                if (dur_us >= 0) {
                    sc_count[name]++
                    sc_total[name] += dur_us
                    if (!(name in sc_min) || dur_us < sc_min[name]) sc_min[name] = dur_us
                    if (!(name in sc_max) || dur_us > sc_max[name]) sc_max[name] = dur_us
                    if (!(name in order)) {
                        order[name] = n++
                        names[order[name]] = name
                    }
                }
                delete enter_ts[key]
            }
        }
        next
    }

    END {
        if (n == 0) {
            printf "null"
            exit
        }
        printf "{"
        for (i = 0; i < n; i++) {
            name = names[i]
            count_v = sc_count[name]
            total = sc_total[name]
            mn = sc_min[name]
            mx = sc_max[name]
            avg = (count_v > 0) ? total / count_v : 0
            if (i > 0) printf ","
            printf "\n      \"%s\": {\"count\": %d, \"total_us\": %.1f, \"min_us\": %.2f, \"max_us\": %.2f, \"avg_us\": %.2f}", \
                name, count_v, total, mn, mx, avg
        }
        printf "\n    }"
    }
    ' "$script_file"
}

_perf_parse_thread_states() {
    local script_file="$1"

    # Parse sched:sched_switch events to estimate thread running/blocked time.
    # This is a simplified view — a full analysis would need per-CPU state tracking.
    # For now, count context switches and extract prev_state information.
    local switch_count
    switch_count=$(grep -c 'sched:sched_switch' "$script_file" 2>/dev/null || echo "0")

    if [ "$switch_count" -eq 0 ]; then
        echo "null"
        return
    fi

    cat << EOF
{
      "context_switches": $switch_count,
      "note": "Detailed per-thread state breakdown available via perf script analysis"
    }
EOF
}

_perf_write_json() {
    local error_msg="${1:-}"

    local errors="[]"
    if [ -n "$error_msg" ]; then
        errors="[\"$error_msg\"]"
    fi

    local json_out="$KPROF_JSON_DIR/kernel_profile.json"
    cat > "$json_out" << EOF
{
  "schema_version": 1,
  "backend": "perf",
  "platform": "$KPROF_PLATFORM",
  "bench_filter": "$KPROF_BENCH_FILTER",
  "duration_s": 0,
  "errors": $errors,
  "limitations": ["perf not available"],
  "syscalls": null,
  "thread_states": null,
  "cpu_samples": null
}
EOF
    echo "  [perf] JSON written to $json_out"
}
