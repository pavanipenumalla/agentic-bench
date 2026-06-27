#!/usr/bin/env python3
"""
Experimental analysis (does NOT touch the production compare_reports.py):

  1. Session-duration CDF using TWO fixed request-count buckets
     (2-12 reqs, 12-151 reqs) instead of the 4 quartile buckets.
     -> two graphs, one per bucket, overlaying all strategies.

  2. Per-strategy TTFT / ITL analysis from summary_lifecycle_metrics.json:
     -> percentile table, approximate CDF curves, and grouped bar charts.

Usage:
    python3 experiment_2bucket_latency.py \
        --las  <las/reports> \
        --rr   <rr/reports> \
        --no-fairness <no-fairness/reports> \
        -o <output_dir>

Only succeeded sessions are used for duration plots (failed sessions have
truncated durations). TTFT/ITL come from the "successes" block of the summary.
"""

import argparse
import json
import os
from typing import Dict, List

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

# Fixed request-count buckets: [lo, hi). Last bucket is inclusive of max.
BUCKETS = [(2, 12, "2-12 reqs"), (12, 152, "12-151 reqs")]

STRATEGY_STYLES = {
    "LAS":         {"linestyle": "--", "marker": "s", "color": plt.cm.Set2.colors[1]},
    "RR":          {"linestyle": ":",  "marker": "D", "color": plt.cm.Set2.colors[2]},
    "No-Fairness": {"linestyle": "-.", "marker": "x", "color": plt.cm.Set2.colors[3]},
}

# Percentile points available in the summary (label -> fraction for CDF y-axis).
PCTL_KEYS = [
    ("min", 0.0), ("p0.1", 0.001), ("p1", 0.01), ("p5", 0.05), ("p10", 0.10),
    ("p25", 0.25), ("median", 0.50), ("p75", 0.75), ("p90", 0.90),
    ("p95", 0.95), ("p99", 0.99), ("p99.9", 0.999), ("max", 1.0),
]


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def load_sessions(report_dir: str) -> List[dict]:
    path = os.path.join(report_dir, "per_session_lifecycle_metrics.json")
    with open(path) as f:
        return json.load(f)


def load_summary(report_dir: str) -> dict:
    path = os.path.join(report_dir, "summary_lifecycle_metrics.json")
    with open(path) as f:
        return json.load(f)


def bucket_of(num_events: int):
    for lo, hi, label in BUCKETS:
        if lo <= num_events < hi:
            return label
    return None


# ---------------------------------------------------------------------------
# 1. Duration CDF — 2 fixed buckets
# ---------------------------------------------------------------------------

def plot_duration_cdfs(strategies: Dict[str, List[dict]], out_dir: str):
    for lo, hi, label in BUCKETS:
        fig, ax = plt.subplots(figsize=(8, 5))
        for name, sessions in strategies.items():
            style = STRATEGY_STYLES[name]
            durs = sorted(
                s["duration_sec"] for s in sessions
                if s.get("success", True) and bucket_of(s["num_events"]) == label
            )
            total = sum(1 for s in sessions if bucket_of(s["num_events"]) == label)
            if not durs:
                continue
            ys = np.arange(1, len(durs) + 1) / len(durs)
            ax.plot(durs, ys, color=style["color"], linestyle=style["linestyle"],
                    marker=style["marker"], markevery=max(1, len(durs) // 20),
                    markersize=5, linewidth=1.8, alpha=0.85,
                    label=f"{name} (n={len(durs)}/{total})")
        ax.set_xlabel("Session Duration (s)", fontsize=11)
        ax.set_ylabel("CDF (fraction of succeeded sessions)", fontsize=11)
        ax.set_ylim(0, 1.05)
        ax.set_title(f"Session-Duration CDF — {label}\n(Succeeded sessions only)", fontsize=12)
        ax.legend(fontsize=9, loc="lower right")
        ax.grid(alpha=0.3)
        fig.tight_layout()
        fname = os.path.join(out_dir, f"duration_cdf_{label.split()[0]}_reqs.png")
        fig.savefig(fname, dpi=150, bbox_inches="tight")
        plt.close(fig)
        print(f"[experiment] Wrote {fname}")

    # Combined 2-panel for convenience
    fig, axes = plt.subplots(1, 2, figsize=(15, 5.5), sharey=True)
    for ax, (lo, hi, label) in zip(axes, BUCKETS):
        for name, sessions in strategies.items():
            style = STRATEGY_STYLES[name]
            durs = sorted(
                s["duration_sec"] for s in sessions
                if s.get("success", True) and bucket_of(s["num_events"]) == label
            )
            if not durs:
                continue
            ys = np.arange(1, len(durs) + 1) / len(durs)
            ax.plot(durs, ys, color=style["color"], linestyle=style["linestyle"],
                    marker=style["marker"], markevery=max(1, len(durs) // 20),
                    markersize=5, linewidth=1.8, alpha=0.85, label=f"{name} (n={len(durs)})")
        ax.set_title(label, fontsize=12)
        ax.set_xlabel("Session Duration (s)", fontsize=11)
        ax.set_ylim(0, 1.05)
        ax.grid(alpha=0.3)
        ax.legend(fontsize=9, loc="lower right")
    axes[0].set_ylabel("CDF (fraction of succeeded sessions)", fontsize=11)
    fig.suptitle("Session-Duration CDF — 2 fixed buckets (succeeded only)", fontsize=13)
    fig.tight_layout(rect=[0, 0, 1, 0.96])
    fname = os.path.join(out_dir, "duration_cdf_2buckets_combined.png")
    fig.savefig(fname, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"[experiment] Wrote {fname}")


# ---------------------------------------------------------------------------
# 2. TTFT / ITL analysis
# ---------------------------------------------------------------------------

def latency_block(summary: dict, key: str) -> dict:
    return summary["successes"]["latency"][key]


def plot_latency_cdf(summaries: Dict[str, dict], key: str, title: str,
                     xlabel: str, scale: float, out_path: str):
    """Approximate CDF built from the percentile points in the summary."""
    fig, ax = plt.subplots(figsize=(8, 5))
    for name, summ in summaries.items():
        style = STRATEGY_STYLES[name]
        blk = latency_block(summ, key)
        xs, ys = [], []
        for pk, frac in PCTL_KEYS:
            if pk in blk:
                xs.append(blk[pk] * scale)
                ys.append(frac)
        ax.plot(xs, ys, color=style["color"], linestyle=style["linestyle"],
                marker=style["marker"], markersize=6, linewidth=1.8, alpha=0.85,
                label=name)
    ax.set_xlabel(xlabel, fontsize=11)
    ax.set_ylabel("CDF (fraction of succeeded requests)", fontsize=11)
    ax.set_ylim(0, 1.05)
    ax.set_title(title, fontsize=12)
    ax.legend(fontsize=9, loc="lower right")
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"[experiment] Wrote {out_path}")


def plot_latency_bars(summaries: Dict[str, dict], out_path: str):
    """Grouped bars: p50/p90/p99 for TTFT (s) and ITL (ms)."""
    names = list(summaries.keys())
    pctls = [("median", "p50"), ("p90", "p90"), ("p99", "p99")]
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(13, 5))

    def grouped(ax, key, scale, ylabel, title):
        x = np.arange(len(pctls))
        w = 0.8 / len(names)
        for i, name in enumerate(names):
            blk = latency_block(summaries[name], key)
            vals = [blk[pk] * scale for pk, _ in pctls]
            ax.bar(x + i * w, vals, w, label=name,
                   color=STRATEGY_STYLES[name]["color"])
        ax.set_xticks(x + w * (len(names) - 1) / 2)
        ax.set_xticklabels([lab for _, lab in pctls])
        ax.set_ylabel(ylabel, fontsize=11)
        ax.set_title(title, fontsize=12)
        ax.legend(fontsize=9)
        ax.grid(alpha=0.3, axis="y")

    grouped(ax1, "time_to_first_token", 1.0, "TTFT (s)", "Time To First Token")
    grouped(ax2, "inter_token_latency", 1000.0, "ITL (ms)", "Inter-Token Latency")
    fig.tight_layout()
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"[experiment] Wrote {out_path}")


