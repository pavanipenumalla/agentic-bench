#!/usr/bin/env python3
"""
Prometheus metrics analyzer.

Reads a JSONL file of scraped Prometheus metrics (from the endpoint picker)
and produces plots for every available metric:

  1. fairness_index.png       — Jain's fairness index over time
  2. ewma_wait_time.png       — Per-program EWMA wait time over time
  3. queue_score.png           — Per-program scheduling queue score over time
  4. queue_depth.png           — Per-program queue depth over time
  5. service_rate.png          — Per-program service rate (weighted tokens/sec) over time
  6. attained_service.png      — Per-program attained service (weighted tokens) over time
  7. requests.png              — Per-program cumulative requests over time
  8. dispatched.png            — Per-program cumulative dispatched requests over time
  9. pick_latency_cdf.png      — Pick() latency CDF from histogram buckets
 10. pick_latency_over_time.png — Average pick latency over time (from sum/count deltas)

Usage:
    python3 analyze_prometheus.py metrics.jsonl
    python3 analyze_prometheus.py metrics.jsonl -o /path/to/output/
"""

import argparse
import json
import os
from typing import Dict, List, Optional

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_jsonl(path: str) -> List[dict]:
    """Load records from a JSONL file, skipping malformed lines."""
    records = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    r = json.loads(line)
                    if "error" not in r:
                        records.append(r)
                except json.JSONDecodeError:
                    continue
    return records


def trim_after_stabilization(records: List[dict], margin_s: float = 20.0) -> List[dict]:
    """Trim records that occur long after all metrics have stabilized.

    Detects the last time the total cumulative request counter grew, then
    keeps only records up to ``margin_s`` seconds after that point.  If the
    tail is short (< 60 s of dead time), records are returned unchanged.
    """
    if not records:
        return records

    t0 = records[0]["ts"]
    prev_total = 0.0
    last_growth_ts = t0

    for r in records:
        total = 0.0
        for pdata in r.get("per_program", {}).values():
            req = pdata.get("requests")
            if req is not None:
                total += req
        if total > prev_total:
            last_growth_ts = r["ts"]
        prev_total = total

    cutoff = last_growth_ts + margin_s
    dead_time = records[-1]["ts"] - last_growth_ts
    if dead_time < 60:
        return records
    trimmed = [r for r in records if r["ts"] <= cutoff]
    print(f"[analyze_prometheus] Trimmed {len(records) - len(trimmed)} idle scrapes "
          f"({dead_time:.0f}s of dead time after metrics stabilized)")
    return trimmed


# ---------------------------------------------------------------------------
# Color mapping — group programs by total request count (quartile-based bins)
# ---------------------------------------------------------------------------

def compute_request_totals(records: List[dict]) -> Dict[str, float]:
    """Compute total requests per program from the final cumulative counter.

    Prometheus counters are cumulative, so the last observed value is the true
    total.  Using a delta (last - first) under-counts requests that occurred
    before the scraper's first observation of each program, and the size of
    that gap varies by strategy/timing, causing inconsistent groupings.
    """
    last_req: Dict[str, float] = {}
    for r in records:
        for pid, pdata in r.get("per_program", {}).items():
            cur = pdata.get("requests")
            if cur is None:
                continue
            last_req[pid] = cur
    return last_req


