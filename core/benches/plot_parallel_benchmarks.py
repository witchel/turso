#!/usr/bin/env python3
"""Generate summary plots from parallel write benchmark results.

Usage:
  # Single run (reads from target/criterion/):
  python3 core/benches/plot_parallel_benchmarks.py

  # Multiple runs (reads from target/criterion/run_N/ directories):
  python3 core/benches/plot_parallel_benchmarks.py --runs 5

Reads Criterion JSON estimates and retry sidecar files, producing
6 PNG plots with error bars when multiple runs are available.
"""

import argparse
import json
import os
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

OUTPUT_DIR = "target/criterion/summary_plots"

# Consistent colors across all plots
COLORS = {
    "turso_wal": "#2ca02c",    # green
    "turso_mvcc": "#d62728",   # red
    "sqlite_wal": "#1f77b4",   # blue
}
LABELS = {
    "turso_wal": "Turso WAL",
    "turso_mvcc": "Turso MVCC",
    "sqlite_wal": "SQLite WAL",
}
MARKERS = {
    "turso_wal": "^",
    "turso_mvcc": "D",
    "sqlite_wal": "o",
}


def get_run_dirs(num_runs):
    """Return list of Criterion data directories for each run."""
    if num_runs <= 1:
        return ["target/criterion"]
    return [f"target/criterion/run_{i}" for i in range(1, num_runs + 1)]


def read_estimate(base_dir, group, bench_id, param):
    """Read the point estimate (in ns) from a Criterion benchmark."""
    path = os.path.join(base_dir, group, bench_id, str(param), "new", "estimates.json")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        data = json.load(f)
    return data["mean"]["point_estimate"]


def read_retries(base_dir, group, bench_id, param):
    """Read the retry count from the sidecar JSON.

    Retry data is stored in target/bench_retries/ (or run_N/bench_retries/)
    separately from Criterion's output to avoid being overwritten.
    """
    # For multi-run: base_dir is target/criterion/run_N, retries are in
    # target/bench_retries/run_N/
    retries_base = base_dir.replace("target/criterion", "target/bench_retries")
    path = os.path.join(retries_base, group, bench_id, f"{param}.json")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        data = json.load(f)
    return data.get("retries", 0)


def read_latency(base_dir, group, bench_id, param):
    """Read latency stats from the sidecar JSON."""
    retries_base = base_dir.replace("target/criterion", "target/bench_retries")
    path = os.path.join(retries_base, group, bench_id, f"{param}_latency.json")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def read_across_runs(run_dirs, group, bench_id, param, reader_fn):
    """Read values from all runs using the given reader function."""
    values = []
    for d in run_dirs:
        val = reader_fn(d, group, bench_id, param)
        if val is not None:
            values.append(val)
    return values


def make_style(variant, dashed=False):
    """Return consistent plot styling for a variant."""
    ls = "--" if variant == "sqlite_wal" or dashed else "-"
    return dict(
        color=COLORS.get(variant, "gray"),
        marker=MARKERS.get(variant, "x"),
        label=LABELS.get(variant, variant),
        linewidth=2,
        markersize=7,
        linestyle=ls,
    )


# ---------------------------------------------------------------------------
# Plot 1: Disjoint Key Scalability
# ---------------------------------------------------------------------------

def plot_disjoint_scalability(run_dirs):
    """Line plot: throughput (Krows/sec) vs writers. Subplot: per-writer efficiency."""
    group_name = "Disjoint Key Scalability"
    writer_counts = [1, 2, 4, 6, 8, 10, 12]
    batches = 10
    rows_per_batch = 50
    multi = len(run_dirs) > 1

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 6))

    for variant in ["turso_wal", "turso_mvcc", "sqlite_wal"]:
        tp_means, tp_stds = [], []
        eff_means, eff_stds = [], []
        counts_used = []

        for w in writer_counts:
            total_rows = w * batches * rows_per_batch
            estimates = read_across_runs(run_dirs, group_name, variant, w, read_estimate)
            if not estimates:
                continue
            throughputs = [total_rows / (e / 1e9) / 1000 for e in estimates]
            per_writer = [t / w for t in throughputs]
            tp_means.append(np.mean(throughputs))
            tp_stds.append(np.std(throughputs) if multi else 0)
            eff_means.append(np.mean(per_writer))
            eff_stds.append(np.std(per_writer) if multi else 0)
            counts_used.append(w)

        if not counts_used:
            continue
        style = make_style(variant)
        if multi:
            ax1.errorbar(counts_used, tp_means, yerr=tp_stds, capsize=4, capthick=1.5, **style)
            ax2.errorbar(counts_used, eff_means, yerr=eff_stds, capsize=4, capthick=1.5, **style)
        else:
            ax1.plot(counts_used, tp_means, **style)
            ax2.plot(counts_used, eff_means, **style)

    suffix = f" (mean of {len(run_dirs)} runs)" if multi else ""
    ax1.set_xlabel("Number of Writers")
    ax1.set_ylabel("Throughput (Krows/sec)")
    ax1.set_title(f"Disjoint Key Scalability{suffix}")
    ax1.legend(fontsize=9)
    ax1.set_xticks(writer_counts)
    ax1.grid(True, alpha=0.3)

    ax2.set_xlabel("Number of Writers")
    ax2.set_ylabel("Per-Writer Throughput (Krows/sec)")
    ax2.set_title(f"Scaling Efficiency{suffix}")
    ax2.legend(fontsize=9)
    ax2.set_xticks(writer_counts)
    ax2.grid(True, alpha=0.3)

    plt.tight_layout()
    plt.savefig(os.path.join(OUTPUT_DIR, "disjoint_scalability.png"), dpi=150)
    plt.close()
    print("  Saved disjoint_scalability.png")


