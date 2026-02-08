#!/usr/bin/env python3
"""Generate summary graphs from parallel write benchmark results.

Usage:
  # Single run (reads from target/criterion/):
  python3 core/benches/plot_parallel_benchmarks.py

  # Multiple runs (reads from target/criterion/run_N/ directories):
  python3 core/benches/plot_parallel_benchmarks.py --runs 5

Reads Criterion JSON estimates and produces PNG plots with error bars
when multiple runs are available.
"""

import argparse
import json
import os
import matplotlib.pyplot as plt
import numpy as np

OUTPUT_DIR = "target/criterion/summary_plots"


def read_estimate(base_dir, group, bench_id, param):
    """Read the point estimate (in ns) from a Criterion benchmark."""
    if param is not None:
        path = os.path.join(base_dir, group, bench_id, str(param), "new", "estimates.json")
    else:
        path = os.path.join(base_dir, group, bench_id, "new", "estimates.json")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        data = json.load(f)
    return data["mean"]["point_estimate"]


def get_run_dirs(num_runs):
    """Return list of Criterion data directories for each run."""
    if num_runs <= 1:
        return ["target/criterion"]
    return [f"target/criterion/run_{i}" for i in range(1, num_runs + 1)]


def read_estimates_across_runs(run_dirs, group, bench_id, param):
    """Read estimates from all runs, returning list of values (ns)."""
    values = []
    for d in run_dirs:
        est = read_estimate(d, group, bench_id, param)
        if est is not None:
            values.append(est)
    return values


def plot_writer_scalability(run_dirs):
    """Plot throughput (rows/sec) vs writer count for each variant."""
    batches = 5
    rows_per_batch = 10
    multi = len(run_dirs) > 1

    variants = {
        "limbo_wal_cooperative": {"counts": [1, 2, 4, 8], "color": "tab:blue", "marker": "o"},
        "limbo_mvcc_cooperative": {"counts": [1, 2, 4, 8], "color": "tab:orange", "marker": "s"},
        "limbo_wal_threaded": {"counts": [1, 2, 4, 8, 12, 16], "color": "tab:green", "marker": "^"},
        "limbo_mvcc_threaded": {"counts": [1, 2, 4, 8, 12, 16], "color": "tab:red", "marker": "D"},
    }

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 6))

    for variant_name, cfg in variants.items():
        tp_means, tp_stds = [], []
        t_means, t_stds = [], []
        counts_used = []

        for w in cfg["counts"]:
            total_rows = w * batches * rows_per_batch
            estimates = read_estimates_across_runs(run_dirs, "Writer Scalability", variant_name, w)
            if not estimates:
                continue
            throughputs = [total_rows / (e / 1e9) / 1000 for e in estimates]  # Krows/sec
            times = [e / 1e6 for e in estimates]  # ms
            tp_means.append(np.mean(throughputs))
            tp_stds.append(np.std(throughputs))
            t_means.append(np.mean(times))
            t_stds.append(np.std(times))
            counts_used.append(w)

        if counts_used:
            label = variant_name.replace("limbo_", "").replace("_", " ").title()
            if multi:
                ax1.errorbar(counts_used, tp_means, yerr=tp_stds, marker=cfg["marker"],
                             ls="-", color=cfg["color"], label=label, linewidth=2,
                             markersize=8, capsize=4, capthick=1.5)
                ax2.errorbar(counts_used, t_means, yerr=t_stds, marker=cfg["marker"],
                             ls="-", color=cfg["color"], label=label, linewidth=2,
                             markersize=8, capsize=4, capthick=1.5)
            else:
                ax1.plot(counts_used, tp_means, marker=cfg["marker"], ls="-",
                         color=cfg["color"], label=label, linewidth=2, markersize=8)
                ax2.plot(counts_used, t_means, marker=cfg["marker"], ls="-",
                         color=cfg["color"], label=label, linewidth=2, markersize=8)

    ax1.set_xlabel("Number of Writers")
    ax1.set_ylabel("Throughput (Krows/sec)")
    title_suffix = f" (mean of {len(run_dirs)} runs)" if multi else ""
    ax1.set_title(f"Writer Scalability: Throughput{title_suffix}")
    ax1.legend(fontsize=9)
    ax1.set_xticks([1, 2, 4, 8, 12, 16])
    ax1.grid(True, alpha=0.3)

    ax2.set_xlabel("Number of Writers")
    ax2.set_ylabel("Total Time (ms)")
    ax2.set_title(f"Writer Scalability: Latency{title_suffix}")
    ax2.legend(fontsize=9)
    ax2.set_xticks([1, 2, 4, 8, 12, 16])
    ax2.grid(True, alpha=0.3)

    plt.tight_layout()
    plt.savefig(os.path.join(OUTPUT_DIR, "writer_scalability.png"), dpi=150)
    plt.close()
    print("  Saved writer_scalability.png")