def _build_request_bins(totals: Dict[str, float]):
    """Create quartile-based bins from request totals.

    Returns (pid_to_group, group_to_color) where groups are human-readable
    labels like '<22 reqs', '22-116 reqs', etc.

    Bins are computed as:
        Q1 = 25th percentile, Q2 = 50th (median), Q3 = 75th percentile
        Bin 0: [min, Q1)    Bin 1: [Q1, Q2)    Bin 2: [Q2, Q3)    Bin 3: [Q3, max]
    """
    vals = sorted(totals.values())
    n = len(vals)
    if n == 0:
        return {}, {}

    q1 = vals[n // 4]
    q2 = vals[n // 2]
    q3 = vals[3 * n // 4]
    edges = [vals[0], q1, q2, q3, vals[-1]]

    # Deduplicate edges (if quartiles collapse).
    unique_edges = sorted(set(edges))
    if len(unique_edges) < 3:
        # Fallback: single bin for everything.
        label = f"{int(vals[0])}-{int(vals[-1])} reqs"
        pid_to_group = {pid: label for pid in totals}
        colors = plt.cm.tab10.colors
        group_to_color = {label: colors[0]}
        return pid_to_group, group_to_color

    # Build bin boundaries.
    bins = []
    for i in range(len(unique_edges) - 1):
        lo, hi = unique_edges[i], unique_edges[i + 1]
        if i == len(unique_edges) - 2:
            label = f"{int(lo)}-{int(hi)} reqs"
        else:
            label = f"{int(lo)}-{int(hi)} reqs"
        bins.append((lo, hi, label, i == len(unique_edges) - 2))

    colors = plt.cm.tab10.colors
    group_to_color = {}
    for i, (_, _, label, _) in enumerate(bins):
        group_to_color[label] = colors[i % len(colors)]

    pid_to_group = {}
    for pid, total in totals.items():
        for lo, hi, label, is_last in bins:
            if is_last:
                if total >= lo:
                    pid_to_group[pid] = label
                    break
            else:
                if lo <= total < hi:
                    pid_to_group[pid] = label
                    break
        else:
            # Fallback to last bin.
            pid_to_group[pid] = bins[-1][2]

    return pid_to_group, group_to_color


# ---------------------------------------------------------------------------
# Generic per-program time-series plotter
# ---------------------------------------------------------------------------

def _plot_per_program_metric(
    records: List[dict],
    metric_key: str,
    out_path: str,
    title: str,
    ylabel: str,
    pid_to_group: Dict[str, str],
    group_to_color: Dict[str, tuple],
    sample_n: Optional[int] = None,
):
    """Plot a single per-program metric over time.

    If sample_n is set, randomly pick that many programs to plot.
    """
    if not records:
        return

    t0 = records[0]["ts"]
    program_series: Dict[str, list] = {}
    for r in records:
        t = r["ts"] - t0
        for pid, pdata in r.get("per_program", {}).items():
            val = pdata.get(metric_key)
            if val is not None:
                program_series.setdefault(pid, []).append((t, val))

    if not program_series:
        print(f"[analyze_prometheus] No {metric_key} data found, skipping {os.path.basename(out_path)}")
        return

    all_pids = sorted(program_series.keys())
    sampled = False
    if sample_n and sample_n < len(all_pids):
        import random
        all_pids = sorted(random.sample(all_pids, sample_n))
        title += f" (sampled {sample_n} programs)"
        sampled = True

    fig, ax = plt.subplots(figsize=(14, 6))
    if sampled:
        colors = plt.cm.tab10.colors
        for i, pid in enumerate(all_pids):
            series = program_series[pid]
            xs, ys = zip(*series)
            ax.plot(xs, ys, color=colors[i % len(colors)], linewidth=1.4, label=f"Program {pid}")
    else:
        seen_groups: set = set()
        for pid in all_pids:
            series = program_series[pid]
            xs, ys = zip(*series)
            group = pid_to_group.get(pid, "unknown")
            color = group_to_color.get(group, (0.5, 0.5, 0.5))
            label = group if group not in seen_groups else "_nolegend_"
            seen_groups.add(group)
            ax.plot(xs, ys, color=color, linewidth=0.9, alpha=0.8, label=label)

    ax.set_xlabel("Time (s)")
    ax.set_ylabel(ylabel)
    ax.set_title(title, fontsize=11)
    ax.legend(fontsize=8, loc="upper left", bbox_to_anchor=(1.02, 1.0), ncol=1)
    ax.grid(alpha=0.3)
    fig.tight_layout(rect=[0, 0, 0.88, 1])
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"[analyze_prometheus] Wrote {out_path}")


# ---------------------------------------------------------------------------
# Plot: Fairness index over time
# ---------------------------------------------------------------------------

def plot_fairness_index(records: List[dict], out_path: str):
    if not records:
        return

    t0 = records[0]["ts"]
    pairs = [(r["ts"] - t0, r.get("fairness_index"))
             for r in records if r.get("fairness_index") is not None]
    if not pairs:
        print("[analyze_prometheus] No fairness index data, skipping")
        return

    xs, ys = zip(*pairs)

    fig, ax = plt.subplots(figsize=(12, 4))
    ax.plot(xs, ys, linewidth=1.5, color="steelblue")
    ax.set_xlabel("Time (s)")
    ax.set_ylabel("Jain's Fairness Index")
    ax.set_ylim(0, 1.05)
    ax.axhline(1.0, color="grey", linestyle="--", linewidth=0.8, alpha=0.6)
    ax.set_title("Jain's Fairness Index Over Time", fontsize=11)
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    print(f"[analyze_prometheus] Wrote {out_path}")


# ---------------------------------------------------------------------------
# Plot: Pick latency CDF (from last histogram snapshot)
# ---------------------------------------------------------------------------

def plot_pick_latency_cdf(records: List[dict], out_path: str):
    if not records:
        return

    # Use the last scrape's histogram (cumulative counters).
    last_hist = None
    for r in reversed(records):
        h = r.get("pick_latency")
        if h and h.get("buckets"):
            last_hist = h
            break
    if last_hist is None:
        print("[analyze_prometheus] No pick latency histogram, skipping CDF")
        return

    buckets = last_hist["buckets"]
    total = last_hist.get("count")
    if not total or total == 0:
        return

    bounds = sorted(
        ((float("inf") if k == "+Inf" else float(k), v) for k, v in buckets.items()),
        key=lambda x: x[0],
    )
    finite = [(le, count) for le, count in bounds if le != float("inf")]
    if not finite:
        return

    xs = [le for le, _ in finite]
    ys = [count / total for _, count in finite]

    fig, ax = plt.subplots(figsize=(10, 4))
    ax.plot(xs, ys, linewidth=1.5, marker="o", markersize=4, color="teal")
    ax.set_xlabel("Pick Latency (us)")
    ax.set_ylabel("CDF")
    ax.set_ylim(0, 1.05)
    ax.set_xscale("log")
    ax.set_title("Pick() Latency CDF", fontsize=11)
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    print(f"[analyze_prometheus] Wrote {out_path}")


# ---------------------------------------------------------------------------
# Plot: Program duration scatter (inferred from request counter changes)
# ---------------------------------------------------------------------------

def plot_program_duration(records: List[dict], out_path: str,
                          pid_to_group: Dict[str, str],
                          group_to_color: Dict[str, tuple],
                          req_totals: Optional[Dict[str, float]] = None):
    """Scatter plot: x = first-activity time, y = active duration per program.

    Activity is inferred by detecting when the cumulative `requests` counter
    increases between consecutive scrapes.  Dots are annotated with the
    program's total request count.
    """
    if not records:
        return

    t0 = records[0]["ts"]

    # For each program, track when requests counter increases.
    prev_requests: Dict[str, float] = {}
    first_activity: Dict[str, float] = {}
    last_activity: Dict[str, float] = {}

    for r in records:
        t = r["ts"]
        for pid, pdata in r.get("per_program", {}).items():
            cur = pdata.get("requests", 0)
            prev = prev_requests.get(pid)
            if prev is not None and cur > prev:
                if pid not in first_activity:
                    first_activity[pid] = t
                last_activity[pid] = t
            prev_requests[pid] = cur

    pids = sorted(set(first_activity.keys()) & set(last_activity.keys()))
    if not pids:
        print("[analyze_prometheus] No program activity detected, skipping program_duration.png")
        return

    fig, ax = plt.subplots(figsize=(12, 6))
    seen_groups: set = set()
    for pid in pids:
        x = first_activity[pid] - t0
        y = last_activity[pid] - first_activity[pid]
        group = pid_to_group.get(pid, "unknown")
        color = group_to_color.get(group, (0.5, 0.5, 0.5))
        label = group if group not in seen_groups else "_nolegend_"
        seen_groups.add(group)
        ax.scatter([x], [y], color=color, s=50, zorder=3, label=label)
        n_reqs = int(req_totals.get(pid, 0)) if req_totals else pid
        ax.annotate(str(n_reqs), (x, y), textcoords="offset points",
                    xytext=(5, 5), fontsize=7, alpha=0.8)

    ax.set_xlabel("Program Start Time (s)")
    ax.set_ylabel("Active Duration (s)")
    ax.set_title("Program Duration vs Start Time (inferred from request counters)", fontsize=11)
    ax.legend(fontsize=8, loc="upper left", bbox_to_anchor=(1.02, 1.0), ncol=1)
    ax.grid(alpha=0.3)
    fig.tight_layout(rect=[0, 0, 0.88, 1])
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"[analyze_prometheus] Wrote {out_path}")


# ---------------------------------------------------------------------------
# Plot: Per-program request completion CDF over time
# ---------------------------------------------------------------------------

def plot_request_completion_cdf(records: List[dict], out_path: str,
                                pid_to_group: Dict[str, str],
                                group_to_color: Dict[str, tuple]):
    """CDF plot: each line is a program, x = time (s), y = fraction of that
    program's total requests completed by that time.

    Colored by request-count bins.
    """
    if not records:
        return

    t0 = records[0]["ts"]

    # Build per-program time series of cumulative requests.
    program_series: Dict[str, List[tuple]] = {}
    for r in records:
        t = r["ts"] - t0
        for pid, pdata in r.get("per_program", {}).items():
            cur = pdata.get("requests")
            if cur is not None:
                program_series.setdefault(pid, []).append((t, cur))

    if not program_series:
        print("[analyze_prometheus] No request data for completion CDF, skipping")
        return

    # Normalize each program's series to 0..1 (fraction of its final total).
    all_pids = sorted(program_series.keys())

    fig, ax = plt.subplots(figsize=(12, 5))
    seen_groups: set = set()
    for pid in all_pids:
        series = program_series[pid]
        xs = [t for t, _ in series]
        raw = [v for _, v in series]
        final = raw[-1]
        first = raw[0]
        if final <= first:
            continue  # no new requests during observation
        ys = [(v - first) / (final - first) for v in raw]
        group = pid_to_group.get(pid, "unknown")
        color = group_to_color.get(group, (0.5, 0.5, 0.5))
        label = group if group not in seen_groups else "_nolegend_"
        seen_groups.add(group)
        ax.plot(xs, ys, color=color, linewidth=1.2, label=label)

    ax.set_xlabel("Time (s)")
    ax.set_ylabel("CDF")
    ax.set_ylim(0, 1.05)
    ax.set_title("Per-Program Request Completion CDF Over Time", fontsize=11)
    ax.legend(fontsize=8, loc="upper left", bbox_to_anchor=(1.02, 1.0), ncol=1)
    ax.grid(alpha=0.3)
    fig.tight_layout(rect=[0, 0, 0.88, 1])
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"[analyze_prometheus] Wrote {out_path}")