# ---------------------------------------------------------------------------
# Plot 2: Hot Row Contention
# ---------------------------------------------------------------------------

def plot_hot_row_contention(run_dirs):
    """Dual-axis: throughput (left) + total retries (right, dashed) vs writers."""
    group_name = "Hot Row Contention"
    writer_counts = [1, 2, 4, 6, 8, 10, 12]
    ops_per_writer = 10 * 10  # 10 batches * 10 updates
    multi = len(run_dirs) > 1

    fig, ax1 = plt.subplots(figsize=(10, 6))
    has_retry_data = False
    ax2 = None
    retry_plot_data = []  # collect for deferred plotting

    for variant in ["turso_wal", "turso_mvcc", "sqlite_wal"]:
        tp_means, tp_stds = [], []
        rt_means, rt_stds = [], []
        counts_used = []
        variant_has_retries = False

        for w in writer_counts:
            total_ops = w * ops_per_writer
            estimates = read_across_runs(run_dirs, group_name, variant, w, read_estimate)
            retries_list = read_across_runs(run_dirs, group_name, variant, w, read_retries)
            if not estimates:
                continue
            throughputs = [total_ops / (e / 1e9) / 1000 for e in estimates]
            tp_means.append(np.mean(throughputs))
            tp_stds.append(np.std(throughputs) if multi else 0)
            if retries_list and any(r > 0 for r in retries_list):
                rt_means.append(np.mean(retries_list))
                rt_stds.append(np.std(retries_list) if multi else 0)
                variant_has_retries = True
            else:
                rt_means.append(0)
                rt_stds.append(0)
            counts_used.append(w)

        if not counts_used:
            continue
        style = make_style(variant)
        if multi:
            ax1.errorbar(counts_used, tp_means, yerr=tp_stds, capsize=4, capthick=1.5, **style)
        else:
            ax1.plot(counts_used, tp_means, **style)

        if variant_has_retries:
            has_retry_data = True
            retry_plot_data.append((counts_used, rt_means, rt_stds, variant))

    # Only create right axis if we have actual retry data
    if has_retry_data:
        ax2 = ax1.twinx()
        for counts_used, rt_means, rt_stds, variant in retry_plot_data:
            retry_style = make_style(variant, dashed=True)
            retry_style["linestyle"] = "--"
            retry_style["label"] = f"{LABELS[variant]} retries"
            if multi:
                ax2.errorbar(counts_used, rt_means, yerr=rt_stds, capsize=4, capthick=1.5, **retry_style)
            else:
                ax2.plot(counts_used, rt_means, **retry_style)
        ax2.set_ylabel("Total Retries (dashed)")

    suffix = f" (mean of {len(run_dirs)} runs)" if multi else ""
    ax1.set_xlabel("Number of Writers")
    ax1.set_ylabel("Throughput (Kops/sec)")
    ax1.set_title(f"Hot Row Contention{suffix}")
    ax1.set_xticks(writer_counts)
    ax1.grid(True, alpha=0.3)

    h1, l1 = ax1.get_legend_handles_labels()
    if ax2:
        h2, l2 = ax2.get_legend_handles_labels()
        ax1.legend(h1 + h2, l1 + l2, fontsize=8, loc="upper left")
    else:
        ax1.legend(fontsize=8, loc="upper left")

    plt.tight_layout()
    plt.savefig(os.path.join(OUTPUT_DIR, "hot_row_contention.png"), dpi=150)
    plt.close()
    print("  Saved hot_row_contention.png")


# ---------------------------------------------------------------------------
# Plot 3: Overlapping Key Insert
# ---------------------------------------------------------------------------

