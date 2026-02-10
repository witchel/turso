#!/usr/bin/env bash
# kernel_profile_xctrace.sh — macOS Instruments (xctrace) backend for kernel_profile.sh
#
# Records a System Trace via `xcrun xctrace record`, then attempts to
# export thread state data via `xcrun xctrace export` for automated analysis.
#
# This is the richest macOS backend: provides thread states (running/blocked/
# preempted), context switches, and backtraces at blocking points.
#
# Falls back to reporting just the trace file path in JSON if export fails.
#
# Environment (set by kernel_profile.sh):
#   KPROF_BENCH_BIN, KPROF_BENCH_FILTER, KPROF_OUTPUT_DIR,
#   KPROF_JSON_DIR, KPROF_ALLOW_SUDO, KPROF_TIME_LIMIT,
#   KPROF_BACKEND, KPROF_PLATFORM

kprof_run() {
    echo "  [xctrace] Starting Instruments System Trace..."

    if ! command -v xcrun &>/dev/null; then
        echo "  [xctrace] Error: xcrun not found. Requires macOS with Xcode."
        _xctrace_write_json "" "xcrun not found — Xcode required"
        return 0
    fi

    local trace_file="$KPROF_OUTPUT_DIR/trace_$(date +%Y%m%d_%H%M%S).trace"

    echo "  [xctrace] Recording trace for: $KPROF_BENCH_FILTER"
    echo "  [xctrace] Output: $trace_file"
    echo "  [xctrace] Time limit: ${KPROF_TIME_LIMIT}s"
    echo ""

    local start_time
    start_time=$(date +%s)

    # Record System Trace
    xcrun xctrace record \
        --template 'System Trace' \
        --output "$trace_file" \
        --time-limit "${KPROF_TIME_LIMIT}s" \
        --launch -- "$KPROF_BENCH_BIN" --bench "$KPROF_BENCH_FILTER" || true

    local end_time
    end_time=$(date +%s)
    local duration=$(( end_time - start_time ))

    echo ""
    echo "  [xctrace] Trace recorded in ${duration}s"
    echo "  [xctrace] Trace file: $trace_file"

    if [ ! -e "$trace_file" ]; then
        echo "  [xctrace] Warning: trace file not created"
        _xctrace_write_json "" "xctrace record produced no output"
        return 0
    fi

    # Attempt to export and parse thread state data
    _xctrace_export_and_parse "$trace_file" "$duration"
}

_xctrace_export_and_parse() {
    local trace_file="$1"
    local duration="$2"

    local export_xml="$KPROF_OUTPUT_DIR/xctrace_export.xml"
    local thread_states="null"
    local errors="[]"
    local limitations="[]"

    echo "  [xctrace] Attempting to export trace data..."

    # Try to export the trace as XML for automated parsing
    # xctrace export supports --xpath to select specific tables
    local export_ok=false
    if xcrun xctrace export --input "$trace_file" --output "$export_xml" 2>/dev/null; then
        export_ok=true
        echo "  [xctrace] Export successful: $export_xml"
    else
        echo "  [xctrace] Export failed (this is common — trace may need Instruments.app)"
        errors='["xctrace export failed — manual analysis in Instruments.app recommended"]'
    fi

    # Parse exported XML for thread state summary
    if [ "$export_ok" = "true" ] && [ -f "$export_xml" ]; then
        thread_states=$(_xctrace_parse_thread_states "$export_xml")
        if [ "$thread_states" = "null" ]; then
            limitations='["Thread state data could not be extracted from export — open trace in Instruments.app for full analysis"]'
        fi
    else
        limitations='["Trace export unavailable — open trace in Instruments.app for thread state analysis"]'
    fi

    # Write JSON
    local json_out="$KPROF_JSON_DIR/kernel_profile.json"
    cat > "$json_out" << EOF
{
  "schema_version": 1,
  "backend": "xctrace",
  "platform": "$KPROF_PLATFORM",
  "bench_filter": "$KPROF_BENCH_FILTER",
  "duration_s": $duration,
  "errors": $errors,
  "limitations": $limitations,
  "syscalls": null,
  "thread_states": $thread_states,
  "cpu_samples": null,
  "trace_file": "$trace_file"
}
EOF
    echo "  [xctrace] JSON written to $json_out"

    # Print instructions for manual analysis
    echo ""
    echo "  [xctrace] To analyze in Instruments:"
    echo "    open $trace_file"
    echo ""
    echo "  In Instruments, look for:"
    echo "    - Thread States: shows running/blocked/preempted per thread"
    echo "    - Backtraces at blocking points (write_lock.write() and fsync)"
    echo "    - Context switch frequency and reasons"
}

_xctrace_parse_thread_states() {
    local xml_file="$1"

    # The xctrace export XML structure varies by Xcode version.
    # We look for thread scheduling data in the export.
    #
    # The export typically contains a TOC (table of contents) at the top level.
    # Thread state data is in tables like "thread-state" or within
    # "thread-scheduling" schemas.
    #
    # If we can't find structured thread data, we at least try to extract
    # basic info like process name and run counts.

    if [ ! -f "$xml_file" ] || [ ! -s "$xml_file" ]; then
        echo "null"
        return
    fi

    # Check if the export contains useful table references
    local has_tables
    has_tables=$(grep -c '<table\|<schema\|<row' "$xml_file" 2>/dev/null || echo "0")

    if [ "$has_tables" -eq 0 ]; then
        # Export may just be a TOC — no detailed data
        echo "null"
        return
    fi

    # Try to extract thread scheduling summary using awk
    # The XML format from xctrace export is not fully standardized,
    # so we do best-effort extraction.
    local result
    result=$(awk '
    BEGIN {
        running = 0; blocked = 0; preempted = 0; total = 0
        found_data = 0
    }

    # Look for thread-state or scheduling rows
    /<row>/ { in_row = 1; next }
    /<\/row>/ { in_row = 0; next }

    # Match state values in various formats xctrace may emit
    in_row && /[Rr]unning/ { running++; total++; found_data = 1 }
    in_row && /[Bb]locked/ { blocked++; total++; found_data = 1 }
    in_row && /[Pp]reempted/ { preempted++; total++; found_data = 1 }
    in_row && /[Ww]aiting/ { blocked++; total++; found_data = 1 }

    END {
        if (!found_data || total == 0) {
            printf "null"
        } else {
            printf "{\n"
            printf "      \"running_intervals\": %d,\n", running
            printf "      \"blocked_intervals\": %d,\n", blocked
            printf "      \"preempted_intervals\": %d,\n", preempted
            printf "      \"total_intervals\": %d\n", total
            printf "    }"
        }
    }
    ' "$xml_file")

    echo "$result"
}

_xctrace_write_json() {
    local trace_file="${1:-}"
    local error_msg="${2:-}"

    local errors="[]"
    if [ -n "$error_msg" ]; then
        errors="[\"$error_msg\"]"
    fi

    local trace_field=""
    if [ -n "$trace_file" ]; then
        trace_field="\"trace_file\": \"$trace_file\","
    fi

    local json_out="$KPROF_JSON_DIR/kernel_profile.json"
    cat > "$json_out" << EOF
{
  "schema_version": 1,
  "backend": "xctrace",
  "platform": "$KPROF_PLATFORM",
  "bench_filter": "$KPROF_BENCH_FILTER",
  "duration_s": 0,
  "errors": $errors,
  "limitations": ["xctrace not available"],
  "syscalls": null,
  "thread_states": null,
  "cpu_samples": null
}
EOF
    echo "  [xctrace] JSON written to $json_out"
}
