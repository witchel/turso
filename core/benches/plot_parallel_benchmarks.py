#!/usr/bin/env python3
"""Generate summary graphs from parallel write benchmark results.

Usage: python3 core/benches/plot_parallel_benchmarks.py

Reads Criterion JSON estimates from target/criterion/ and produces PNG plots.
"""

import json
import os
import matplotlib.pyplot as plt
import numpy as np

CRITERION_DIR = "target/criterion"
OUTPUT_DIR = "target/criterion/summary_plots"


def read_estimate(group, bench_id, param):
    """Read the point estimate (in ns) from a Criterion benchmark."""
    path = os.path.join(CRITERION_DIR, group, bench_id, str(param), "new", "estimates.json")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        data = json.load(f)
    # mean point_estimate is in nanoseconds
    return data["mean"]["point_estimate"]


def ns_to_ms(ns):
    return ns / 1e6 if ns else None


def plot_writer_scalability():
    """Plot throughput (rows/sec) vs writer count for each variant."""
    writer_counts = [1, 2, 4, 8, 16]
    batches = 5
    rows_per_batch = 10

    variants = {
        "limbo_wal_cooperative": {"counts": [1, 2, 4, 8], "color": "tab:blue", "marker": "o", "ls": "-"},
        "limbo_mvcc_cooperative": {"counts": [1, 2, 4, 8], "color": "tab:orange", "marker": "s", "ls": "-"},
        "limbo_wal_threaded": {"counts": [1, 2, 4, 8, 16], "color": "tab:green", "marker": "^", "ls": "--"},
        "limbo_mvcc_threaded": {"counts": [1, 2, 4, 8, 16], "color": "tab:red", "marker": "D", "ls": "--"},
    }

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 6))

    for variant_name, cfg in variants.items():
        throughputs = []
        times = []
        counts_used = []
        for w in cfg["counts"]:
            total_rows = w * batches * rows_per_batch
            est = read_estimate("Writer Scalability", variant_name, w)
            if est:
                throughput = total_rows / (est / 1e9)  # rows/sec
                throughputs.append(throughput / 1000)  # Krows/sec
                times.append(est / 1e6)  # ms
                counts_used.append(w)

        if counts_used:
            label = variant_name.replace("limbo_", "").replace("_", " ").title()
            ax1.plot(counts_used, throughputs, marker=cfg["marker"], ls=cfg["ls"],
                     color=cfg["color"], label=label, linewidth=2, markersize=8)
            ax2.plot(counts_used, times, marker=cfg["marker"], ls=cfg["ls"],
                     color=cfg["color"], label=label, linewidth=2, markersize=8)

    ax1.set_xlabel("Number of Writers")
    ax1.set_ylabel("Throughput (Krows/sec)")
    ax1.set_title("Writer Scalability: Throughput")
    ax1.legend(fontsize=9)
    ax1.set_xticks([1, 2, 4, 8, 16])
    ax1.grid(True, alpha=0.3)

    ax2.set_xlabel("Number of Writers")
    ax2.set_ylabel("Total Time (ms)")
    ax2.set_title("Writer Scalability: Latency")
    ax2.legend(fontsize=9)
    ax2.set_xticks([1, 2, 4, 8, 16])
    ax2.grid(True, alpha=0.3)

    plt.tight_layout()
    plt.savefig(os.path.join(OUTPUT_DIR, "writer_scalability.png"), dpi=150)
    plt.close()
    print("  Saved writer_scalability.png")


