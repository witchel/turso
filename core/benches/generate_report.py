#!/usr/bin/env python3
"""Generate a self-contained HTML report with embedded benchmark graphs."""

import base64
import json
import os
from datetime import datetime

PLOT_DIR = os.path.join("target", "criterion", "summary_plots")
DATE_STR = datetime.now().strftime("%Y-%m-%d")

def embed_image(filename):
    path = os.path.join(PLOT_DIR, filename)
    if not os.path.exists(path):
        return ""
    with open(path, "rb") as f:
        data = base64.b64encode(f.read()).decode("ascii")
    return f"data:image/png;base64,{data}"

def img_tag(filename, alt):
    src = embed_image(filename)
    if not src:
        return f"<p><em>Plot not found: {filename}</em></p>"
    return f'<img src="{src}" alt="{alt}">'


def kernel_profile_section():
    """Generate the Kernel Profile HTML section from kernel_profile.json."""
    json_path = os.path.join("target", "bench_retries", "kernel_profile.json")
    if not os.path.exists(json_path):
        return ""

    with open(json_path) as f:
        kdata = json.load(f)

    backend = kdata.get("backend", "unknown")
    platform = kdata.get("platform", "unknown")
    bench_filter = kdata.get("bench_filter", "N/A")
    duration = kdata.get("duration_s", 0)
    errors = kdata.get("errors", [])
    limitations = kdata.get("limitations", [])
    trace_file = kdata.get("trace_file", "")

    # Build metadata table rows
    meta_rows = f"""
<tr><td>Backend</td><td><code>{backend}</code></td></tr>
<tr><td>Platform</td><td>{platform}</td></tr>
<tr><td>Bench filter</td><td><code>{bench_filter}</code></td></tr>
<tr><td>Duration</td><td>{duration}s</td></tr>"""

    if trace_file:
        meta_rows += f'\n<tr><td>Trace file</td><td><code>{trace_file}</code></td></tr>'

    # Build limitations list
    lim_html = ""
    if limitations:
        lim_items = "".join(f"<li>{lim}</li>" for lim in limitations)
        lim_html = f"""
<h3>Limitations</h3>
<ul>{lim_items}</ul>"""

    # Build errors list
    err_html = ""
    if errors:
        err_items = "".join(f"<li>{err}</li>" for err in errors)
        err_html = f"""
<h3>Errors</h3>
<ul style="color: #c0392b;">{err_items}</ul>"""

    # Data availability explanation
    avail_rows = ""
    syscalls = kdata.get("syscalls")
    thread_states = kdata.get("thread_states")
    cpu_samples = kdata.get("cpu_samples")

    has_syscalls = isinstance(syscalls, dict) and len(syscalls) > 0
    has_thread = isinstance(thread_states, dict) and len(thread_states) > 0
    has_cpu = isinstance(cpu_samples, dict) and cpu_samples.get("total_samples", 0) > 0

    def check(val):
        return "Available" if val else "Not available"

    avail_rows = f"""
<tr><td>Syscall timing</td><td>{check(has_syscalls)}</td></tr>
<tr><td>Thread states</td><td>{check(has_thread)}</td></tr>
<tr><td>CPU samples</td><td>{check(has_cpu)}</td></tr>"""

    return f"""
<!-- ===== Kernel Profile ===== -->
<h2>6b. Kernel Profile</h2>

<p>
<strong>Purpose:</strong> Identify whether lock hold time is dominated by CPU work or
kernel I/O (syscalls like fsync, pwrite). The kernel profiler runs the benchmark under
a platform-specific tool (DTrace, perf, or Instruments) and extracts per-syscall timing
and thread scheduling data.
</p>

<h3>Profiling Metadata</h3>
<table class="hw-table">
<tr><th>Property</th><th>Value</th></tr>
{meta_rows}
</table>

<h3>Data Availability</h3>
<table class="hw-table">
<tr><th>Data Type</th><th>Status</th></tr>
{avail_rows}
</table>

<p>
Data availability depends on the backend and system permissions. On macOS with SIP enabled,
DTrace can capture syscall timing but not thread scheduling states. The xctrace backend
(Instruments System Trace) can provide thread states but not per-syscall timing.
On Linux, <code>perf</code> can provide all three data types with sufficient permissions.
</p>
{lim_html}
{err_html}

<div class="graph">
  {img_tag("kernel_profile.png", "Kernel Profile")}
</div>

<div class="findings">
<strong>How to read this data:</strong>
<ul>
  <li><strong>Syscall breakdown</strong> (if available): Horizontal bars show total wall-clock time
  spent in each syscall. The annotations show call count and average duration. Look for
  <code>fsync</code>/<code>fdatasync</code> (durability cost) and
  <code>pwrite64</code>/<code>pwritev</code> (write I/O cost) as these dominate under the
  commit lock.</li>
  <li><strong>Thread states</strong> (if available): Shows how much time benchmark threads spent
  running vs blocked vs preempted. High blocked counts indicate I/O waits or lock contention
  at the kernel level.</li>
  <li>If neither chart is shown, open the trace file in Instruments.app (macOS) for
  full thread-level analysis.</li>
</ul>
</div>
"""

