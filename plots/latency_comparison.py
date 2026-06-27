#!/usr/bin/env python3
"""
Compare per-request latency (TTFT, ITL, E2E) across scheduling strategies.

Reads summary_lifecycle_metrics.json from each strategy's report dir and uses
the percentile distributions in the "successes" block to plot approximate CDFs
and a percentile table. Produces:
  - latency_table.txt          (TTFT / ITL / E2E p50/p90/p99/mean per strategy)
  - ttft_cdf.png, itl_cdf.png, e2e_cdf.png
  - latency_bars.png           (grouped p50/p90/p99)

IMPORTANT: latencies are over SUCCEEDED requests only. No-Fairness completes far
fewer sessions (survivorship), so its latencies are NOT comparable at face value
— the table prints success counts so the bias is visible.

Usage:
  python3 latency_comparison.py --las <dir> --rr <dir> --no-fairness <dir> -o <out>
"""

import argparse
import json
import os
from typing import Dict

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

RUN = "run"  # set from --label in main; appears in titles

STYLES = {
    "LAS":         {"linestyle": "--", "marker": "s", "color": plt.cm.Set2.colors[1]},
    "RR":          {"linestyle": ":",  "marker": "D", "color": plt.cm.Set2.colors[2]},
    "No-Fairness": {"linestyle": "-.", "marker": "x", "color": plt.cm.Set2.colors[3]},
}

# (summary key, label, unit, scale-to-unit)
METRICS = [
    ("time_to_first_token", "TTFT", "s",  1.0),
    ("inter_token_latency", "ITL",  "ms", 1000.0),
    ("request_latency",     "E2E",  "s",  1.0),
]

# percentile label -> CDF fraction
PCTL = [("min", 0.0), ("p1", .01), ("p5", .05), ("p10", .10), ("p25", .25),
        ("median", .50), ("p75", .75), ("p90", .90), ("p95", .95),
        ("p99", .99), ("p99.9", .999), ("max", 1.0)]


def load(report_dir):
    with open(os.path.join(report_dir, "summary_lifecycle_metrics.json")) as f:
        return json.load(f)


def load_session(report_dir):
    with open(os.path.join(report_dir, "summary_session_lifecycle_metrics.json")) as f:
        return json.load(f)


def completion_ceiling(report_dir):
    """Fraction of INTENDED work (scheduled events) that actually completed.
    This is the honest denominator: cancelled/abandoned events never become a
    latency datapoint, so succeeded-only latency is survivorship-biased."""
    ss = load_session(report_dir)
    return ss["total_events_completed"] / ss["total_events"], ss["total_events_completed"], ss["total_events"]


def plot_cdf_allreqs(summaries, ceilings, key, label, unit, scale, out_path):
    """Completion-normalized CDF: y = fraction of ALL intended requests.
    Each strategy's curve rises only to its completion ceiling, then a dashed
    plateau marks the work it never finished. Removes survivorship bias."""
    pts = [(pk, fr) for pk, fr in PCTL if pk not in ("min", "max")]
    fig, ax = plt.subplots(figsize=(8.5, 5.5))
    for name, s in summaries.items():
        b = blk(s, key)
        ceil = ceilings[name][0]
        xs = [b[pk] * scale for pk, fr in pts if pk in b]
        ys = [fr * ceil for pk, fr in pts if pk in b]   # scale CDF to completion ceiling
        st = STYLES[name]
        ax.plot(xs, ys, color=st["color"], linestyle=st["linestyle"], marker=st["marker"],
                markersize=6, linewidth=1.8, alpha=0.9,
                label=f"{name}: {ceil*100:.0f}% completed")
        # plateau showing abandoned work
        ax.hlines(ceil, xs[-1], xs[-1] * 6, color=st["color"], linestyle=":", linewidth=1.2, alpha=0.6)
    ax.set_xscale("log")
    ax.set_xlabel(f"{label} ({unit}, log scale)", fontsize=11)
    ax.set_ylabel("Fraction of ALL intended requests", fontsize=11)
    ax.set_ylim(0, 1.02)
    ax.axhline(1.0, color="gray", linewidth=0.8, linestyle="--", alpha=0.5)
    ax.set_title(f"{label} CDF over ALL requests — {RUN}\n(curve caps at completion %; gap to 1.0 = abandoned work)", fontsize=11)
    ax.legend(fontsize=9, loc="lower right", title="ceiling = work finished")
    ax.grid(alpha=0.3, which="both")
    fig.tight_layout()
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"[latency_comparison] Wrote {out_path}")


def write_goodput_table(summaries, ceilings, out_path):
    lines = [f"Goodput & completion — {RUN} (the honest top-line)\n",
             f"{'Strategy':<14}{'events done':>13}{'/ intended':>11}{'completion':>12}{'goodput(req/s)':>16}"]
    lines.append("-" * len(lines[-1]))
    for name, s in summaries.items():
        ceil, done, tot = ceilings[name]
        bt = s.get("benchmark_time_seconds", 0) or 1
        gp = done / bt
        lines.append(f"{name:<14}{done:>13}{tot:>11}{ceil*100:>11.1f}%{gp:>16.2f}")
    text = "\n".join(lines)
    with open(out_path, "w") as f:
        f.write(text + "\n")
    print(f"[latency_comparison] Wrote {out_path}\n{text}\n")


