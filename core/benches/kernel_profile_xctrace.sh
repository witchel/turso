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

SCRIPT_DIR="${SCRIPT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"

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

    # Step 1: Export table of contents to discover available schemas
    local toc_xml="$KPROF_OUTPUT_DIR/xctrace_toc.xml"
    local export_ok=false

    if xcrun xctrace export --input "$trace_file" --toc --output "$toc_xml" 2>/dev/null; then
        echo "  [xctrace] TOC exported: $toc_xml"

        # Step 2: Export thread-state and syscall tables (both present in System Trace)
        local schemas_found=()
        for schema in "thread-state" "syscall" "context-switch" "cpu-state"; do
            if grep -q "\"$schema\"" "$toc_xml" 2>/dev/null; then
                schemas_found+=("$schema")
                echo "  [xctrace] Found schema: $schema"
            fi
        done

        # Export thread-state table (primary target)
        local thread_state_xml="$KPROF_OUTPUT_DIR/xctrace_thread_state.xml"
        local syscall_xml="$KPROF_OUTPUT_DIR/xctrace_syscall.xml"

        for schema in "${schemas_found[@]}"; do
            local xpath="/trace-toc/run[@number=\"1\"]/data/table[@schema=\"${schema}\"]"
            local out_file="$KPROF_OUTPUT_DIR/xctrace_${schema}.xml"
            echo "  [xctrace] Exporting schema: $schema ..."
            if xcrun xctrace export --input "$trace_file" --xpath "$xpath" --output "$out_file" 2>/dev/null; then
                echo "  [xctrace] Exported: $out_file ($(wc -c < "$out_file") bytes)"
                export_ok=true
            else
                echo "  [xctrace] XPath export failed for: $schema"
            fi
        done

        # Use thread-state XML as the primary export for parsing
        if [ -f "$thread_state_xml" ]; then
            cp "$thread_state_xml" "$export_xml"
        elif [ -f "$syscall_xml" ]; then
            cp "$syscall_xml" "$export_xml"
        fi
    else
        echo "  [xctrace] TOC export failed"
        errors='["xctrace export failed — manual analysis in Instruments.app recommended"]'
    fi

    # Parse exported thread-state XML using Python streaming parser
    local parsed_json="$KPROF_OUTPUT_DIR/xctrace_thread_states_parsed.json"
    local thread_state_file="$KPROF_OUTPUT_DIR/xctrace_thread-state.xml"

    if [ "$export_ok" = "true" ] && [ -f "$thread_state_file" ]; then
        echo "  [xctrace] Parsing thread-state XML ($(du -h "$thread_state_file" | cut -f1) )..."
        local python="${BENCH_PYTHON:-python3}"
        if $python "$SCRIPT_DIR/parse_xctrace_xml.py" "$thread_state_file" "$parsed_json" 2>&1; then
            # Read the parsed JSON as thread_states value
            if [ -f "$parsed_json" ] && ! grep -q '"error"' "$parsed_json" 2>/dev/null; then
                thread_states=$(cat "$parsed_json")
            else
                echo "  [xctrace] Parser found no matching process data"
                limitations='["Thread state data could not be extracted — benchmark process not found in trace"]'
            fi
        else
            echo "  [xctrace] Python parser failed"
            limitations='["Thread state XML parsing failed — open trace in Instruments.app for full analysis"]'
        fi
    elif [ "$export_ok" = "true" ]; then
        limitations='["thread-state table not found in trace export"]'
    else
        if [ -z "${errors:-}" ] || [ "$errors" = "[]" ]; then
            limitations='["Trace export unavailable — open trace in Instruments.app for thread state analysis"]'
        fi
    fi

    # Write final JSON
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