def plot_contention_patterns(run_dirs):
    """Plot throughput for different contention patterns."""
    patterns = ["separate_tables", "disjoint_keys", "overlapping_keys", "hot_rows_update"]
    variants = ["mvcc_cooperative", "mvcc_threaded"]
    colors = {"mvcc_cooperative": "tab:blue", "mvcc_threaded": "tab:red"}
    total_rows = 4 * 5 * 10
    multi = len(run_dirs) > 1

    fig, ax = plt.subplots(figsize=(10, 6))
    x = np.arange(len(patterns))
    width = 0.35

    for i, variant in enumerate(variants):
        tp_means, tp_stds = [], []
        for pat in patterns:
            estimates = read_estimates_across_runs(run_dirs, "Write Contention", variant, pat)
            if estimates:
                throughputs = [total_rows / (e / 1e9) / 1000 for e in estimates]
                tp_means.append(np.mean(throughputs))
                tp_stds.append(np.std(throughputs))
            else:
                tp_means.append(0)
                tp_stds.append(0)

        label = variant.replace("mvcc_", "MVCC ").title()
        if multi:
            ax.bar(x + i * width, tp_means, width, yerr=tp_stds, label=label,
                   color=colors[variant], alpha=0.8, capsize=4)
        else:
            ax.bar(x + i * width, tp_means, width, label=label,
                   color=colors[variant], alpha=0.8)

    ax.set_xlabel("Contention Pattern")
    ax.set_ylabel("Throughput (Krows/sec)")
    title_suffix = f" (mean of {len(run_dirs)} runs)" if multi else ""
    ax.set_title(f"Write Contention Patterns (4 MVCC Writers){title_suffix}")
    ax.set_xticks(x + width / 2)
    ax.set_xticklabels([p.replace("_", " ").title() for p in patterns], rotation=15)
    ax.legend()
    ax.grid(True, alpha=0.3, axis="y")

    plt.tight_layout()
    plt.savefig(os.path.join(OUTPUT_DIR, "contention_patterns.png"), dpi=150)
    plt.close()
    print("  Saved contention_patterns.png")


def plot_write_burst(run_dirs):
    """Plot throughput for write burst at different writer counts."""
    writer_counts = [4, 8, 12, 16]
    rows_per_writer = 50
    multi = len(run_dirs) > 1
    variants = {
        "limbo_mvcc_threaded": {"color": "tab:red", "marker": "D"},
        "limbo_wal_threaded": {"color": "tab:green", "marker": "^"},
    }

    fig, ax = plt.subplots(figsize=(8, 6))

    for variant_name, cfg in variants.items():
        tp_means, tp_stds = [], []
        counts_used = []
        for w in writer_counts:
            total_rows = w * rows_per_writer
            estimates = read_estimates_across_runs(run_dirs, "Write Burst", variant_name, w)
            if not estimates:
                continue
            throughputs = [total_rows / (e / 1e9) / 1000 for e in estimates]
            tp_means.append(np.mean(throughputs))
            tp_stds.append(np.std(throughputs))
            counts_used.append(w)

        if counts_used:
            label = variant_name.replace("limbo_", "").replace("_", " ").title()
            if multi:
                ax.errorbar(counts_used, tp_means, yerr=tp_stds, marker=cfg["marker"],
                             ls="-", color=cfg["color"], label=label, linewidth=2,
                             markersize=8, capsize=4, capthick=1.5)
            else:
                ax.plot(counts_used, tp_means, marker=cfg["marker"], ls="-",
                        color=cfg["color"], label=label, linewidth=2, markersize=8)

    ax.set_xlabel("Number of Simultaneous Writers")
    ax.set_ylabel("Throughput (Krows/sec)")
    title_suffix = f" (mean of {len(run_dirs)} runs)" if multi else ""
    ax.set_title(f"Write Burst (Thundering Herd){title_suffix}")
    ax.set_xticks(writer_counts)
    ax.legend()
    ax.grid(True, alpha=0.3)

    plt.tight_layout()
    plt.savefig(os.path.join(OUTPUT_DIR, "write_burst.png"), dpi=150)
    plt.close()
    print("  Saved write_burst.png")


def plot_mixed_workload(run_dirs):
    """Plot mixed workload result as a simple bar chart."""
    total_ops = (4 + 2) * 50
    multi = len(run_dirs) > 1

    estimates = read_estimates_across_runs(run_dirs, "Mixed Workload", "limbo_mvcc_threaded", None)
    if not estimates:
        print("  Skipped mixed_workload.png (no data)")
        return

    throughputs = [total_ops / (e / 1e9) / 1000 for e in estimates]
    times = [e / 1e6 for e in estimates]
    tp_mean = np.mean(throughputs)
    tp_std = np.std(throughputs)
    time_mean = np.mean(times)

    fig, ax = plt.subplots(figsize=(6, 5))

    if multi:
        ax.bar(["MVCC Threaded\n(4R + 2W)"], [tp_mean], yerr=[tp_std],
               color="tab:red", alpha=0.8, width=0.4, capsize=6)
    else:
        ax.bar(["MVCC Threaded\n(4R + 2W)"], [tp_mean],
               color="tab:red", alpha=0.8, width=0.4)

    ax.set_ylabel("Throughput (Kops/sec)")
    title_suffix = f" (mean of {len(run_dirs)} runs)" if multi else ""
    ax.set_title(f"Mixed Read-Write Workload (YCSB-B Style){title_suffix}\n{time_mean:.2f} ms for {total_ops} ops")
    ax.grid(True, alpha=0.3, axis="y")

    plt.tight_layout()
    plt.savefig(os.path.join(OUTPUT_DIR, "mixed_workload.png"), dpi=150)
    plt.close()
    print("  Saved mixed_workload.png")


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
    plot_writer_scalability(run_dirs)
    plot_contention_patterns(run_dirs)
    plot_write_burst(run_dirs)
    plot_mixed_workload(run_dirs)
    print(f"\nAll plots saved to {OUTPUT_DIR}/")


if __name__ == "__main__":
    main()