def plot_overlapping_insert(run_dirs):
    """Dual-axis: throughput (left) + total retries (right, dashed) vs writers."""
    group_name = "Overlapping Key Insert"
    writer_counts = [1, 2, 4, 6, 8, 10, 12]
    batches = 10
    rows_per_batch = 50
    multi = len(run_dirs) > 1

    fig, ax1 = plt.subplots(figsize=(10, 6))
    has_retry_data = False
    ax2 = None
    retry_plot_data = []

    for variant in ["turso_wal", "turso_mvcc", "sqlite_wal"]:
        tp_means, tp_stds = [], []
        rt_means, rt_stds = [], []
        counts_used = []
        variant_has_retries = False

        for w in writer_counts:
            total_rows = w * batches * rows_per_batch
            estimates = read_across_runs(run_dirs, group_name, variant, w, read_estimate)
            retries_list = read_across_runs(run_dirs, group_name, variant, w, read_retries)
            if not estimates:
                continue
            throughputs = [total_rows / (e / 1e9) / 1000 for e in estimates]
            tp_means.append(np.mean(throughputs))
            tp_stds.append(np.std(throughputs) if multi else 0)
            if retries_list and any(r > 0 for r in retries_list):
                rt_means.append(np.mean(retries_list))
                rt_stds.append(np.std(retries_list) if multi else 0)
                variant_has_retries = True
            else:
                rt_means.append(0)
                rt_stds.append(0)
            counts_used.append(w)

        if not counts_used:
            continue
        style = make_style(variant)
        if multi:
            ax1.errorbar(counts_used, tp_means, yerr=tp_stds, capsize=4, capthick=1.5, **style)
        else:
            ax1.plot(counts_used, tp_means, **style)

        if variant_has_retries:
            has_retry_data = True
            retry_plot_data.append((counts_used, rt_means, rt_stds, variant))

    if has_retry_data:
        ax2 = ax1.twinx()
        for counts_used, rt_means, rt_stds, variant in retry_plot_data:
            retry_style = make_style(variant, dashed=True)
            retry_style["linestyle"] = "--"
            retry_style["label"] = f"{LABELS[variant]} retries"
            if multi:
                ax2.errorbar(counts_used, rt_means, yerr=rt_stds, capsize=4, capthick=1.5, **retry_style)
            else:
                ax2.plot(counts_used, rt_means, **retry_style)
        ax2.set_ylabel("Total Retries (dashed)")

    suffix = f" (mean of {len(run_dirs)} runs)" if multi else ""
    ax1.set_xlabel("Number of Writers")
    ax1.set_ylabel("Throughput (Krows/sec)")
    ax1.set_title(f"Overlapping Key Insert (50% overlap){suffix}")
    ax1.set_xticks(writer_counts)
    ax1.grid(True, alpha=0.3)

    h1, l1 = ax1.get_legend_handles_labels()
    if ax2:
        h2, l2 = ax2.get_legend_handles_labels()
        ax1.legend(h1 + h2, l1 + l2, fontsize=8, loc="upper left")
    else:
        ax1.legend(fontsize=8, loc="upper left")

    plt.tight_layout()
    plt.savefig(os.path.join(OUTPUT_DIR, "overlapping_insert.png"), dpi=150)
    plt.close()
    print("  Saved overlapping_insert.png")


# ---------------------------------------------------------------------------
# Plot 4: Single Row Increment
# ---------------------------------------------------------------------------

def plot_single_row_increment(run_dirs):
    """Dual-axis: throughput (left) + retry-to-success ratio (right) vs writers."""
    group_name = "Single Row Increment"
    writer_counts = [1, 2, 4, 6, 8, 10, 12]
    ops_per_writer = 100
    multi = len(run_dirs) > 1

    fig, ax1 = plt.subplots(figsize=(10, 6))
    has_retry_data = False
    ax2 = None
    retry_plot_data = []  # collect for deferred plotting

    for variant in ["turso_wal", "turso_mvcc", "sqlite_wal"]:
        tp_means, tp_stds = [], []
        ratio_means, ratio_stds = [], []
        counts_used = []
        variant_has_retries = False

        for w in writer_counts:
            total_ops = w * ops_per_writer
            estimates = read_across_runs(run_dirs, group_name, variant, w, read_estimate)
            retries_list = read_across_runs(run_dirs, group_name, variant, w, read_retries)
            if not estimates:
                continue
            throughputs = [total_ops / (e / 1e9) / 1000 for e in estimates]
            tp_means.append(np.mean(throughputs))
            tp_stds.append(np.std(throughputs) if multi else 0)
            # retry-to-success ratio: retries / total_ops
            if retries_list and any(r > 0 for r in retries_list):
                ratios = [r / total_ops for r in retries_list]
                ratio_means.append(np.mean(ratios))
                ratio_stds.append(np.std(ratios) if multi else 0)
                variant_has_retries = True
            else:
                ratio_means.append(0)
                ratio_stds.append(0)
            counts_used.append(w)

        if not counts_used:
            continue
        style = make_style(variant)
        if multi:
            ax1.errorbar(counts_used, tp_means, yerr=tp_stds, capsize=4, capthick=1.5, **style)
        else:
            ax1.plot(counts_used, tp_means, **style)

        if variant_has_retries:
            has_retry_data = True
            retry_plot_data.append((counts_used, ratio_means, ratio_stds, variant))

    # Only create right axis if we have actual retry data
    if has_retry_data:
        ax2 = ax1.twinx()
        for counts_used, ratio_means, ratio_stds, variant in retry_plot_data:
            ratio_style = make_style(variant, dashed=True)
            ratio_style["linestyle"] = "--"
            ratio_style["label"] = f"{LABELS[variant]} retry ratio"
            if multi:
                ax2.errorbar(counts_used, ratio_means, yerr=ratio_stds, capsize=4, capthick=1.5, **ratio_style)
            else:
                ax2.plot(counts_used, ratio_means, **ratio_style)
        ax2.set_ylabel("Retry / Success Ratio (dashed)")

    suffix = f" (mean of {len(run_dirs)} runs)" if multi else ""
    ax1.set_xlabel("Number of Writers")
    ax1.set_ylabel("Throughput (Kops/sec)")
    ax1.set_title(f"Single Row Increment (worst-case contention){suffix}")
    ax1.set_xticks(writer_counts)
    ax1.grid(True, alpha=0.3)

    h1, l1 = ax1.get_legend_handles_labels()
    if ax2:
        h2, l2 = ax2.get_legend_handles_labels()
        ax1.legend(h1 + h2, l1 + l2, fontsize=8, loc="upper left")
    else:
        ax1.legend(fontsize=8, loc="upper left")

    plt.tight_layout()
    plt.savefig(os.path.join(OUTPUT_DIR, "single_row_increment.png"), dpi=150)
    plt.close()
    print("  Saved single_row_increment.png")


