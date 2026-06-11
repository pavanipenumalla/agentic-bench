#!/usr/bin/env python3
"""
Analyze inference-perf session lifecycle reports from OTel trace replay.

Produces scatter plots of session duration vs start time, colored by
request count quartiles.

Usage:
    python3 analyze_reports.py results/las-halflife/reports -o results/las-halflife/
"""

import argparse
import json
import os
from typing import Dict, List

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_per_session_metrics(report_dir: str) -> List[dict]:
    """Load per-session lifecycle metrics."""
    path = os.path.join(report_dir, "per_session_lifecycle_metrics.json")
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return json.load(f)


def load_summary(report_dir: str) -> dict:
    """Load summary session lifecycle metrics."""
    path = os.path.join(report_dir, "summary_session_lifecycle_metrics.json")
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Grouping by request count (quartile bins)
# ---------------------------------------------------------------------------

def build_request_bins(sessions: List[dict]):
    """Group sessions into quartile bins by num_events (LLM calls per session)."""
    counts = {i: s.get("num_events", 0) for i, s in enumerate(sessions)}
    vals = sorted(counts.values())
    n = len(vals)
    if n == 0:
        return {}, {}

    q1, q2, q3 = vals[n // 4], vals[n // 2], vals[3 * n // 4]
    unique_edges = sorted(set([vals[0], q1, q2, q3, vals[-1]]))

    if len(unique_edges) < 3:
        label = f"{int(vals[0])}-{int(vals[-1])} calls"
        return {i: label for i in counts}, {label: plt.cm.tab10.colors[0]}

    bins = []
    for i in range(len(unique_edges) - 1):
        lo, hi = unique_edges[i], unique_edges[i + 1]
        bins.append((lo, hi, f"{int(lo)}-{int(hi)} calls", i == len(unique_edges) - 2))

    colors = plt.cm.tab10.colors
    group_to_color = {label: colors[i % len(colors)] for i, (_, _, label, _) in enumerate(bins)}

    idx_to_group = {}
    for idx, count in counts.items():
        for lo, hi, label, is_last in bins:
            if (is_last and count >= lo) or (not is_last and lo <= count < hi):
                idx_to_group[idx] = label
                break
        else:
            idx_to_group[idx] = bins[-1][2]

    return idx_to_group, group_to_color


# ---------------------------------------------------------------------------
# Plots
# ---------------------------------------------------------------------------

def plot_session_duration_scatter(sessions: List[dict], idx_to_group, group_to_color, out_path):
    """Scatter plot of session duration vs start time."""
    if not sessions:
        return

    fig, ax = plt.subplots(figsize=(12, 6))
    seen_groups = set()

    # Find earliest start time for relative positioning
    start_times = [s.get("start_time", 0) for s in sessions]
    t0 = min(start_times) if start_times else 0

    for i, session in enumerate(sessions):
        x = session.get("start_time", 0) - t0
        y = session.get("duration_sec", 0)
        n_events = session.get("num_events", 0)

        group = idx_to_group.get(i, "unknown")
        color = group_to_color.get(group, (0.5, 0.5, 0.5))
        label = group if group not in seen_groups else "_nolegend_"
        seen_groups.add(group)

        marker = "o" if session.get("success", True) else "x"
        ax.scatter([x], [y], color=color, s=50, zorder=3, label=label, marker=marker)
        ax.annotate(str(n_events), (x, y), textcoords="offset points",
                    xytext=(5, 5), fontsize=7, alpha=0.8)

    ax.set_xlabel("Session Start Time (s, relative)")
    ax.set_ylabel("Session Duration (s)")
    ax.set_title("Session Duration vs Start Time (o=success, x=failed)")
    ax.legend(fontsize=8, loc="upper left", bbox_to_anchor=(1.02, 1.0), ncol=1)
    ax.grid(alpha=0.3)
    fig.tight_layout(rect=[0, 0, 0.88, 1])
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"[analyze_reports] Wrote {out_path}")


def plot_success_rate(sessions: List[dict], out_path):
    """Bar chart of success vs failure."""
    if not sessions:
        return

    success = sum(1 for s in sessions if s.get("success", True))
    failed = len(sessions) - success

    fig, ax = plt.subplots(figsize=(6, 4))
    bars = ax.bar(["Success", "Failed"], [success, failed],
                  color=["#4CAF50", "#F44336"], edgecolor="white", width=0.5)
    for bar, val in zip(bars, [success, failed]):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height(),
                str(val), ha="center", va="bottom", fontsize=11)

    ax.set_ylabel("Session Count")
    ax.set_title(f"Session Outcomes ({len(sessions)} total)")
    ax.grid(alpha=0.3, axis="y")
    fig.tight_layout()
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"[analyze_reports] Wrote {out_path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Analyze session lifecycle reports from OTel trace replay")
    parser.add_argument("report_dir", help="Path to the report directory")
    parser.add_argument("-o", "--output-dir", default=None,
                        help="Output directory for plots (default: report_dir parent)")
    args = parser.parse_args()

    report_dir = args.report_dir
    if not os.path.isdir(report_dir):
        print(f"[analyze_reports] Not a directory: {report_dir}")
        return

    sessions = load_per_session_metrics(report_dir)
    if not sessions:
        print(f"[analyze_reports] No per_session_lifecycle_metrics.json in {report_dir}")
        return

    print(f"[analyze_reports] Loaded {len(sessions)} sessions from {report_dir}")

    success = sum(1 for s in sessions if s.get("success", True))
    print(f"[analyze_reports] Success: {success}/{len(sessions)} "
          f"({100*success/len(sessions):.1f}%)")

    idx_to_group, group_to_color = build_request_bins(sessions)
    for group in sorted(group_to_color.keys()):
        count = sum(1 for g in idx_to_group.values() if g == group)
        print(f"[analyze_reports]   {group}: {count} sessions")

    out_dir = args.output_dir or os.path.dirname(report_dir)
    plots_dir = os.path.join(out_dir, "plots")
    os.makedirs(plots_dir, exist_ok=True)

    plot_session_duration_scatter(sessions, idx_to_group, group_to_color,
                                 os.path.join(plots_dir, "session_duration_scatter.png"))
    plot_success_rate(sessions, os.path.join(plots_dir, "session_outcomes.png"))

    print(f"[analyze_reports] All plots written to {plots_dir}/")


if __name__ == "__main__":
    main()
