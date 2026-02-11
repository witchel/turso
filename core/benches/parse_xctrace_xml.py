#!/usr/bin/env python3
"""Parse xctrace thread-state XML export into summary JSON.

Usage:
    python3 parse_xctrace_xml.py <thread-state.xml> <output.json> [process-name-prefix]

Reads the XML exported by `xctrace export --xpath '...thread-state...'`,
filters for the benchmark process, and produces a JSON summary of thread
state durations.

Uses iterparse (streaming) to handle the 100+ MB XML files that System
Trace produces without loading the entire DOM into memory.
"""

import json
import sys
import xml.etree.ElementTree as ET


def parse_thread_state_xml(xml_path, process_prefix="parallel_write_benchmark"):
    """Parse xctrace thread-state XML using streaming iterparse."""

    # xctrace XML uses id/ref deduplication:
    #   <thread-state id="7" fmt="Running">Running</thread-state>  -- defines id 7
    #   <thread-state ref="7"/>                                     -- references id 7
    #
    # Each <row> contains child elements for: thread, thread-state, duration, process.
    # Some children are <sentinel/> (null). We accumulate state durations for rows
    # matching our benchmark process.

    id_map = {}             # id -> fmt value (for ref lookups)
    bench_pids = set()      # process id values for our benchmark
    state_durations = {}    # state_name -> total nanoseconds
    state_counts = {}       # state_name -> count of intervals
    total_intervals = 0

    # Current row state (reset on each <row>)
    in_row = False
    row_state = None
    row_duration = 0
    row_process_id = None

    for event, elem in ET.iterparse(xml_path, events=("start", "end")):
        tag = elem.tag

        # Build id->fmt map from any element with an id attribute
        if event == "start":
            eid = elem.get("id")
            if eid:
                fmt = elem.get("fmt", elem.text or "")
                id_map[eid] = fmt

            # Identify benchmark process elements by name prefix
            if tag == "process":
                fmt = elem.get("fmt", "")
                pid_id = elem.get("id")
                if process_prefix in fmt and pid_id:
                    bench_pids.add(pid_id)

        if event == "start" and tag == "row":
            in_row = True
            row_state = None
            row_duration = 0
            row_process_id = None
            continue

        if event == "end" and tag == "row":
            # Process completed row
            if row_process_id in bench_pids and row_state and row_duration > 0:
                state_durations[row_state] = state_durations.get(row_state, 0) + row_duration
                state_counts[row_state] = state_counts.get(row_state, 0) + 1
                total_intervals += 1
            in_row = False
            # Free memory — don't keep row elements
            elem.clear()
            continue

        if not in_row:
            continue

        # Inside a row — extract fields from child elements on "end" event
        if event == "end":
            if tag == "thread-state":
                ref = elem.get("ref")
                eid = elem.get("id")
                if ref:
                    row_state = id_map.get(ref, "Unknown")
                elif eid:
                    row_state = elem.get("fmt", elem.text or "Unknown")

            elif tag == "duration":
                ref = elem.get("ref")
                eid = elem.get("id")
                if ref and ref in id_map:
                    try:
                        row_duration = int(id_map[ref])
                    except (ValueError, TypeError):
                        pass
                elif eid:
                    try:
                        row_duration = int(elem.text or "0")
                    except (ValueError, TypeError):
                        pass

            elif tag == "process":
                ref = elem.get("ref")
                pid_id = elem.get("id")
                if ref:
                    row_process_id = ref
                elif pid_id:
                    row_process_id = pid_id

    if not bench_pids:
        return {"error": f"No process matching '{process_prefix}' found in trace"}

    # Build result
    result = {
        "total_intervals": total_intervals,
        "process_ids": sorted(bench_pids),
    }

    for state, total_ns in sorted(state_durations.items(), key=lambda x: -x[1]):
        key = state.lower().replace(" ", "_")
        result[f"{key}_ns"] = total_ns
        result[f"{key}_ms"] = round(total_ns / 1e6, 2)
        result[f"{key}_intervals"] = state_counts.get(state, 0)

    return result


def main():
    if len(sys.argv) < 3:
        print(f"Usage: {sys.argv[0]} <input.xml> <output.json> [process-prefix]", file=sys.stderr)
        sys.exit(1)

    xml_path = sys.argv[1]
    json_path = sys.argv[2]
    prefix = sys.argv[3] if len(sys.argv) > 3 else "parallel_write_benchmark"

    result = parse_thread_state_xml(xml_path, prefix)

    with open(json_path, "w") as f:
        json.dump(result, f, indent=2)

    # Print summary to stdout
    if "error" in result:
        print(f"  [xctrace-parser] Error: {result['error']}")
    else:
        print(f"  [xctrace-parser] Parsed {result['total_intervals']} intervals for process")
        for key, val in result.items():
            if key.endswith("_ms"):
                state = key.replace("_ms", "")
                intervals = result.get(f"{state}_intervals", 0)
                print(f"    {state}: {val} ms ({intervals} intervals)")


if __name__ == "__main__":
    main()