# ---------------------------------------------------------------------------
# Plot 5: Writer-Heavy Mixed Workload
# ---------------------------------------------------------------------------

def plot_writer_heavy_mixed(run_dirs):
    """Grouped bars (writer throughput) + line overlay (total throughput) vs writer count."""
    group_name = "Writer Heavy Mixed"
    writer_counts = [4, 6, 8, 10, 12]
    num_readers = 2
    writer_batches = 10
    updates_per_batch = 10
    reads_per_reader = 100
    multi = len(run_dirs) > 1

    fig, ax = plt.subplots(figsize=(10, 6))
    x = np.arange(len(writer_counts))
    width = 0.25
    variants = ["turso_wal", "turso_mvcc", "sqlite_wal"]

    for i, variant in enumerate(variants):
        tp_means, tp_stds = [], []
        for w in writer_counts:
            total_ops = w * writer_batches * updates_per_batch + num_readers * reads_per_reader
            estimates = read_across_runs(run_dirs, group_name, variant, w, read_estimate)
            if estimates:
                throughputs = [total_ops / (e / 1e9) / 1000 for e in estimates]
                tp_means.append(np.mean(throughputs))
                tp_stds.append(np.std(throughputs) if multi else 0)
            else:
                tp_means.append(0)
                tp_stds.append(0)

        if multi:
            ax.bar(x + i * width, tp_means, width, yerr=tp_stds,
                   label=LABELS[variant], color=COLORS[variant], alpha=0.85, capsize=4)
        else:
            ax.bar(x + i * width, tp_means, width,
                   label=LABELS[variant], color=COLORS[variant], alpha=0.85)

    suffix = f" (mean of {len(run_dirs)} runs)" if multi else ""
    ax.set_xlabel("Number of Writers (+ 2 readers)")
    ax.set_ylabel("Total Throughput (Kops/sec)")
    ax.set_title(f"Writer-Heavy Mixed Workload{suffix}")
    ax.set_xticks(x + width)
    ax.set_xticklabels([f"{w}W + 2R" for w in writer_counts])
    ax.legend(fontsize=9)
    ax.grid(True, alpha=0.3, axis="y")

    plt.tight_layout()
    plt.savefig(os.path.join(OUTPUT_DIR, "writer_heavy_mixed.png"), dpi=150)
    plt.close()
    print("  Saved writer_heavy_mixed.png")


# ---------------------------------------------------------------------------
# Plot 6: Writer-Heavy Latency (avg + p99 for reads and writes)
# ---------------------------------------------------------------------------

