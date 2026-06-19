#!/usr/bin/env python3
"""
Analyze agentic-trace report folders and produce session duration plots.

Uses quartile-based grouping by LLM call count (num_events) per session.
Reads per_session_lifecycle_metrics.json from the report directory.

Duration plots only include succeeded sessions (failed sessions have
truncated durations). Failure analysis plots are generated separately.

Usage:
    python3 analyze_reports.py reports-two-sessions-sequential
    python3 analyze_reports.py reports-two-sessions-sequential -o /out/
"""

import argparse
import json
import os
from typing import Dict, List, Tuple

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np


def _get_session_id(session: dict, index: int) -> str:
    """Get a stable session identifier, falling back to index if no ID field exists."""
    return session.get("session_id", session.get("file_path", f"session_{index}"))


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_per_session_metrics(report_dir: str) -> List[dict]:
    path = os.path.join(report_dir, "per_session_lifecycle_metrics.json")
    if not os.path.exists(path):
        print(f"[analyze_reports] ERROR: {path} not found. "
              f"Ensure per_session: true is set in the report config.")
        return []
    with open(path) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Color mapping — quartile bins by num_events (LLM call count)
# ---------------------------------------------------------------------------

def _build_request_bins(totals: Dict[str, int]):
    vals = sorted(totals.values())
    n = len(vals)
    if n == 0:
        return {}, {}

    q1, q2, q3 = vals[n // 4], vals[n // 2], vals[3 * n // 4]
    unique_edges = sorted(set([vals[0], q1, q2, q3, vals[-1]]))

    if len(unique_edges) < 3:
        label = f"{int(vals[0])}-{int(vals[-1])} reqs"
        return {pid: label for pid in totals}, {label: plt.cm.tab10.colors[0]}

    bins = []
    for i in range(len(unique_edges) - 1):
        lo, hi = unique_edges[i], unique_edges[i + 1]
        bins.append((lo, hi, f"{int(lo)}-{int(hi)} reqs", i == len(unique_edges) - 2))

    colors = plt.cm.tab10.colors
    group_to_color = {label: colors[i % len(colors)] for i, (_, _, label, _) in enumerate(bins)}

    pid_to_group = {}
    for pid, total in totals.items():
        for lo, hi, label, is_last in bins:
            if (is_last and total >= lo) or (not is_last and lo <= total < hi):
                pid_to_group[pid] = label
                break
        else:
            pid_to_group[pid] = bins[-1][2]

    return pid_to_group, group_to_color


# ---------------------------------------------------------------------------
# Plot: Program duration scatter (succeeded sessions only)
# ---------------------------------------------------------------------------

def plot_program_duration(sessions, session_ids, out_path, pid_to_group,
                          group_to_color, req_totals, total_sessions, failed_count):
    if not sessions:
        return

    fig, ax = plt.subplots(figsize=(12, 6))
    seen_groups: set = set()
    min_start = min(s["start_time"] for s in sessions)

    for sid, session in zip(session_ids, sessions):
        x = session["start_time"] - min_start
        y = session["duration_sec"]
        n_reqs = int(req_totals.get(sid, 0))

        group = pid_to_group.get(sid, "unknown")
        color = group_to_color.get(group, (0.5, 0.5, 0.5))
        label = group if group not in seen_groups else "_nolegend_"
        seen_groups.add(group)
        ax.scatter([x], [y], color=color, s=50, zorder=3, label=label)
        ax.annotate(str(n_reqs), (x, y), textcoords="offset points",
                    xytext=(5, 5), fontsize=7, alpha=0.8)

    succeeded_count = len(sessions)
    ax.set_xlabel("Session Start Time (s)")
    ax.set_ylabel("Session Duration (s)")
    ax.set_title(
        f"Session Duration vs Start Time — Succeeded Only\n"
        f"(N={succeeded_count} succeeded, {failed_count} failed, {total_sessions} total)",
        fontsize=11)
    ax.legend(fontsize=8, loc="upper left", bbox_to_anchor=(1.02, 1.0), ncol=1)
    ax.grid(alpha=0.3)
    fig.tight_layout(rect=[0, 0, 0.88, 1])
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"[analyze_reports] Wrote {out_path}")


# ---------------------------------------------------------------------------
# Plot: Failure analysis
# ---------------------------------------------------------------------------

