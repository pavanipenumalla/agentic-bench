#!/usr/bin/env python3
"""
Compare session metrics across scheduling strategies from OTel trace replay.

Produces CDFs, bar charts, and percentile tables for session duration,
grouped by LLM call count quartiles and differentiated by strategy.

Accepts any number of strategies via --<name> <path> arguments:

    python3 compare_reports.py \
        --las-halflife results/exp-01/las-halflife/reports \
        --las-default  results/exp-01/las-default/reports \
        --rr           results/exp-01/rr/reports \
        -o results/exp-01/comparison/
"""

import argparse
import json
import os
import sys
from typing import Dict, List, Tuple

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np


# ---------------------------------------------------------------------------
# Strategy visual styles
# ---------------------------------------------------------------------------

_KNOWN_STYLES = {
    "LAS-HALFLIFE": {"linestyle": "-",  "marker": "o", "markevery": 10},
    "LAS-DEFAULT":  {"linestyle": "--", "marker": "s", "markevery": 10},
    "RR":           {"linestyle": ":",  "marker": "D", "markevery": 10},
}

_FALLBACK_STYLES = [
    {"linestyle": "-.",  "marker": "^", "markevery": 10},
    {"linestyle": (0, (3, 1, 1, 1)), "marker": "v", "markevery": 10},
    {"linestyle": (0, (5, 2)), "marker": "P", "markevery": 10},
]


def get_strategy_styles(strategy_names: List[str]) -> Dict[str, dict]:
    styles = {}
    fallback_idx = 0
    for name in strategy_names:
        if name in _KNOWN_STYLES:
            styles[name] = _KNOWN_STYLES[name]
        else:
            styles[name] = _FALLBACK_STYLES[fallback_idx % len(_FALLBACK_STYLES)]
            fallback_idx += 1
    return styles


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


def build_request_bins(all_sessions: List[dict]):
    """Group sessions by num_events quartiles (across all strategies combined)."""
    counts = sorted(s.get("num_events", 0) for s in all_sessions)
    n = len(counts)
    if n == 0:
        return {}, {}

    q1, q2, q3 = counts[n // 4], counts[n // 2], counts[3 * n // 4]
    unique_edges = sorted(set([counts[0], q1, q2, q3, counts[-1]]))

    if len(unique_edges) < 3:
        label = f"{int(counts[0])}-{int(counts[-1])} calls"
        return lambda s: label, {label: plt.cm.tab10.colors[0]}

    bins = []
    for i in range(len(unique_edges) - 1):
        lo, hi = unique_edges[i], unique_edges[i + 1]
        bins.append((lo, hi, f"{int(lo)}-{int(hi)} calls", i == len(unique_edges) - 2))

    colors = plt.cm.tab10.colors
    group_to_color = {label: colors[i % len(colors)] for i, (_, _, label, _) in enumerate(bins)}

    def get_group(session):
        count = session.get("num_events", 0)
        for lo, hi, label, is_last in bins:
            if (is_last and count >= lo) or (not is_last and lo <= count < hi):
                return label
        return bins[-1][2]

    return get_group, group_to_color


# ---------------------------------------------------------------------------
# Plots
# ---------------------------------------------------------------------------

def plot_duration_cdf(strategy_sessions: Dict[str, List[dict]],
                      strategy_styles: Dict[str, dict],
                      get_group, group_to_color, out_path: str):
    """CDF of session duration per strategy."""
    fig, ax = plt.subplots(figsize=(10, 6))

    strat_colors = plt.cm.Set2.colors
    for i, (strategy, sessions) in enumerate(sorted(strategy_sessions.items())):
        style = strategy_styles.get(strategy, {"linestyle": "-", "marker": "x", "markevery": 10})
        durations = sorted(s.get("duration_sec", 0) for s in sessions if s.get("success", True))
        if not durations:
            continue
        ys = np.arange(1, len(durations) + 1) / len(durations)
        me = max(1, len(durations) // 15)
        ax.plot(durations, ys,
                color=strat_colors[i % len(strat_colors)],
                linestyle=style["linestyle"],
                marker=style["marker"],
                markevery=me,
                markersize=5,
                linewidth=1.8,
                label=f"{strategy} (n={len(durations)})",
                alpha=0.85)

    ax.set_xlabel("Session Duration (s)", fontsize=11)
    ax.set_ylabel("CDF (fraction of sessions)", fontsize=11)
    ax.set_ylim(0, 1.05)
    ax.set_title("CDF of Session Duration by Strategy (successful sessions only)")
    ax.legend(fontsize=9)
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"[compare_reports] Wrote {out_path}")


def plot_success_rate_comparison(strategy_sessions: Dict[str, List[dict]], out_path: str):
    """Bar chart comparing success rates across strategies."""
    fig, ax = plt.subplots(figsize=(8, 5))
    names = sorted(strategy_sessions.keys())
    colors = plt.cm.Set2.colors

    rates = []
    for name in names:
        sessions = strategy_sessions[name]
        success = sum(1 for s in sessions if s.get("success", True))
        rates.append(100 * success / len(sessions) if sessions else 0)

    bars = ax.bar(names, rates,
                  color=[colors[i % len(colors)] for i in range(len(names))],
                  edgecolor="white", width=0.45)
    for bar, val in zip(bars, rates):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height(),
                f"{val:.1f}%", ha="center", va="bottom", fontsize=10)

    ax.set_ylabel("Session Success Rate (%)", fontsize=11)
    ax.set_title("Session Success Rate by Strategy")
    ax.set_ylim(0, 110)
    ax.grid(alpha=0.3, axis="y")
    fig.tight_layout()
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"[compare_reports] Wrote {out_path}")