def plot_writer_heavy_latency(run_dirs):
    """Dual-panel line plot: write latency (left) and read latency (right) vs writers."""
    group_name = "Writer Heavy Mixed"
    writer_counts = [4, 6, 8, 10, 12]
    multi = len(run_dirs) > 1
    has_data = False

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 6))

    for variant in ["turso_wal", "turso_mvcc", "sqlite_wal"]:
        wavg_means, wavg_stds = [], []
        wp99_means, wp99_stds = [], []
        ravg_means, ravg_stds = [], []
        rp99_means, rp99_stds = [], []
        counts_used = []

        for w in writer_counts:
            lat_data = read_across_runs(run_dirs, group_name, variant, w, read_latency)
            if not lat_data:
                continue

            wavgs = [d["write_avg_us"] for d in lat_data]
            wp99s = [d["write_p99_us"] for d in lat_data]
            ravgs = [d["read_avg_us"] for d in lat_data]
            rp99s = [d["read_p99_us"] for d in lat_data]

            wavg_means.append(np.mean(wavgs))
            wavg_stds.append(np.std(wavgs) if multi else 0)
            wp99_means.append(np.mean(wp99s))
            wp99_stds.append(np.std(wp99s) if multi else 0)
            ravg_means.append(np.mean(ravgs))
            ravg_stds.append(np.std(ravgs) if multi else 0)
            rp99_means.append(np.mean(rp99s))
            rp99_stds.append(np.std(rp99s) if multi else 0)
            counts_used.append(w)

        if not counts_used:
            continue
        has_data = True

        # Write latency panel (left)
        avg_style = make_style(variant)
        avg_style["linestyle"] = "-"
        avg_style["label"] = f"{LABELS[variant]} avg"
        p99_style = make_style(variant)
        p99_style["linestyle"] = "--"
        p99_style["marker"] = "x"
        p99_style["label"] = f"{LABELS[variant]} p99"

        if multi:
            ax1.errorbar(counts_used, wavg_means, yerr=wavg_stds,
                         capsize=4, capthick=1.5, **avg_style)
            ax1.errorbar(counts_used, wp99_means, yerr=wp99_stds,
                         capsize=4, capthick=1.5, **p99_style)
        else:
            ax1.plot(counts_used, wavg_means, **avg_style)
            ax1.plot(counts_used, wp99_means, **p99_style)

        # Read latency panel (right)
        avg_style2 = make_style(variant)
        avg_style2["linestyle"] = "-"
        avg_style2["label"] = f"{LABELS[variant]} avg"
        p99_style2 = make_style(variant)
        p99_style2["linestyle"] = "--"
        p99_style2["marker"] = "x"
        p99_style2["label"] = f"{LABELS[variant]} p99"

        if multi:
            ax2.errorbar(counts_used, ravg_means, yerr=ravg_stds,
                         capsize=4, capthick=1.5, **avg_style2)
            ax2.errorbar(counts_used, rp99_means, yerr=rp99_stds,
                         capsize=4, capthick=1.5, **p99_style2)
        else:
            ax2.plot(counts_used, ravg_means, **avg_style2)
            ax2.plot(counts_used, rp99_means, **p99_style2)

    if not has_data:
        plt.close()
        print("  Skipped writer_heavy_latency.png (no latency data)")
        return

    suffix = f" (mean of {len(run_dirs)} runs)" if multi else ""
    ax1.set_xlabel("Number of Writers")
    ax1.set_ylabel("Write Latency (\u00b5s)")
    ax1.set_title(f"Per-Op Write Latency{suffix}")
    ax1.legend(fontsize=7, ncol=2)
    ax1.set_xticks(writer_counts)
    ax1.grid(True, alpha=0.3)

    ax2.set_xlabel("Number of Writers")
    ax2.set_ylabel("Read Latency (\u00b5s)")
    ax2.set_title(f"Per-Op Read Latency{suffix}")
    ax2.legend(fontsize=7, ncol=2)
    ax2.set_xticks(writer_counts)
    ax2.grid(True, alpha=0.3)

    plt.tight_layout()
    plt.savefig(os.path.join(OUTPUT_DIR, "writer_heavy_latency.png"), dpi=150)
    plt.close()
    print("  Saved writer_heavy_latency.png")


# ---------------------------------------------------------------------------
# Plot 7: Contention Overview (grouped bar at 8 writers)
# ---------------------------------------------------------------------------

def plot_contention_overview(run_dirs):
    """Grouped bar chart at 8 writers comparing all contention levels side-by-side."""
    multi = len(run_dirs) > 1
    writers = 8
    groups_info = [
        ("Disjoint Key Scalability", "Disjoint Keys", 8 * 10 * 50),
        ("Hot Row Contention", "Hot Row", 8 * 10 * 10),
        ("Overlapping Key Insert", "Overlapping Keys", 8 * 10 * 50),
        ("Single Row Increment", "Single Row", 8 * 100),
    ]
    variants = ["turso_wal", "turso_mvcc", "sqlite_wal"]

    fig, ax = plt.subplots(figsize=(12, 6))
    x = np.arange(len(groups_info))
    width = 0.25

    for i, variant in enumerate(variants):
        tp_means, tp_stds = [], []
        for group_name, _, total_ops in groups_info:
            estimates = read_across_runs(run_dirs, group_name, variant, writers, read_estimate)
            if estimates:
                throughputs = [total_ops / (e / 1e9) / 1000 for e in estimates]
                tp_means.append(np.mean(throughputs))
                tp_stds.append(np.std(throughputs) if multi else 0)
            else:
                tp_means.append(0)
                tp_stds.append(0)

        if multi:
            ax.bar(x + i * width, tp_means, width, yerr=tp_stds,
                   label=LABELS[variant], color=COLORS[variant], alpha=0.85, capsize=4)
        else:
            ax.bar(x + i * width, tp_means, width,
                   label=LABELS[variant], color=COLORS[variant], alpha=0.85)

    suffix = f" (mean of {len(run_dirs)} runs)" if multi else ""
    ax.set_xlabel("Contention Level")
    ax.set_ylabel("Throughput (Kops/sec)")
    ax.set_title(f"Contention Overview at {writers} Writers{suffix}")
    ax.set_xticks(x + width)
    ax.set_xticklabels([info[1] for info in groups_info], rotation=15)
    ax.legend(fontsize=9)
    ax.grid(True, alpha=0.3, axis="y")

    plt.tight_layout()
    plt.savefig(os.path.join(OUTPUT_DIR, "contention_overview.png"), dpi=150)
    plt.close()
    print("  Saved contention_overview.png")


