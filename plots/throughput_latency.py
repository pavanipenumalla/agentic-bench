#!/usr/bin/env python3
"""
prism-style operating-point plot: output tokens/sec (Y) vs latency (X), one
point per strategy, for each latency metric (TTFT, ITL, E2E). Marker = p50,
whisker = p50->p99. Each point is annotated with SESSION COMPLETION %, so
throughput/latency is always read alongside how much work actually finished.

NOTE: these runs are a single load point (600 concurrent sessions), so this is
one operating point per strategy, not a swept curve. Y uses each run's own
benchmark_time; No-Fairness's run ended earlier (it cancelled sessions), which
inflates its rate — the completion % annotation makes that visible.

Usage:
  python3 throughput_latency.py --las <d> --rr <d> --no-fairness <d> -o <out>
"""
import argparse, json, os
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

STYLES = {
    "LAS":         {"color": plt.cm.Set2.colors[1], "marker": "s"},
    "RR":          {"color": plt.cm.Set2.colors[2], "marker": "D"},
    "No-Fairness": {"color": plt.cm.Set2.colors[3], "marker": "X"},
}
RUN = "run"
METRICS = [("time_to_first_token", "TTFT", "s", 1.0),
           ("inter_token_latency", "ITL", "ms", 1000.0),
           ("request_latency", "E2E", "s", 1.0)]


def stats(report_dir):
    lc = json.load(open(os.path.join(report_dir, "summary_lifecycle_metrics.json")))
    ss = json.load(open(os.path.join(report_dir, "summary_session_lifecycle_metrics.json")))
    psl = json.load(open(os.path.join(report_dir, "per_session_lifecycle_metrics.json")))
    bt = lc["benchmark_time_seconds"]
    out_tok = sum(s.get("total_output_tokens", 0) for s in psl)
    good_tok = sum(s.get("total_output_tokens", 0) for s in psl if s.get("success", True))
    return {
        "lat": lc["successes"]["latency"],
        "tok_s": out_tok / bt,
        "goodput_tok_s": good_tok / bt,
        "sess_pct": 100.0 * ss["num_sessions_succeeded"] / ss["num_sessions"],
        "evt_pct": 100.0 * ss["total_events_completed"] / ss["total_events"],
        "bt": bt,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--las"); ap.add_argument("--rr"); ap.add_argument("--no-fairness", dest="nf")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--label", default=None)
    ap.add_argument("--y", choices=["tok_s", "goodput_tok_s"], default="tok_s",
                    help="tok_s = raw output tok/s; goodput_tok_s = completed-session tok/s")
    a = ap.parse_args()
    global RUN
    RUN = a.label or os.path.basename(os.path.dirname(os.path.abspath(a.out))) or "run"
    dirs = {k: v for k, v in {"LAS": a.las, "RR": a.rr, "No-Fairness": a.nf}.items() if v}
    os.makedirs(a.out, exist_ok=True)
    S = {k: stats(v) for k, v in dirs.items()}

    ylabel = "Output tokens/sec" if a.y == "tok_s" else "Goodput tokens/sec (completed sessions)"
    fig, axes = plt.subplots(1, len(METRICS), figsize=(6 * len(METRICS), 5.5))
    for ax, (key, label, unit, scale) in zip(axes, METRICS):
        for name, s in S.items():
            b = s["lat"][key]
            x50, x99 = b["median"] * scale, b["p99"] * scale
            y = s[a.y]
            st = STYLES[name]
            ax.errorbar([x50], [y], xerr=[[max(0, x50 - 0)], [max(0, x99 - x50)]],
                        fmt=st["marker"], color=st["color"], markersize=13, capsize=5,
                        elinewidth=1.5, alpha=0.9, label=name)
            ax.annotate(f"{name}\n{s['sess_pct']:.0f}% sessions done",
                        (x50, y), textcoords="offset points", xytext=(8, 8), fontsize=8.5)
        ax.set_xscale("log")
        ax.set_xlabel(f"{label} ({unit}, log) — marker=p50, whisker→p99", fontsize=10)
        ax.set_ylabel(ylabel, fontsize=10)
        ax.set_title(f"{ylabel.split('(')[0].strip()} vs {label}", fontsize=11)
        ax.grid(alpha=0.3, which="both")
        ax.legend(fontsize=8, loc="lower left")
    fig.suptitle(f"Throughput vs latency by strategy — {RUN}  (up-and-left better; "
                 "read with completion %)", fontsize=12)
    fig.tight_layout(rect=[0, 0, 1, 0.95])
    out = os.path.join(a.out, f"throughput_vs_latency_{a.y}.png")
    fig.savefig(out, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"[throughput_latency] Wrote {out}")
    # summary line
    for name, s in S.items():
        print(f"  {name:<12} tok/s={s['tok_s']:.0f} goodput={s['goodput_tok_s']:.0f} "
              f"sessions={s['sess_pct']:.0f}% events={s['evt_pct']:.0f}% bt={s['bt']:.0f}s")


if __name__ == "__main__":
    main()