# ---------------------------------------------------------------------------
# Plot: Average pick latency over time (delta sum / delta count)
# ---------------------------------------------------------------------------

def plot_pick_latency_over_time(records: List[dict], out_path: str):
    if not records:
        return

    t0 = records[0]["ts"]
    xs, ys = [], []
    prev_sum, prev_count = None, None

    for r in records:
        h = r.get("pick_latency")
        if not h:
            continue
        cur_sum = h.get("sum")
        cur_count = h.get("count")
        if cur_sum is None or cur_count is None:
            continue
        if prev_sum is not None and prev_count is not None:
            d_count = cur_count - prev_count
            d_sum = cur_sum - prev_sum
            if d_count > 0:
                xs.append(r["ts"] - t0)
                ys.append(d_sum / d_count)  # average latency in us for this interval
        prev_sum, prev_count = cur_sum, cur_count

    if not xs:
        print("[analyze_prometheus] No pick latency time-series data, skipping")
        return

    fig, ax = plt.subplots(figsize=(12, 4))
    ax.plot(xs, ys, linewidth=1.2, color="darkorange")
    ax.set_xlabel("Time (s)")
    ax.set_ylabel("Avg Pick Latency (us)")
    ax.set_title("Average Pick() Latency Over Time", fontsize=11)
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    print(f"[analyze_prometheus] Wrote {out_path}")