# ---------------------------------------------------------------------------
# Plot 8: Lock Hold Time Breakdown (requires lock_metrics feature)
# ---------------------------------------------------------------------------

def read_lock_metrics(base_dir, group, bench_id, param):
    """Read lock metrics from the sidecar JSON."""
    retries_base = base_dir.replace("target/criterion", "target/bench_retries")
    path = os.path.join(retries_base, group, bench_id, f"{param}_lock_metrics.json")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def plot_lock_hold_breakdown(run_dirs):
    """1x2 subplot: WAL lock breakdown (left), MVCC lock breakdown (right).

    WAL panel shows 5-segment stacked bars: WAL header prepare, page reads,
    frame pwritev, WAL fsync, and non-I/O (CPU/cache).
    MVCC panel shows 3-segment bars: fsync, log I/O, and other.
    """
    group_name = "Disjoint Key Scalability"
    writer_counts = [1, 2, 4, 6, 8, 10, 12]
    multi = len(run_dirs) > 1
    has_data = False

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 6))

    # --- WAL panel (left): 5-segment breakdown ---
    wal_prepare_means, wal_read_means, wal_write_means = [], [], []
    wal_sync_means, wal_other_means = [], []
    wal_counts = []

    for w in writer_counts:
        metrics_list = read_across_runs(run_dirs, group_name, "turso_wal", w, read_lock_metrics)
        if not metrics_list:
            continue
        prepare_pa, read_pa, write_pa, sync_pa, other_pa = [], [], [], [], []
        for m in metrics_list:
            acq = m.get("wal_acquires", 0)
            if acq == 0:
                continue
            hold = m.get("wal_hold_us", 0)
            restart = m.get("wal_restart_io_us", 0)
            prepare = m.get("wal_commit_prepare_us", 0)
            read = m.get("wal_commit_read_us", 0)
            write = m.get("wal_commit_write_us", 0)
            sync = m.get("wal_commit_sync_us", 0)
            # Also accept legacy field name for backwards compat
            if restart == 0 and "wal_io_us" in m:
                restart = m["wal_io_us"]
            total_io = restart + prepare + read + write + sync
            prepare_pa.append((restart + prepare) / acq)
            read_pa.append(read / acq)
            write_pa.append(write / acq)
            sync_pa.append(sync / acq)
            other_pa.append(max(0, (hold - total_io)) / acq)
        if not prepare_pa:
            continue
        has_data = True
        wal_prepare_means.append(np.mean(prepare_pa))
        wal_read_means.append(np.mean(read_pa))
        wal_write_means.append(np.mean(write_pa))
        wal_sync_means.append(np.mean(sync_pa))
        wal_other_means.append(np.mean(other_pa))
        wal_counts.append(w)

    if wal_counts:
        x = np.arange(len(wal_counts))
        width = 0.6
        bottom = np.zeros(len(wal_counts))
        ax1.bar(x, wal_other_means, width, bottom=bottom,
                label="Non-I/O (CPU/cache)", color="#c8e6c9", alpha=0.85)
        bottom = bottom + np.array(wal_other_means)
        ax1.bar(x, wal_prepare_means, width, bottom=bottom,
                label="WAL header (prepare)", color="#a5d6a7", alpha=0.85)
        bottom = bottom + np.array(wal_prepare_means)
        ax1.bar(x, wal_read_means, width, bottom=bottom,
                label="Page reads", color="#66bb6a", alpha=0.85)
        bottom = bottom + np.array(wal_read_means)
        ax1.bar(x, wal_write_means, width, bottom=bottom,
                label="Frame pwritev", color="#388e3c", alpha=0.85)
        bottom = bottom + np.array(wal_write_means)
        ax1.bar(x, wal_sync_means, width, bottom=bottom,
                label="WAL fsync", color="#1b5e20", alpha=0.85)
        ax1.set_xlabel("Number of Writers")
        ax1.set_ylabel("\u00b5s per Lock Acquisition")
        ax1.set_title("WAL write_lock Hold Time Breakdown")
        ax1.set_xticks(x)
        ax1.set_xticklabels(wal_counts)
        ax1.legend(fontsize=8)
        ax1.grid(True, alpha=0.3, axis="y")

    # --- MVCC panel (right): log_tx + sync only (residual is <1%, omitted) ---
    mvcc_sync_means, mvcc_log_means = [], []
    mvcc_counts = []

    for w in writer_counts:
        metrics_list = read_across_runs(run_dirs, group_name, "turso_mvcc", w, read_lock_metrics)
        if not metrics_list:
            continue
        sync_per_acq = []
        log_per_acq = []
        for m in metrics_list:
            acq = m.get("mvcc_acquires", 0)
            if acq == 0:
                continue
            log = m.get("mvcc_log_us", 0)
            sync = m.get("mvcc_sync_us", 0)
            sync_per_acq.append(sync / acq)
            log_per_acq.append(log / acq)
        if not sync_per_acq:
            continue
        has_data = True
        mvcc_sync_means.append(np.mean(sync_per_acq))
        mvcc_log_means.append(np.mean(log_per_acq))
        mvcc_counts.append(w)

    if mvcc_counts:
        x = np.arange(len(mvcc_counts))
        width = 0.6
        ax2.bar(x, mvcc_log_means, width,
                label="log_tx() I/O", color="#e53935", alpha=0.85)
        ax2.bar(x, mvcc_sync_means, width, bottom=mvcc_log_means,
                label="sync() / fsync", color="#b71c1c", alpha=0.85)
        ax2.set_xlabel("Number of Writers")
        ax2.set_ylabel("\u00b5s per Lock Acquisition")
        ax2.set_title("MVCC pager_commit_lock Hold Time Breakdown")
        ax2.set_xticks(x)
        ax2.set_xticklabels(mvcc_counts)
        ax2.legend(fontsize=9)
        ax2.grid(True, alpha=0.3, axis="y")

    if not has_data:
        plt.close()
        print("  Skipped lock_hold_breakdown.png (no lock metrics data)")
        return

    suffix = f" (mean of {len(run_dirs)} runs)" if multi else ""
    fig.suptitle(f"Lock Hold Time Breakdown — Disjoint Key Workload{suffix}", fontsize=13, y=1.02)
    plt.tight_layout()
    plt.savefig(os.path.join(OUTPUT_DIR, "lock_hold_breakdown.png"), dpi=150, bbox_inches="tight")
    plt.close()
    print("  Saved lock_hold_breakdown.png")