def plot_failure_analysis(all_sessions, session_ids, pid_to_group, group_to_color,
                          out_path):
    """Bar chart of success rate per category + completion fraction for failed sessions."""
    if not all_sessions:
        return

    groups_sorted = sorted(group_to_color.keys(),
                           key=lambda g: int(g.split("-")[0]))

    # Compute success rate per category
    group_total = {g: 0 for g in groups_sorted}
    group_succeeded = {g: 0 for g in groups_sorted}
    failed_completion_fractions = []

    for sid, s in zip(session_ids, all_sessions):
        group = pid_to_group.get(sid, None)
        if group is None:
            continue
        group_total[group] += 1
        if s.get("success", False):
            group_succeeded[group] += 1
        else:
            num_events = s.get("num_events", 1)
            num_completed = s.get("num_events_completed", 0)
            if num_events > 0:
                failed_completion_fractions.append(num_completed / num_events)

    fig, axes = plt.subplots(1, 2, figsize=(14, 5))

    # Panel 1: Success rate per category
    ax = axes[0]
    x_pos = range(len(groups_sorted))
    rates = [group_succeeded[g] / group_total[g] if group_total[g] > 0 else 0
             for g in groups_sorted]
    bar_colors = [group_to_color[g] for g in groups_sorted]
    bars = ax.bar(x_pos, rates, color=bar_colors, edgecolor="white", width=0.6)
    for bar, g in zip(bars, groups_sorted):
        count_text = f"{group_succeeded[g]}/{group_total[g]}"
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.02,
                count_text, ha="center", va="bottom", fontsize=9)
    ax.set_xticks(x_pos)
    ax.set_xticklabels(groups_sorted, fontsize=9)
    ax.set_ylabel("Success Rate")
    ax.set_ylim(0, 1.15)
    ax.set_title("Session Success Rate by Category")
    ax.grid(alpha=0.3, axis="y")

    # Panel 2: Completion fraction CDF for failed sessions
    ax = axes[1]
    if failed_completion_fractions:
        vals = sorted(failed_completion_fractions)
        ys = np.arange(1, len(vals) + 1) / len(vals)
        ax.plot(vals, ys, linewidth=2, color="firebrick")
        ax.set_xlabel("Completion Fraction (events_completed / total_events)")
        ax.set_ylabel("CDF (fraction of failed sessions)")
        ax.set_title(f"Where Do Sessions Fail? (n={len(vals)} failed)")
        ax.set_xlim(0, 1.05)
        ax.set_ylim(0, 1.05)
        ax.grid(alpha=0.3)
    else:
        ax.text(0.5, 0.5, "No failed sessions", ha="center", va="center",
                fontsize=12, transform=ax.transAxes)
        ax.set_title("Where Do Sessions Fail?")

    fig.tight_layout()
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"[analyze_reports] Wrote {out_path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Plot session duration from per_session_lifecycle_metrics.json")
    parser.add_argument("report_dir", help="Path to the report directory")
    parser.add_argument("-o", "--output-dir", default=None,
                        help="Output directory for plots (default: <report_dir>/report_plots)")
    args = parser.parse_args()

    report_dir = args.report_dir
    if not os.path.isdir(report_dir):
        print(f"[analyze_reports] Not a directory: {report_dir}")
        return

    all_sessions = load_per_session_metrics(report_dir)
    if not all_sessions:
        return

    total_sessions = len(all_sessions)

    # Assign stable session IDs (using original index as fallback)
    all_session_ids = [_get_session_id(s, i) for i, s in enumerate(all_sessions)]

    succeeded_indices = [i for i, s in enumerate(all_sessions) if s.get("success", False)]
    succeeded_sessions = [all_sessions[i] for i in succeeded_indices]
    succeeded_session_ids = [all_session_ids[i] for i in succeeded_indices]
    failed_count = total_sessions - len(succeeded_sessions)

    print(f"[analyze_reports] Loaded {total_sessions} sessions "
          f"({len(succeeded_sessions)} succeeded, {failed_count} failed)")

    # Bin using ALL sessions' num_events (so categories are consistent)
    all_req_totals = {all_session_ids[i]: s["num_events"]
                      for i, s in enumerate(all_sessions)}
    pid_to_group_all, group_to_color = _build_request_bins(all_req_totals)

    # Build mapping for succeeded sessions — same IDs, no re-indexing needed
    succeeded_req_totals = {sid: all_req_totals[sid] for sid in succeeded_session_ids}
    pid_to_group_succeeded = {sid: pid_to_group_all[sid]
                              for sid in succeeded_session_ids}

    for group in sorted(group_to_color.keys()):
        total_in_group = sum(1 for g in pid_to_group_all.values() if g == group)
        succeeded_in_group = sum(1 for g in pid_to_group_succeeded.values() if g == group)
        print(f"[analyze_reports]   {group}: {succeeded_in_group}/{total_in_group} sessions succeeded")

    out_dir = args.output_dir or report_dir
    plots_dir = os.path.join(out_dir, "report_plots")
    os.makedirs(plots_dir, exist_ok=True)

    # Duration plot: succeeded sessions only
    plot_program_duration(succeeded_sessions, succeeded_session_ids,
                          os.path.join(plots_dir, "program_duration.png"),
                          pid_to_group_succeeded, group_to_color,
                          succeeded_req_totals, total_sessions, failed_count)

    # Failure analysis plot: all sessions
    plot_failure_analysis(all_sessions, all_session_ids, pid_to_group_all,
                          group_to_color,
                          os.path.join(plots_dir, "failure_analysis.png"))

    print(f"[analyze_reports] All plots written to {plots_dir}/")


if __name__ == "__main__":
    main()