# ---------------------------------------------------------------------------
# Score computation
# ---------------------------------------------------------------------------

def compute_scores(records: List[dict]) -> Optional[dict]:
    """Compute summary scores from Prometheus metrics.

    Returns a dict with:
      - avg_per_request_duration: mean(duration_i / requests_i) across programs
      - sum_completion_time: sum(duration_i) across programs
      - per_program: list of {program_id, requests, duration, per_request_duration}
    """
    if not records:
        return None

    t0 = records[0]["ts"]

    # Compute request deltas per program.
    first_req: Dict[str, float] = {}
    last_req: Dict[str, float] = {}
    for r in records:
        for pid, pdata in r.get("per_program", {}).items():
            cur = pdata.get("requests")
            if cur is None:
                continue
            if pid not in first_req:
                first_req[pid] = cur
            last_req[pid] = cur

    # Compute active durations per program.
    prev_requests: Dict[str, float] = {}
    first_activity: Dict[str, float] = {}
    last_activity: Dict[str, float] = {}
    for r in records:
        t = r["ts"]
        for pid, pdata in r.get("per_program", {}).items():
            cur = pdata.get("requests", 0)
            prev = prev_requests.get(pid)
            if prev is not None and cur > prev:
                if pid not in first_activity:
                    first_activity[pid] = t
                last_activity[pid] = t
            prev_requests[pid] = cur

    # Build per-program scores.
    per_program = []
    for pid in sorted(first_req.keys()):
        reqs = last_req.get(pid, 0) - first_req.get(pid, 0)
        if reqs <= 0 or pid not in first_activity or pid not in last_activity:
            continue
        duration = last_activity[pid] - first_activity[pid]
        per_program.append({
            "program_id": pid,
            "requests": reqs,
            "duration": round(duration, 2),
            "per_request_duration": round(duration / reqs, 4),
        })

    if not per_program:
        return None

    avg_per_request_duration = sum(p["per_request_duration"] for p in per_program) / len(per_program)
    sum_completion_time = sum(p["duration"] for p in per_program)

    return {
        "avg_per_request_duration": round(avg_per_request_duration, 4),
        "sum_completion_time": round(sum_completion_time, 2),
        "num_programs": len(per_program),
        "per_program": per_program,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Plot Prometheus metrics from a JSONL file")
    parser.add_argument("jsonl_file", help="Path to the metrics JSONL file")
    parser.add_argument("-o", "--output-dir", default=None,
                        help="Output directory for plots (default: same dir as input file)")
    args = parser.parse_args()

    raw_records = load_jsonl(args.jsonl_file)
    if not raw_records:
        print(f"[analyze_prometheus] No valid records in {args.jsonl_file}")
        return

    print(f"[analyze_prometheus] Loaded {len(raw_records)} records from {args.jsonl_file}")
    records = trim_after_stabilization(raw_records)

    out_dir = args.output_dir or os.path.dirname(os.path.abspath(args.jsonl_file))
    plots_dir = os.path.join(out_dir, "prometheus_plots")
    os.makedirs(plots_dir, exist_ok=True)

    # Compute request-count-based color bins (use untrimmed for true totals).
    req_totals = compute_request_totals(raw_records)
    pid_to_group, group_to_color = _build_request_bins(req_totals)

    # Print the bin ranges for transparency.
    for group in sorted(group_to_color.keys()):
        count = sum(1 for g in pid_to_group.values() if g == group)
        print(f"[analyze_prometheus]   {group}: {count} programs")

    # 1. Fairness index
    plot_fairness_index(records, os.path.join(plots_dir, "fairness_index.png"))

    # 2a. EWMA wait time — all programs
    _plot_per_program_metric(
        records, "ewma_wait_ms",
        os.path.join(plots_dir, "ewma_wait_time.png"),
        "Per-Program EWMA Wait Time Over Time",
        "EWMA Wait Time (ms)",
        pid_to_group, group_to_color,
    )

    # 2b. EWMA wait time — sampled subset for readability
    _plot_per_program_metric(
        records, "ewma_wait_ms",
        os.path.join(plots_dir, "ewma_wait_time_sampled.png"),
        "Per-Program EWMA Wait Time Over Time",
        "EWMA Wait Time (ms)",
        pid_to_group, group_to_color,
        sample_n=4,
    )

    # 3. Queue score
    _plot_per_program_metric(
        records, "queue_score",
        os.path.join(plots_dir, "queue_score.png"),
        "Per-Program Queue Score Over Time",
        "Queue Score",
        pid_to_group, group_to_color,
    )

    # 4. Queue depth
    _plot_per_program_metric(
        records, "queue_size",
        os.path.join(plots_dir, "queue_depth.png"),
        "Per-Program Queue Depth Over Time",
        "Queue Depth",
        pid_to_group, group_to_color,
    )

    # 5. Service rate
    _plot_per_program_metric(
        records, "service_rate_tps",
        os.path.join(plots_dir, "service_rate.png"),
        "Per-Program Service Rate Over Time",
        "Service Rate (weighted tokens/sec)",
        pid_to_group, group_to_color,
    )

    # 6. Attained service
    _plot_per_program_metric(
        records, "attained_service",
        os.path.join(plots_dir, "attained_service.png"),
        "Per-Program Attained Service Over Time",
        "Attained Service (weighted tokens)",
        pid_to_group, group_to_color,
    )

    # 7. Cumulative requests
    _plot_per_program_metric(
        records, "requests",
        os.path.join(plots_dir, "requests.png"),
        "Per-Program Cumulative Requests Over Time",
        "Requests",
        pid_to_group, group_to_color,
    )

    # 8. Cumulative dispatched
    _plot_per_program_metric(
        records, "dispatched",
        os.path.join(plots_dir, "dispatched.png"),
        "Per-Program Cumulative Dispatched Over Time",
        "Dispatched",
        pid_to_group, group_to_color,
    )

    # 9. Pick latency CDF
    plot_pick_latency_cdf(records, os.path.join(plots_dir, "pick_latency_cdf.png"))

    # 10. Pick latency over time
    plot_pick_latency_over_time(records, os.path.join(plots_dir, "pick_latency_over_time.png"))

    # 11. Program duration scatter
    plot_program_duration(records, os.path.join(plots_dir, "program_duration.png"),
                          pid_to_group, group_to_color, req_totals=req_totals)

    # 12. Per-program request completion CDF
    plot_request_completion_cdf(records, os.path.join(plots_dir, "request_completion_cdf.png"),
                                pid_to_group, group_to_color)

    print(f"[analyze_prometheus] All plots written to {plots_dir}/")

    # Compute and export scores (use untrimmed records for accurate totals).
    scores = compute_scores(raw_records)
    if scores:
        scores_path = os.path.join(plots_dir, "scores.json")
        with open(scores_path, "w") as f:
            json.dump(scores, f, indent=2)
        print(f"[analyze_prometheus] Scores: avg_per_request_duration={scores['avg_per_request_duration']:.4f}s  "
              f"sum_completion_time={scores['sum_completion_time']:.2f}s")
        print(f"[analyze_prometheus] Wrote {scores_path}")


if __name__ == "__main__":
    main()