def plot_avg_duration(strategy_sessions: Dict[str, List[dict]], out_path: str):
    """Bar chart of average session duration."""
    fig, ax = plt.subplots(figsize=(8, 5))
    names = sorted(strategy_sessions.keys())
    colors = plt.cm.Set2.colors

    avgs = []
    for name in names:
        durs = [s.get("duration_sec", 0) for s in strategy_sessions[name]
                if s.get("success", True)]
        avgs.append(sum(durs) / len(durs) if durs else 0)

    bars = ax.bar(names, avgs,
                  color=[colors[i % len(colors)] for i in range(len(names))],
                  edgecolor="white", width=0.45)
    for bar, val in zip(bars, avgs):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height(),
                f"{val:.1f}s", ha="center", va="bottom", fontsize=10)

    ax.set_ylabel("Avg Session Duration (s)", fontsize=11)
    ax.set_title("Average Session Duration by Strategy (successful only)")
    ax.grid(alpha=0.3, axis="y")
    fig.tight_layout()
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"[compare_reports] Wrote {out_path}")


def print_percentile_table(strategy_sessions: Dict[str, List[dict]], out_path: str = None):
    """Print and optionally save p50/p90/p99 of session duration per strategy."""
    strategies = sorted(strategy_sessions.keys())

    lines = []
    header = f"{'Strategy':<20s}  {'Count':>6s}  {'Success%':>8s}  {'p50':>7s}  {'p90':>7s}  {'p99':>7s}"
    lines.append(header)
    lines.append("-" * len(header))

    for strategy in strategies:
        sessions = strategy_sessions[strategy]
        total = len(sessions)
        success = sum(1 for s in sessions if s.get("success", True))
        durs = sorted(s.get("duration_sec", 0) for s in sessions if s.get("success", True))

        if durs:
            p50 = np.percentile(durs, 50)
            p90 = np.percentile(durs, 90)
            p99 = np.percentile(durs, 99)
            lines.append(f"{strategy:<20s}  {total:>6d}  {100*success/total:>7.1f}%  "
                         f"{p50:>7.1f}  {p90:>7.1f}  {p99:>7.1f}")
        else:
            lines.append(f"{strategy:<20s}  {total:>6d}  {0:>7.1f}%  {'—':>7s}  {'—':>7s}  {'—':>7s}")

    print("\n" + "\n".join(lines) + "\n")

    if out_path:
        with open(out_path, "w") as f:
            f.write("\n".join(lines) + "\n")
        print(f"[compare_reports] Wrote {out_path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_strategy_args(argv: List[str]) -> Tuple[Dict[str, str], str]:
    """Parse --<strategy> <path> pairs plus -o from argv."""
    parser = argparse.ArgumentParser(
        description="Compare session metrics across scheduling strategies",
        epilog="Strategies: --<name> <report-dir> pairs. At least 2 required.\n"
               "Example: %(prog)s --las-halflife path/to/reports --rr path/to/reports -o out/",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-o", "--output-dir", default="comparison",
                        help="Output directory (default: comparison)")

    args, unknown = parser.parse_known_args(argv)

    strategy_dirs = {}
    i = 0
    while i < len(unknown):
        if unknown[i].startswith("--") and i + 1 < len(unknown) and not unknown[i + 1].startswith("--"):
            name = unknown[i].lstrip("-").upper()
            strategy_dirs[name] = unknown[i + 1]
            i += 2
        else:
            parser.error(f"Unrecognized argument: {unknown[i]}")

    if len(strategy_dirs) < 2:
        parser.error("At least 2 strategies required (e.g., --las-halflife <path> --rr <path>)")

    return strategy_dirs, args.output_dir


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    strategy_dirs, output_dir = parse_strategy_args(sys.argv[1:])
    os.makedirs(output_dir, exist_ok=True)

    strategy_sessions = {}
    for name, report_dir in sorted(strategy_dirs.items()):
        if not os.path.isdir(report_dir):
            print(f"[compare_reports] WARNING: {name} not found: {report_dir}, skipping")
            continue
        sessions = load_per_session_metrics(report_dir)
        if not sessions:
            print(f"[compare_reports] WARNING: {name} has no session data, skipping")
            continue
        strategy_sessions[name] = sessions
        print(f"[compare_reports] {name}: {len(sessions)} sessions")

    if len(strategy_sessions) < 2:
        print("[compare_reports] ERROR: Need at least 2 strategies with valid data")
        return

    # Build grouping from all sessions combined
    all_sessions = [s for sessions in strategy_sessions.values() for s in sessions]
    get_group, group_to_color = build_request_bins(all_sessions)

    strategy_styles = get_strategy_styles(sorted(strategy_sessions.keys()))

    print_percentile_table(strategy_sessions,
                           os.path.join(output_dir, "percentile_table.txt"))
    plot_duration_cdf(strategy_sessions, strategy_styles, get_group, group_to_color,
                      os.path.join(output_dir, "session_duration_cdf.png"))
    plot_success_rate_comparison(strategy_sessions,
                                os.path.join(output_dir, "success_rate_comparison.png"))
    plot_avg_duration(strategy_sessions,
                      os.path.join(output_dir, "avg_session_duration.png"))

    print(f"[compare_reports] Done — output in {output_dir}/")


if __name__ == "__main__":
    main()