def blk(summary, key):
    return summary["successes"]["latency"][key]


def plot_cdf(summaries, key, label, unit, scale, out_path):
    # Drop min (often 0 → breaks log) and max (single outlier dominates axis);
    # keep p1..p99.9 and use a log x-axis since latencies span orders of magnitude.
    pts = [(pk, fr) for pk, fr in PCTL if pk not in ("min", "max")]
    fig, ax = plt.subplots(figsize=(8, 5))
    for name, s in summaries.items():
        b = blk(s, key)
        xs = [b[pk] * scale for pk, fr in pts if pk in b]
        ys = [fr for pk, fr in pts if pk in b]
        ax.plot(xs, ys, color=STYLES[name]["color"], linestyle=STYLES[name]["linestyle"],
                marker=STYLES[name]["marker"], markersize=6, linewidth=1.8, alpha=0.85,
                label=f"{name} (n={s['successes']['count']})")
    ax.set_xscale("log")
    ax.set_xlabel(f"{label} ({unit}, log scale)", fontsize=11)
    ax.set_ylabel("CDF (fraction of succeeded requests)", fontsize=11)
    ax.set_ylim(0, 1.02)
    ax.set_title(f"{label} CDF by Strategy — {RUN} (succeeded only; p1–p99.9)", fontsize=12)
    ax.legend(fontsize=9, loc="upper left")
    ax.grid(alpha=0.3, which="both")
    fig.tight_layout()
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"[latency_comparison] Wrote {out_path}")


def plot_bars(summaries, out_path):
    names = list(summaries.keys())
    pctls = [("median", "p50"), ("p90", "p90"), ("p99", "p99")]
    fig, axes = plt.subplots(1, 3, figsize=(16, 5))
    for ax, (key, label, unit, scale) in zip(axes, METRICS):
        xs = range(len(pctls))
        w = 0.8 / len(names)
        for i, name in enumerate(names):
            b = blk(summaries[name], key)
            vals = [b[pk] * scale for pk, _ in pctls]
            ax.bar([x + i * w for x in xs], vals, w, label=name, color=STYLES[name]["color"])
        ax.set_xticks([x + w * (len(names) - 1) / 2 for x in xs])
        ax.set_xticklabels([lab for _, lab in pctls])
        ax.set_ylabel(f"{label} ({unit})", fontsize=11)
        ax.set_title(label, fontsize=12)
        ax.legend(fontsize=8)
        ax.grid(alpha=0.3, axis="y")
    fig.suptitle(f"Per-request latency by strategy — {RUN} (succeeded only)", fontsize=13)
    fig.tight_layout(rect=[0, 0, 1, 0.96])
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"[latency_comparison] Wrote {out_path}")


def write_table(summaries, out_path):
    lines = [f"Per-request latency comparison — {RUN} (SUCCEEDED requests only)",
             "WARNING: No-Fairness completes far fewer sessions; its latencies are survivorship-biased.\n"]
    for key, label, unit, scale in METRICS:
        lines.append(f"== {label} ({unit}) ==")
        hdr = f"{'Strategy':<14}{'succeeded':>11}{'mean':>10}{'p50':>10}{'p90':>10}{'p99':>10}"
        lines.append(hdr); lines.append("-" * len(hdr))
        for name, s in summaries.items():
            b = blk(s, key)
            lines.append(f"{name:<14}{s['successes']['count']:>11}"
                         f"{b['mean']*scale:>10.2f}{b['median']*scale:>10.2f}"
                         f"{b['p90']*scale:>10.2f}{b['p99']*scale:>10.2f}")
        lines.append("")
    text = "\n".join(lines)
    with open(out_path, "w") as f:
        f.write(text + "\n")
    print(f"[latency_comparison] Wrote {out_path}\n")
    print(text)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--las"); ap.add_argument("--rr")
    ap.add_argument("--no-fairness", dest="nf")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--label", default=None, help="run label for titles")
    a = ap.parse_args()
    global RUN
    RUN = a.label or os.path.basename(os.path.dirname(os.path.abspath(a.out))) or "run"
    dirs = {"LAS": a.las, "RR": a.rr, "No-Fairness": a.nf}
    dirs = {k: v for k, v in dirs.items() if v}
    os.makedirs(a.out, exist_ok=True)
    summaries = {k: load(v) for k, v in dirs.items()}
    ceilings = {k: completion_ceiling(v) for k, v in dirs.items()}
    write_table(summaries, os.path.join(a.out, "latency_table.txt"))
    write_goodput_table(summaries, ceilings, os.path.join(a.out, "goodput_completion.txt"))
    for key, label, unit, scale in METRICS:
        # survivorship-biased (succeeded only) — kept for reference
        plot_cdf(summaries, key, label, unit, scale,
                 os.path.join(a.out, f"{label.lower()}_cdf.png"))
        # honest: normalized over ALL intended requests
        plot_cdf_allreqs(summaries, ceilings, key, label, unit, scale,
                         os.path.join(a.out, f"{label.lower()}_cdf_allreqs.png"))
    plot_bars(summaries, os.path.join(a.out, "latency_bars.png"))
    print(f"\n[latency_comparison] Done — {a.out}")


if __name__ == "__main__":
    main()