# ---------------------------------------------------------------------------
# Plot 9: Kernel Profile (syscall breakdown + thread states)
# ---------------------------------------------------------------------------

def plot_kernel_profile():
    """Plot kernel profiling data from kernel_profile.json.

    Generates up to two sub-plots:
    - Syscall breakdown: horizontal stacked bars showing total time per syscall
    - Thread states: per-state interval counts (if available)

    Null-safe: skips gracefully when data is absent.
    """
    json_path = os.path.join("target", "bench_retries", "kernel_profile.json")
    if not os.path.exists(json_path):
        print("  Skipped kernel_profile.png (no kernel_profile.json)")
        return

    with open(json_path) as f:
        data = json.load(f)

    syscalls = data.get("syscalls")
    thread_states = data.get("thread_states")
    backend = data.get("backend", "unknown")
    has_syscalls = isinstance(syscalls, dict) and len(syscalls) > 0
    has_thread_states = isinstance(thread_states, dict) and len(thread_states) > 0

    if not has_syscalls and not has_thread_states:
        print(f"  Skipped kernel_profile.png (backend={backend}, no plottable data)")
        return

    num_panels = int(has_syscalls) + int(has_thread_states)
    fig, axes = plt.subplots(1, num_panels, figsize=(7 * num_panels, 6))
    if num_panels == 1:
        axes = [axes]

    panel_idx = 0

    # ── Syscall breakdown bar chart ──
    if has_syscalls:
        ax = axes[panel_idx]
        panel_idx += 1

        # Sort syscalls by total_us descending
        sorted_sc = sorted(syscalls.items(), key=lambda x: x[1].get("total_us", 0), reverse=True)

        # Show top 15 syscalls to avoid clutter
        top_n = 15
        if len(sorted_sc) > top_n:
            sorted_sc = sorted_sc[:top_n]

        names = [sc[0] for sc in sorted_sc]
        totals = [sc[1].get("total_us", 0) for sc in sorted_sc]
        counts = [sc[1].get("count", 0) for sc in sorted_sc]
        avgs = [sc[1].get("avg_us", 0) for sc in sorted_sc]

        # Convert to ms for readability if values are large
        max_total = max(totals) if totals else 0
        if max_total > 10000:
            totals_plot = [t / 1000.0 for t in totals]
            unit = "ms"
        else:
            totals_plot = totals
            unit = "\u00b5s"

        y_pos = np.arange(len(names))
        bars = ax.barh(y_pos, totals_plot, color="#4c6ef5", alpha=0.85)

        # Annotate with count and avg
        for i, (bar, count, avg) in enumerate(zip(bars, counts, avgs)):
            w = bar.get_width()
            label = f"n={count:,}  avg={avg:.1f}\u00b5s"
            ax.text(w + max(totals_plot) * 0.02, bar.get_y() + bar.get_height() / 2,
                    label, va='center', fontsize=7, color='#333')

        ax.set_yticks(y_pos)
        ax.set_yticklabels(names, fontsize=8)
        ax.invert_yaxis()
        ax.set_xlabel(f"Total Time ({unit})")
        ax.set_title(f"Syscall Time Breakdown ({backend})")
        ax.grid(True, alpha=0.3, axis="x")

    # ── Thread state chart ──
    if has_thread_states:
        ax = axes[panel_idx]
        panel_idx += 1

        # Prefer _ms (duration) data from xctrace Python parser; fall back to _intervals
        state_colors = {
            "running": "#2ca02c",
            "blocked": "#d62728",
            "preempted": "#ff7f0e",
            "runnable": "#1f77b4",
            "interrupted": "#9467bd",
        }
        # Exclude "terminated" — it's just post-exit idle time per thread
        exclude_states = {"terminated", "unknown"}

        state_keys = []
        state_values = []
        state_intervals = []
        bar_colors = []
        use_ms = any(k.endswith("_ms") for k in thread_states)

        if use_ms:
            # Rich data from xctrace Python parser
            for key, val in thread_states.items():
                if not key.endswith("_ms"):
                    continue
                state = key.replace("_ms", "")
                if state in exclude_states:
                    continue
                state_keys.append(state.capitalize())
                state_values.append(val)
                state_intervals.append(thread_states.get(f"{state}_intervals", 0))
                bar_colors.append(state_colors.get(state, "#888"))
        else:
            # Simple interval counts (perf backend or legacy)
            for key in ["running_intervals", "blocked_intervals", "preempted_intervals"]:
                if key in thread_states:
                    label = key.replace("_intervals", "").capitalize()
                    state_keys.append(label)
                    state_values.append(thread_states[key])
                    state_intervals.append(thread_states[key])
                    bar_colors.append(state_colors.get(key.replace("_intervals", ""), "#888"))

            # Also handle perf-style context_switches
            if "context_switches" in thread_states and not state_keys:
                state_keys.append("Context Switches")
                state_values.append(thread_states["context_switches"])
                state_intervals.append(thread_states["context_switches"])
                bar_colors.append("#888")

        if state_keys:
            bars = ax.bar(state_keys, state_values, color=bar_colors, alpha=0.85)

            for bar, val, intervals in zip(bars, state_values, state_intervals):
                if use_ms:
                    label = f"{val:,.0f} ms\n({intervals:,} intervals)"
                else:
                    label = f"{val:,}"
                ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height(),
                        label, ha='center', va='bottom', fontsize=8)

            ylabel = "Total Time (ms)" if use_ms else "Count"
            ax.set_ylabel(ylabel)
            ax.set_title(f"Thread State Duration ({backend})")
            ax.grid(True, alpha=0.3, axis="y")
        else:
            ax.text(0.5, 0.5, "No thread state data available",
                    ha='center', va='center', transform=ax.transAxes, fontsize=12, color='gray')
            ax.set_title(f"Thread States ({backend})")

    # Add metadata as figure text
    bench_filter = data.get("bench_filter", "")
    duration = data.get("duration_s", 0)
    limitations = data.get("limitations", [])
    lim_text = "; ".join(limitations) if limitations else "none"
    fig.text(0.5, -0.02,
             f"Filter: {bench_filter}  |  Duration: {duration}s  |  Limitations: {lim_text}",
             ha='center', fontsize=8, color='#666')

    plt.tight_layout()
    plt.savefig(os.path.join(OUTPUT_DIR, "kernel_profile.png"), dpi=150, bbox_inches="tight")
    plt.close()
    print("  Saved kernel_profile.png")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Plot parallel write benchmark results")
    parser.add_argument("--runs", type=int, default=1,
                        help="Number of runs to aggregate (default: 1, reads target/criterion/)")
    args = parser.parse_args()

    run_dirs = get_run_dirs(args.runs)
    missing = [d for d in run_dirs if not os.path.isdir(d)]
    if missing:
        print(f"Warning: missing run directories: {missing}")
        run_dirs = [d for d in run_dirs if os.path.isdir(d)]
    if not run_dirs:
        print("Error: no valid run directories found")
        return

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    print(f"Generating summary plots from {len(run_dirs)} run(s)...")

    plot_disjoint_scalability(run_dirs)
    plot_hot_row_contention(run_dirs)
    plot_overlapping_insert(run_dirs)
    plot_single_row_increment(run_dirs)
    plot_writer_heavy_mixed(run_dirs)
    plot_writer_heavy_latency(run_dirs)
    plot_contention_overview(run_dirs)
    plot_lock_hold_breakdown(run_dirs)
    plot_kernel_profile()

    print(f"\nAll plots saved to {OUTPUT_DIR}/")


if __name__ == "__main__":
    main()