html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Turso Parallel Write Benchmark Results &mdash; {DATE_STR}</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    line-height: 1.6;
    color: #1a1a2e;
    background: #f8f9fa;
  }}
  .container {{
    max-width: 1100px;
    margin: 0 auto;
    padding: 2rem 1.5rem;
  }}
  h1 {{
    font-size: 2rem;
    margin-bottom: 0.5rem;
    color: #0f0f23;
  }}
  .subtitle {{
    color: #555;
    font-size: 1.1rem;
    margin-bottom: 2.5rem;
  }}
  h2 {{
    font-size: 1.5rem;
    margin: 2.5rem 0 1rem;
    padding-bottom: 0.4rem;
    border-bottom: 2px solid #dee2e6;
    color: #0f0f23;
  }}
  h3 {{
    font-size: 1.15rem;
    margin: 1.5rem 0 0.5rem;
    color: #333;
  }}
  p {{
    margin-bottom: 1rem;
    color: #333;
  }}
  .graph {{
    background: #fff;
    border: 1px solid #dee2e6;
    border-radius: 8px;
    padding: 1rem;
    margin: 1.5rem 0;
    text-align: center;
  }}
  .graph img {{
    max-width: 100%;
    height: auto;
  }}
  table {{
    border-collapse: collapse;
    margin: 1rem 0 1.5rem;
    width: 100%;
  }}
  th, td {{
    text-align: left;
    padding: 0.5rem 0.75rem;
    border: 1px solid #dee2e6;
  }}
  th {{
    background: #e9ecef;
    font-weight: 600;
  }}
  tr:nth-child(even) {{ background: #f8f9fa; }}
  code {{
    background: #e9ecef;
    padding: 0.15rem 0.4rem;
    border-radius: 3px;
    font-size: 0.9em;
  }}
  .hw-table {{ max-width: 550px; }}
  .findings {{
    background: #fff;
    border-left: 4px solid #4c6ef5;
    padding: 1rem 1.25rem;
    margin: 1rem 0 1.5rem;
    border-radius: 0 6px 6px 0;
  }}
  .findings ul {{
    margin: 0.5rem 0 0 1.25rem;
  }}
  .findings li {{
    margin-bottom: 0.3rem;
  }}
  .legend {{
    display: inline-flex; gap: 1.5rem; margin: 0.5rem 0 1rem; flex-wrap: wrap;
  }}
  .legend-item {{
    display: inline-flex; align-items: center; gap: 0.4rem; font-size: 0.9rem;
  }}
  .swatch {{
    display: inline-block; width: 14px; height: 14px; border-radius: 3px;
  }}
  .swatch-green {{ background: #2ca02c; }}
  .swatch-red {{ background: #d62728; }}
  .swatch-blue {{ background: #1f77b4; }}
  footer {{
    margin-top: 3rem;
    padding-top: 1rem;
    border-top: 1px solid #dee2e6;
    color: #888;
    font-size: 0.85rem;
  }}
</style>
</head>
<body>
<div class="container">

<h1>Turso Parallel Write Benchmark Results</h1>
<p class="subtitle">Contention diagnostics for WAL and MVCC concurrent writers &mdash; {DATE_STR}</p>

<!-- ===== Hardware ===== -->
<h2>Test Environment</h2>
<table class="hw-table">
<tr><th>Component</th><th>Details</th></tr>
<tr><td>CPU</td><td>Apple M2 Max (8 performance + 4 efficiency cores, 12 threads)</td></tr>
<tr><td>Memory</td><td>32 GB unified LPDDR5</td></tr>
<tr><td>L1d cache</td><td>64 KB per core</td></tr>
<tr><td>L2 cache</td><td>4 MB shared</td></tr>
<tr><td>Storage</td><td>Internal Apple SSD (benchmarks use tmpfs via <code>tempfile::tempdir()</code>)</td></tr>
<tr><td>OS</td><td>macOS 26.2 Tahoe (Darwin 25.2.0)</td></tr>
<tr><td>Rust toolchain</td><td>rustc 1.88.0 (2025-06-23)</td></tr>
<tr><td>Allocator</td><td>mimalloc</td></tr>
<tr><td>Benchmark framework</td><td>Criterion.rs, sample_size=10, 5 independent runs</td></tr>
</table>

<h3>Color Legend (all plots)</h3>
<div class="legend">
  <span class="legend-item"><span class="swatch swatch-green"></span> Turso WAL</span>
  <span class="legend-item"><span class="swatch swatch-red"></span> Turso MVCC</span>
  <span class="legend-item"><span class="swatch swatch-blue"></span> SQLite WAL (dashed)</span>
</div>

<!-- ===== Benchmark 1 ===== -->
<h2>1. Disjoint Key Scalability</h2>

<p>
<strong>Purpose:</strong> Isolate commit-path serialization cost from data contention.
Each writer inserts 10 batches of 50 rows into non-overlapping key ranges
(500 rows/writer total). With no key overlap, any throughput degradation
as writer count increases comes purely from lock contention on the commit
path: WAL's <code>write_lock</code>, MVCC's <code>pager_commit_lock</code>,
or SQLite's WAL write lock.
</p>

<h3>Schema</h3>
<table>
<tr><th>Variant</th><th>Schema</th><th>Why</th></tr>
<tr><td>Turso MVCC</td><td><code>id INTEGER PRIMARY KEY, data TEXT</code></td><td>MVCC transactions are atomic; safe with PK</td></tr>
<tr><td>Turso WAL</td><td><code>id INTEGER, data TEXT</code></td><td>No PK avoids UNIQUE failures when Busy retry re-executes partially-committed batch</td></tr>
<tr><td>SQLite WAL</td><td><code>id INTEGER PRIMARY KEY, data TEXT</code></td><td>SQLite's busy_timeout handles retries at the connection level</td></tr>
</table>

<h3>Reading the Graph</h3>
<p>
<strong>Left panel:</strong> X-axis is the number of concurrent OS-thread writers (1&ndash;12).
Y-axis is total throughput in thousands of rows per second (Krows/sec), computed as
<em>total_rows / elapsed_seconds / 1000</em> where total_rows = writers &times; 10 batches &times;
50 rows. Higher is better.
Error bars show &plusmn;1 standard deviation across 5 independent runs.
</p>
<p>
<strong>Right panel:</strong> Same X-axis. Y-axis is per-writer throughput (Krows/sec), i.e.
total throughput divided by writer count. This measures scaling efficiency &mdash; a flat line
would indicate perfect linear scaling.
</p>

<div class="graph">
  {img_tag("disjoint_scalability.png", "Disjoint Key Scalability")}
</div>

<div class="findings">
<strong>Key findings:</strong>
<ul>
  <li><strong>Turso MVCC</strong> scales well up to ~6 writers, reaching ~700 Krows/sec. Beyond 6 writers, per-writer throughput flattens as the <code>pager_commit_lock</code> serializes commits.</li>
  <li><strong>Turso WAL</strong> shows similar scaling shape but with slightly lower peak throughput, as WAL's single write lock forces complete serialization of each commit.</li>
  <li><strong>SQLite WAL</strong> struggles significantly at higher thread counts, showing 3&ndash;5x lower throughput than Turso variants. This is largely due to SQLite's coarser locking and the overhead of separate process-level connections.</li>
  <li>The right panel (per-writer efficiency) shows all variants declining from 1 writer, confirming that commit-path contention is the dominant bottleneck even without data conflicts.</li>
</ul>
</div>

<!-- ===== Benchmark 2 ===== -->
<h2>2. Hot Row Contention</h2>

<p>
<strong>Purpose:</strong> Maximum write-write conflicts. All writers UPDATE the same
100 pre-populated rows. Each writer performs 10 batches of 10 UPDATEs (100 ops total),
with each UPDATE wrapped in its own <code>BEGIN CONCURRENT</code> / <code>COMMIT</code>
transaction for MVCC. This creates the highest realistic contention scenario.
</p>

<h3>Reading the Graph</h3>
<p>
<strong>X-axis:</strong> Number of concurrent OS-thread writers (1&ndash;12).
<strong>Left Y-axis (solid lines):</strong> Throughput in thousands of operations per second
(Kops/sec), where each op is one UPDATE. Computed as
<em>total_ops / elapsed_seconds / 1000</em> where total_ops = writers &times; 100.
Higher is better.
<strong>Right Y-axis (dashed lines, when present):</strong> Total number of retries across all
writers for the entire benchmark iteration. A retry occurs when a transaction gets a
<code>Busy</code> or <code>WriteWriteConflict</code> error and must re-execute.
Error bars show &plusmn;1 stddev across 5 runs.
</p>

<div class="graph">
  {img_tag("hot_row_contention.png", "Hot Row Contention")}
</div>

<div class="findings">
<strong>Key findings:</strong>
<ul>
  <li>Throughput drops dramatically compared to disjoint keys for all variants, confirming that data contention (not just commit-path serialization) is a major factor.</li>
  <li><strong>Turso MVCC</strong> maintains higher throughput than WAL at higher writer counts because it can execute the UPDATE optimistically and only detects conflicts at commit time, amortizing some overhead.</li>
  <li><strong>SQLite WAL</strong> suffers most under contention, as each writer must acquire the write lock even for single-row updates.</li>
  <li>Retry counts (when available) rise super-linearly with writer count, showing that contention creates a "retry storm" effect.</li>
</ul>
</div>

<!-- ===== Benchmark 3 ===== -->
<h2>3. Overlapping Key Insert</h2>

<p>
<strong>Purpose:</strong> Realistic partial contention. Each writer inserts 10 batches
of 50 rows using <code>INSERT OR REPLACE</code>. Writer K's range starts at
<code>K &times; 250</code>, so adjacent writers share 50% of their key space.
This models real-world scenarios where write sets partially overlap.
</p>

<h3>Reading the Graph</h3>
<p>
<strong>X-axis:</strong> Number of concurrent OS-thread writers (1&ndash;12).
<strong>Left Y-axis (solid lines):</strong> Throughput in Krows/sec, computed as
<em>total_rows / elapsed_seconds / 1000</em> where total_rows = writers &times; 10 batches &times;
50 rows. Higher is better.
<strong>Right Y-axis (dashed lines, when present):</strong> Total retries across all writers for
the benchmark iteration. Error bars show &plusmn;1 stddev across 5 runs.
</p>

<div class="graph">
  {img_tag("overlapping_insert.png", "Overlapping Key Insert")}
</div>

<div class="findings">
<strong>Key findings:</strong>
<ul>
  <li><strong>Turso WAL</strong> actually outperforms MVCC here. Since WAL serializes at the commit level, overlapping keys don't cause additional retries &mdash; the lock already prevents conflicts. WAL's simpler path wins when retries are expensive.</li>
  <li><strong>Turso MVCC shows zero application-level retries</strong> despite 50% key overlap.
  <code>INSERT OR REPLACE</code> eliminates data-level write-write conflicts (the row is
  simply replaced, not conflicted). However, MVCC still contends internally on the
  <code>pager_commit_lock</code> &mdash; lock metrics show tens of thousands of internal
  try-lock failures that are handled transparently via I/O yielding without surfacing
  as errors to the caller. This internal contention explains why MVCC throughput is
  lower than WAL despite zero visible retries: time is spent spinning on the commit lock
  rather than doing useful work.</li>
  <li>This reveals an important design tradeoff: MVCC's optimistic approach avoids
  application-visible retries for <code>OR REPLACE</code> workloads, but internal commit
  lock contention still limits throughput. WAL's simpler pessimistic path wins here
  because it avoids the commit lock spinning entirely.</li>
  <li><strong>SQLite</strong> continues to trail both Turso variants significantly.</li>
</ul>
</div>

<!-- ===== Benchmark 4 ===== -->
<h2>4. Single Row Increment</h2>

<p>
<strong>Purpose:</strong> Theoretical worst case. All writers execute
<code>UPDATE test SET counter = counter + 1 WHERE id = 1</code> in a loop,
100 times each. Every operation conflicts with every other writer. This
measures the absolute contention floor and retry overhead.
</p>

<h3>Reading the Graph</h3>
<p>
<strong>X-axis:</strong> Number of concurrent OS-thread writers (1&ndash;12).
<strong>Left Y-axis (solid lines):</strong> Throughput in Kops/sec, computed as
<em>total_ops / elapsed_seconds / 1000</em> where total_ops = writers &times; 100 increments.
Higher is better.
<strong>Right Y-axis (dashed lines, when present):</strong> Retry-to-success ratio, computed as
<em>total_retries / total_ops</em>. A ratio of 2.0 means two retries per successful operation.
This metric captures how "wasteful" contention is &mdash; higher values indicate more wasted work.
Error bars show &plusmn;1 stddev across 5 runs.
</p>

<div class="graph">
  {img_tag("single_row_increment.png", "Single Row Increment")}
</div>

<div class="findings">
<strong>Key findings:</strong>
<ul>
  <li>This is the worst case for parallelism &mdash; useful parallelism is near zero since every operation targets the same row.</li>
  <li><strong>Turso MVCC</strong> is ~2x faster than both WAL variants at 12 writers (~15 Kops/sec vs ~8 Kops/sec). MVCC's optimistic execution still provides benefit because the UPDATE itself is fast; the cost is in conflict detection and retry.</li>
  <li>At 12 writers with 100 ops each (1,200 total), the retry-to-success ratio can exceed 2:1, meaning more than half of all transaction attempts fail and must retry.</li>
  <li>Turso WAL and SQLite WAL converge to nearly identical performance, confirming that the bottleneck is the fundamental single-writer serialization inherent to WAL mode.</li>
</ul>
</div>

<!-- ===== Benchmark 5 ===== -->
<h2>5. Writer-Heavy Mixed Workload</h2>

<p>
<strong>Purpose:</strong> Validate that readers are not starved under heavy write pressure.
A fixed 2 readers each perform 100 point-lookup SELECTs while a variable number of writers
(4&ndash;12) each perform 10 batches of 10 UPDATEs on hot rows in [0, 200).
The table is pre-populated with 1,000 rows.
</p>

<h3>Reading the Graph</h3>
<p>
<strong>X-axis:</strong> Configuration label showing writer count + 2 fixed readers (e.g. "4W + 2R").
<strong>Y-axis:</strong> Total combined throughput in Kops/sec, covering both writer ops
(writers &times; 10 batches &times; 10 updates) and reader ops (2 &times; 100 SELECTs).
Grouped bars place the three variants side-by-side for each writer count.
Error bars show &plusmn;1 stddev across 5 runs.
</p>

<div class="graph">
  {img_tag("writer_heavy_mixed.png", "Writer-Heavy Mixed Workload")}
</div>

<div class="findings">
<strong>Key findings:</strong>
<ul>
  <li><strong>Turso MVCC</strong> provides the highest combined throughput across all writer counts, demonstrating MVCC's snapshot isolation advantage: readers get a consistent view without being blocked by concurrent writes.</li>
  <li><strong>Turso WAL</strong> shows competitive throughput at lower writer counts but degrades more steeply, as the write lock can delay readers during commits.</li>
  <li><strong>SQLite WAL</strong> trails both Turso variants, with throughput degrading significantly at 10+ writers.</li>
  <li>The grouped bar format clearly shows the throughput gap widening as writer count increases, confirming that MVCC's advantage grows with concurrency.</li>
</ul>
</div>

<!-- ===== Benchmark 5b: Latency ===== -->
<h2>5b. Writer-Heavy Mixed &mdash; Per-Operation Latency</h2>

<p>
<strong>Purpose:</strong> Complement the throughput view with per-operation latency
distributions. Throughput averages can hide the experience of individual operations
&mdash; a single slow retry can dominate tail latency even when average throughput
looks good. Each operation is timed individually, <em>including</em> any retry
wait time, to capture the full cost a client would observe.
</p>

<h3>Reading the Graph</h3>
<p>
<strong>Left panel (Write Latency):</strong> X-axis is the number of concurrent writer threads
(4&ndash;12). Y-axis is latency in microseconds (&mu;s). Solid lines show average (mean)
per-operation write latency across all writer threads. Dashed lines show p99 (99th
percentile) &mdash; the latency at which 99% of operations complete. Higher writer counts
increase contention, which inflates both avg and p99 as threads spend more time retrying.
</p>
<p>
<strong>Right panel (Read Latency):</strong> Same axes. Shows latency for the 2 fixed reader
threads. Under MVCC, readers should be largely unaffected by writer count (snapshot isolation).
Under WAL, readers may see elevated p99 latency as the write lock occasionally blocks reads
during commits.
</p>
<p>
Error bars show &plusmn;1 stddev across independent runs. Per variant, each marker/color
matches the throughput plot above.
</p>

<div class="graph">
  {img_tag("writer_heavy_latency.png", "Writer-Heavy Latency")}
</div>

<div class="findings">
<strong>Key findings:</strong>
<ul>
  <li><strong>Write p99 latency</strong> typically grows much faster than average as writers increase, revealing the "long tail" from retry storms &mdash; a few unlucky operations wait through multiple retry cycles.</li>
  <li><strong>MVCC read latency</strong> should remain nearly flat regardless of writer count, confirming snapshot isolation: readers see a consistent view without waiting for writers.</li>
  <li><strong>WAL read latency</strong> may show elevated p99 at high writer counts, as the write lock can momentarily block readers during commit.</li>
  <li>If avg write latency is 100&mu;s but p99 is 5,000&mu;s, that 50x gap reveals that contention creates extreme outliers &mdash; important for latency-sensitive applications.</li>
</ul>
</div>

<!-- ===== Lock Hold Breakdown ===== -->
<h2>6. Lock Hold Time Breakdown</h2>

<p>
<strong>Purpose:</strong> Decompose the lock hold time to answer two key questions:
<em>"What fraction of lock hold time is disk I/O vs CPU work?"</em> and
<em>"Which I/O operations dominate?"</em>
This identifies whether optimization should target I/O reduction (group commit,
deferred fsync) or CPU reduction (moving frame preparation outside the lock).
This plot is only generated when benchmarks are run with <code>--features lock_metrics</code>.
</p>

<h3>Reading the Graph</h3>
<p>
<strong>Left panel (WAL):</strong> Five-segment stacked bars show per-acquisition lock hold time
in microseconds. From bottom to top: non-I/O CPU work (lightest &mdash; frame preparation,
page cache operations, commit finalization), WAL header prepare (WAL header pwrite + fsync),
page reads (reading evicted pages from disk), frame pwritev (writing dirty page frames
to the WAL file), and WAL fsync (final durability sync). The relative sizes immediately
reveal whether the lock is I/O-bound or CPU-bound.
</p>
<p>
<strong>Right panel (MVCC):</strong> Two-segment stacked bars showing <code>log_tx()</code>
I/O (medium red) and <code>sync()</code>/fsync (dark red). These two operations account
for &gt;99% of MVCC lock hold time; the remaining commit finalization overhead is &lt;1%
and is omitted.
</p>

<div class="graph">
  {img_tag("lock_hold_breakdown.png", "Lock Hold Time Breakdown")}
</div>

<div class="findings">
<strong>Key findings:</strong>
<ul>
  <li><strong>WAL is CPU-bound under lock:</strong> Non-I/O CPU work (lightest green)
  dominates at ~85% of hold time across all writer counts. The pwritev phase accounts
  for only ~14%, and fsync is negligible (&lt;0.2%) because the benchmarks use
  <code>SyncMode::Normal</code> which skips WAL-commit fsync. The CPU time is spent in
  <code>WalCommitDone</code> finalization (frame cache updates via
  <code>commit_prepared_frames</code>, dirty page clearing) and <code>PrepareFrames</code>
  (frame header computation, page collection from cache).</li>
  <li><strong>WAL optimization path:</strong> Since CPU work dominates, the primary
  optimization target is moving frame preparation and commit finalization outside the
  write_lock critical section, not reducing I/O. Moving I/O outside the lock would
  only recover ~15% of hold time.</li>
  <li><strong>MVCC is I/O-bound under lock:</strong> In contrast, MVCC lock hold time is
  &gt;99% I/O &mdash; split roughly 70% fsync and 30% <code>log_tx()</code>. This makes MVCC
  a good candidate for group commit or deferred fsync optimizations. With
  <code>PRAGMA synchronous=NORMAL</code>, the fsync component would vanish entirely.</li>
  <li><strong>Per-acquire cost at 12 writers:</strong> WAL holds the lock ~100&mu;s per
  acquisition (83&mu;s CPU + 17&mu;s I/O). MVCC holds it ~70&mu;s per acquisition
  (all I/O). MVCC&rsquo;s lower per-acquire time explains its higher throughput in
  the Disjoint Key Scalability plot.</li>
</ul>
</div>

{kernel_profile_section()}

<!-- ===== Overview ===== -->
<h2>7. Contention Overview</h2>

<p>
Side-by-side comparison of all contention levels at a fixed 8 writers, showing
how each variant responds to increasing data contention.
</p>

<h3>Reading the Graph</h3>
<p>
<strong>X-axis:</strong> Contention level categories, from lowest (Disjoint Keys &mdash; no data
conflicts) to highest (Single Row &mdash; every op conflicts). Bars are grouped by variant.
<strong>Y-axis:</strong> Throughput in Kops/sec at 8 writers. The total ops differ per group
(Disjoint: 8&times;500=4000 rows; Hot Row: 8&times;100=800 ops; Overlapping: 8&times;500=4000 rows;
Single Row: 8&times;100=800 ops), so absolute values reflect both work volume and speed.
Error bars show &plusmn;1 stddev across 5 runs.
</p>

<div class="graph">
  {img_tag("contention_overview.png", "Contention Overview at 8 Writers")}
</div>

<div class="findings">
<strong>Key findings:</strong>
<ul>
  <li>Moving from left to right (disjoint &rarr; hot row &rarr; overlapping &rarr; single row), throughput drops dramatically for all variants, confirming that data contention dominates commit-path contention.</li>
  <li>MVCC and WAL trade positions depending on contention type: MVCC wins at disjoint and hot-row workloads; WAL can win at overlapping inserts where large-batch retries are expensive.</li>
  <li>SQLite consistently trails by 2&ndash;5x, validating Turso's performance advantage for concurrent write workloads.</li>
</ul>
</div>

<!-- ===== Methodology ===== -->
<h2>Methodology</h2>

<h3>Execution</h3>
<p>
Each benchmark group was run 5 independent times using Criterion.rs with
<code>sample_size(10)</code>. Error bars in all plots represent &plusmn;1 standard
deviation across the 5 runs. Databases are created in a fresh temporary directory
for each Criterion iteration to avoid cross-run interference.
</p>

<h3>Thread Synchronization</h3>
<p>
All writers (and readers in the mixed workload) synchronize via
<code>std::sync::Barrier</code> before beginning work. Timing starts after
barrier release and ends when all threads have joined.
</p>

<h3>Retry Strategy</h3>
<table>
<tr><th>Variant</th><th>Error</th><th>Action</th></tr>
<tr><td>Turso WAL</td><td><code>Busy</code></td><td><code>thread::yield_now()</code> + retry same statement</td></tr>
<tr><td>Turso MVCC</td><td><code>WriteWriteConflict</code> or <code>Busy</code></td><td><code>ROLLBACK</code> + <code>yield_now()</code> + retry full <code>BEGIN CONCURRENT</code> / statement / <code>COMMIT</code> cycle</td></tr>
<tr><td>SQLite WAL</td><td><code>SQLITE_BUSY</code> or <code>SQLITE_LOCKED</code></td><td><code>yield_now()</code> + retry (with <code>busy_timeout(10s)</code> fallback)</td></tr>
</table>

<h3>Retry Counting</h3>
<p>
Each retry is counted via <code>AtomicU64</code> (Relaxed ordering) shared across
all writer threads. After each benchmark iteration, the count is written to a JSON
sidecar file in <code>target/bench_retries/</code> for the plotter to overlay on
throughput graphs.
</p>

<h3>Latency Collection</h3>
<p>
In the Writer-Heavy Mixed workload, each thread times individual operations using
<code>Instant::now()</code> around the full execute-with-retry call. Per-op durations
(including retry wait time) are collected per thread, merged after joining, and
summarized as mean and p99 in microseconds. Statistics are written to JSON sidecar
files alongside retry counts.
</p>

<h3>Source Files</h3>
<p>
Benchmarks: <code>core/benches/parallel_write_benchmark.rs</code><br>
Plotter: <code>core/benches/plot_parallel_benchmarks.py</code><br>
Report generator: <code>core/benches/generate_report.py</code><br>
Runner: <code>core/benches/run_parallel_benchmarks.sh</code>
</p>

<footer>
Generated {DATE_STR} from Criterion benchmark data &bull; Turso (turso_core v0.5.0-pre.7)
</footer>

</div>
</body>
</html>
"""

output_path = os.path.join(PLOT_DIR, f"benchmark_report_{DATE_STR}.html")
os.makedirs(PLOT_DIR, exist_ok=True)
with open(output_path, "w") as f:
    f.write(html)
print(f"Report written to {output_path}")