def write_latency_table(summaries: Dict[str, dict], out_path: str):
    lines = ["Latency comparison (succeeded requests only)\n"]
    specs = [
        ("time_to_first_token", "TTFT", "s", 1.0),
        ("inter_token_latency", "ITL", "ms", 1000.0),
        ("time_per_output_token", "TPOT", "ms", 1000.0),
        ("request_latency", "ReqLatency", "s", 1.0),
    ]
    for key, label, unit, scale in specs:
        lines.append(f"\n== {label} ({unit}) ==")
        header = f"{'Strategy':<14}{'count':>8}{'mean':>10}{'p50':>10}{'p90':>10}{'p99':>10}"
        lines.append(header)
        lines.append("-" * len(header))
        for name, summ in summaries.items():
            blk = latency_block(summ, key)
            cnt = summ["successes"]["count"]
            lines.append(
                f"{name:<14}{cnt:>8}"
                f"{blk['mean']*scale:>10.3f}{blk['median']*scale:>10.3f}"
                f"{blk['p90']*scale:>10.3f}{blk['p99']*scale:>10.3f}"
            )
    text = "\n".join(lines) + "\n"
    with open(out_path, "w") as f:
        f.write(text)
    print(f"[experiment] Wrote {out_path}")
    print(text)


# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--las")
    ap.add_argument("--rr")
    ap.add_argument("--no-fairness", dest="no_fairness")
    ap.add_argument("-o", "--out", required=True)
    args = ap.parse_args()

    dirs = {"LAS": args.las, "RR": args.rr, "No-Fairness": args.no_fairness}
    dirs = {k: v for k, v in dirs.items() if v}
    os.makedirs(args.out, exist_ok=True)

    sessions = {name: load_sessions(d) for name, d in dirs.items()}
    summaries = {name: load_summary(d) for name, d in dirs.items()}

    plot_duration_cdfs(sessions, args.out)
    write_latency_table(summaries, os.path.join(args.out, "latency_percentile_table.txt"))
    plot_latency_cdf(summaries, "time_to_first_token",
                     "TTFT CDF by Strategy", "Time To First Token (s)", 1.0,
                     os.path.join(args.out, "ttft_cdf.png"))
    plot_latency_cdf(summaries, "inter_token_latency",
                     "ITL CDF by Strategy", "Inter-Token Latency (ms)", 1000.0,
                     os.path.join(args.out, "itl_cdf.png"))
    plot_latency_bars(summaries, os.path.join(args.out, "ttft_itl_bars.png"))
    print(f"\n[experiment] Done — output in {args.out}")


if __name__ == "__main__":
    main()