def plot_contention_patterns():
    """Plot throughput for different contention patterns."""
    patterns = ["separate_tables", "disjoint_keys", "overlapping_keys", "hot_rows_update"]
    variants = ["mvcc_cooperative", "mvcc_threaded"]
    colors = {"mvcc_cooperative": "tab:blue", "mvcc_threaded": "tab:red"}
    total_rows = 4 * 5 * 10  # 4 writers, 5 batches, 10 rows

    fig, ax = plt.subplots(figsize=(10, 6))

    x = np.arange(len(patterns))
    width = 0.35

    for i, variant in enumerate(variants):
        throughputs = []
        for pat in patterns:
            est = read_estimate("Write Contention", variant, pat)
            if est:
                throughput = total_rows / (est / 1e9)
                throughputs.append(throughput / 1000)
            else:
                throughputs.append(0)

        label = variant.replace("mvcc_", "MVCC ").title()
        ax.bar(x + i * width, throughputs, width, label=label, color=colors[variant], alpha=0.8)

    ax.set_xlabel("Contention Pattern")
    ax.set_ylabel("Throughput (Krows/sec)")
    ax.set_title("Write Contention Patterns (4 MVCC Writers)")
    ax.set_xticks(x + width / 2)
    ax.set_xticklabels([p.replace("_", " ").title() for p in patterns], rotation=15)
    ax.legend()
    ax.grid(True, alpha=0.3, axis="y")

    plt.tight_layout()
    plt.savefig(os.path.join(OUTPUT_DIR, "contention_patterns.png"), dpi=150)
    plt.close()
    print("  Saved contention_patterns.png")


def plot_write_burst():
    """Plot throughput for write burst at different writer counts."""
    writer_counts = [4, 8, 16]
    rows_per_writer = 50
    variants = {
        "limbo_mvcc_threaded": {"color": "tab:red", "marker": "D"},
        "limbo_wal_threaded": {"color": "tab:green", "marker": "^"},
    }

    fig, ax = plt.subplots(figsize=(8, 6))

    for variant_name, cfg in variants.items():
        throughputs = []
        counts_used = []
        for w in writer_counts:
            total_rows = w * rows_per_writer
            est = read_estimate("Write Burst", variant_name, w)
            if est:
                throughput = total_rows / (est / 1e9)
                throughputs.append(throughput / 1000)
                counts_used.append(w)

        if counts_used:
            label = variant_name.replace("limbo_", "").replace("_", " ").title()
            ax.plot(counts_used, throughputs, marker=cfg["marker"],
                    color=cfg["color"], label=label, linewidth=2, markersize=8)

    ax.set_xlabel("Number of Simultaneous Writers")
    ax.set_ylabel("Throughput (Krows/sec)")
    ax.set_title("Write Burst (Thundering Herd)")
    ax.set_xticks(writer_counts)
    ax.legend()
    ax.grid(True, alpha=0.3)

    plt.tight_layout()
    plt.savefig(os.path.join(OUTPUT_DIR, "write_burst.png"), dpi=150)
    plt.close()
    print("  Saved write_burst.png")


def plot_mixed_workload():
    """Plot mixed workload result as a simple bar chart."""
    fig, ax = plt.subplots(figsize=(6, 5))

    total_ops = (4 + 2) * 50  # 4 readers + 2 writers, 50 ops each

    est = read_estimate("Mixed Workload", "limbo_mvcc_threaded", None)
    # Mixed workload doesn't have a parameter subdirectory
    path = os.path.join(CRITERION_DIR, "Mixed Workload", "limbo_mvcc_threaded", "new", "estimates.json")
    if os.path.exists(path):
        with open(path) as f:
            data = json.load(f)
        est_ns = data["mean"]["point_estimate"]
        throughput = total_ops / (est_ns / 1e9) / 1000
        time_ms = est_ns / 1e6

        ax.bar(["MVCC Threaded\n(4R + 2W)"], [throughput], color="tab:red", alpha=0.8, width=0.4)
        ax.set_ylabel("Throughput (Kops/sec)")
        ax.set_title(f"Mixed Read-Write Workload (YCSB-B Style)\n{time_ms:.2f} ms for {total_ops} ops")
        ax.grid(True, alpha=0.3, axis="y")

    plt.tight_layout()
    plt.savefig(os.path.join(OUTPUT_DIR, "mixed_workload.png"), dpi=150)
    plt.close()
    print("  Saved mixed_workload.png")


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    print("Generating summary plots from Criterion data...")
    plot_writer_scalability()
    plot_contention_patterns()
    plot_write_burst()
    plot_mixed_workload()
    print(f"\nAll plots saved to {OUTPUT_DIR}/")


if __name__ == "__main__":
    main()
