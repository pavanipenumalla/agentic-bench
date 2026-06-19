#!/usr/bin/env python3
"""
Prometheus metrics scraper for test/load-test.

Polls the EPP metrics endpoint every second during a phase run and writes
timestamped JSONL records for later analysis.

Per-program metrics (try BOTH subsystem prefixes for forward/back compat
across the v6 → v7 PR-707-split metric rename):
  Old prefix (v6 builds):  program_aware_*
  New prefix (v7 builds):  llm_d_router_epp_program_aware_*

Per-program signals (any subset present is captured):
  jains_fairness_index                  scalar
  avg_wait_time_milliseconds            per program_id  (back-compat:
                                          ewma_wait_time_milliseconds)
  attained_service_tokens               per program_id  (LAS only)
  deficit_tokens                        per program_id  (DRR only, not on
                                          program-aware-las branch)
  service_rate_tokens_per_second        per program_id  (program-aware-plugin
                                          only; dropped in PR 707 split LAS)
  queue_score                           per program_id  (also dropped in PR 707)
  requests_total                        per program_id  (dropped in PR 707)
  dispatched_total                      per program_id  (dropped in PR 707)
  input_tokens_total / output_tokens_total  per program_id  (dropped in PR 707)
  throughput_tokens_per_second          per program_id  (dropped in PR 707)
  pick_latency_microseconds             histogram (dropped in PR 707)

Per-flow framework metrics (always present when flow-control is enabled).
Try BOTH prefixes:
  Old prefix (v6 / earlier): inference_extension_flow_control_*
  New prefix (v7 era):       llm_d_router_epp_flow_control_*

Per-flow signals:
  queue_size                            gauge per fairness_id (also priority,
                                          inference_pool, model_name,
                                          target_model_name)
  queue_bytes                           gauge per fairness_id (same labels)
  request_queue_duration_seconds        histogram per (fairness_id, outcome)
  request_enqueue_duration_seconds      histogram per (fairness_id, outcome)

Cluster-level framework metrics (no fairness_id label):
  dispatch_cycle_duration_seconds       histogram (no labels)
  pool_saturation                       gauge per inference_pool

Usage:
    python3 scrape_metrics.py \
        --url http://localhost:9090/metrics \
        --duration 150 \
        --output results/simple-ab/program-aware/metrics.jsonl

The --subsystem flag is retained for back-compat with run.sh callers but
ignored — both v6 and v7 prefixes are queried unconditionally.
"""

import argparse
import json
import re
import time
import urllib.request
from typing import Dict, Optional


# Subsystem prefixes for program-aware plugin metrics. Older v6 builds
# emitted under "program_aware_*"; PR-707-split (v7+) renamed to
# "llm_d_router_epp_program_aware_*". We always try both — whichever the
# live EPP exposes wins (the other returns empty).
PROGRAM_AWARE_PREFIXES = ("llm_d_router_epp_program_aware", "program_aware")

# Subsystem prefixes for framework flow-control metrics.
FLOW_CONTROL_PREFIXES = ("llm_d_router_epp_flow_control", "inference_extension_flow_control")


# ---------------------------------------------------------------------------
# Prometheus text format parser
# ---------------------------------------------------------------------------

def parse_prometheus(text: str) -> Dict[str, float]:
    """Parse Prometheus text exposition into a flat {metric_line: value} dict."""
    result = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.rsplit(" ", 1)
        if len(parts) == 2:
            try:
                result[parts[0]] = float(parts[1])
            except ValueError:
                continue
    return result


def extract_scalar(metrics: Dict[str, float], name: str) -> Optional[float]:
    """Return a scalar metric value (no labels)."""
    return metrics.get(name)


def extract_by_label(metrics: Dict[str, float], metric_name: str, label: str) -> Dict[str, float]:
    """Return {label_value: metric_value} for all series matching metric_name{label=...}.

    If a series has multiple labels, the first matching label value wins;
    later labels overwrite (callers using multi-label metrics should call
    extract_full_series instead).
    """
    result = {}
    prefix = metric_name + "{"
    pattern = re.compile(rf'{label}="([^"]+)"')
    for key, val in metrics.items():
        if key.startswith(prefix):
            m = pattern.search(key)
            if m:
                result[m.group(1)] = val
    return result


def extract_full_series(metrics: Dict[str, float], metric_name: str) -> list:
    """Return [{labels_dict, value}] for every series of metric_name."""
    out = []
    prefix = metric_name + "{"
    label_re = re.compile(r'(\w+)="([^"]*)"')
    for key, val in metrics.items():
        if not key.startswith(prefix):
            continue
        labels_part = key[len(metric_name) + 1 : key.rindex("}")]
        labels = dict(label_re.findall(labels_part))
        out.append({"labels": labels, "value": val})
    return out


def extract_histogram(metrics: Dict[str, float], name: str) -> Optional[dict]:
    """Extract histogram buckets, sum, and count for a label-less metric.

    Returns {buckets: {le: count}, sum, count} or None if the metric is
    absent. For a histogram with labels, use extract_histogram_by_label.
    """
    bucket_prefix = name + '_bucket{le="'
    buckets = {}
    for key, val in metrics.items():
        if key.startswith(bucket_prefix):
            le = key[len(bucket_prefix):key.index('"}')]
            buckets[le] = val
    total = metrics.get(name + "_count")
    hsum = metrics.get(name + "_sum")
    if not buckets and total is None and hsum is None:
        return None
    return {"buckets": buckets, "sum": hsum, "count": total}


def extract_histogram_aggregate(metrics: Dict[str, float], name: str) -> Optional[dict]:
    """Aggregate a labeled histogram across all label combinations.

    For metrics like flow_control_request_queue_duration_seconds that have
    fairness_id / outcome labels, returns one rolled-up bucket map and total
    sum / count across all label values seen. Per-label-combo breakdown is
    skipped to keep JSONL line size bounded; the aggregate is enough for
    distribution-shape analysis at the cluster level.
    """
    bucket_re = re.compile(rf'^{re.escape(name)}_bucket\{{(.*)\}}$')
    sum_re = re.compile(rf'^{re.escape(name)}_sum\{{(.*)\}}$')
    count_re = re.compile(rf'^{re.escape(name)}_count\{{(.*)\}}$')
    le_re = re.compile(r'le="([^"]*)"')

    buckets: Dict[str, float] = {}
    total_sum = 0.0
    total_count = 0.0
    saw = False

    for key, val in metrics.items():
        m = bucket_re.match(key)
        if m:
            saw = True
            le_match = le_re.search(m.group(1))
            if le_match:
                le = le_match.group(1)
                buckets[le] = buckets.get(le, 0.0) + val
            continue
        if sum_re.match(key):
            saw = True
            total_sum += val
            continue
        if count_re.match(key):
            saw = True
            total_count += val

    if not saw:
        return None
    return {"buckets": buckets, "sum": total_sum, "count": total_count}


# ---------------------------------------------------------------------------
# Per-prefix collectors
# ---------------------------------------------------------------------------

def _try_prefixes(metrics: Dict[str, float], prefixes, suffix: str, kind: str):
    """Walk prefixes in order; return whichever yields a non-empty result."""
    for p in prefixes:
        full = f"{p}_{suffix}"
        if kind == "scalar":
            v = extract_scalar(metrics, full)
            if v is not None:
                return v
        elif kind == "by_label_program":
            v = extract_by_label(metrics, full, "program_id")
            if v:
                return v
        elif kind == "by_label_fairness":
            v = extract_by_label(metrics, full, "fairness_id")
            if v:
                return v
        elif kind == "by_label_pool":
            v = extract_by_label(metrics, full, "inference_pool")
            if v:
                return v
        elif kind == "histogram":
            v = extract_histogram(metrics, full)
            if v is not None:
                return v
        elif kind == "histogram_aggregate":
            v = extract_histogram_aggregate(metrics, full)
            if v is not None:
                return v
        elif kind == "full_series":
            v = extract_full_series(metrics, full)
            if v:
                return v
    return None if kind in ("scalar", "histogram", "histogram_aggregate") else {}


def collect_program_aware(metrics: Dict[str, float]) -> dict:
    """Build the program-aware plugin metric block.

    Tries both subsystem prefixes per metric so v6 and v7 EPP builds are
    both supported with the same scraper.
    """
    fairness_index = _try_prefixes(metrics, PROGRAM_AWARE_PREFIXES, "jains_fairness_index", "scalar")

    avg_wait = _try_prefixes(metrics, PROGRAM_AWARE_PREFIXES, "avg_wait_time_milliseconds", "by_label_program") or {}
    if not avg_wait:
        # Legacy alias before the avg/ewma rename in mid-v6 era.
        avg_wait = _try_prefixes(metrics, PROGRAM_AWARE_PREFIXES, "ewma_wait_time_milliseconds", "by_label_program") or {}

    attained_svc = _try_prefixes(metrics, PROGRAM_AWARE_PREFIXES, "attained_service_tokens", "by_label_program") or {}
    deficit = _try_prefixes(metrics, PROGRAM_AWARE_PREFIXES, "deficit_tokens", "by_label_program") or {}
    queue_score = _try_prefixes(metrics, PROGRAM_AWARE_PREFIXES, "queue_score", "by_label_program") or {}
    service_rate = _try_prefixes(metrics, PROGRAM_AWARE_PREFIXES, "service_rate_tokens_per_second", "by_label_program") or {}
    throughput = _try_prefixes(metrics, PROGRAM_AWARE_PREFIXES, "throughput_tokens_per_second", "by_label_program") or {}
    requests = _try_prefixes(metrics, PROGRAM_AWARE_PREFIXES, "requests_total", "by_label_program") or {}
    dispatched = _try_prefixes(metrics, PROGRAM_AWARE_PREFIXES, "dispatched_total", "by_label_program") or {}
    input_tokens = _try_prefixes(metrics, PROGRAM_AWARE_PREFIXES, "input_tokens_total", "by_label_program") or {}
    output_tokens = _try_prefixes(metrics, PROGRAM_AWARE_PREFIXES, "output_tokens_total", "by_label_program") or {}
    pick_latency = _try_prefixes(metrics, PROGRAM_AWARE_PREFIXES, "pick_latency_microseconds", "histogram")

    all_ids = (set(avg_wait) | set(attained_svc) | set(deficit) | set(queue_score)
               | set(service_rate) | set(throughput) | set(requests) | set(dispatched)
               | set(input_tokens) | set(output_tokens))

    per_program = {
        pid: {
            "avg_wait_ms":       avg_wait.get(pid),
            "ewma_wait_ms":      avg_wait.get(pid),  # back-compat alias
            "attained_service":  attained_svc.get(pid),
            "deficit_tokens":    deficit.get(pid),
            "queue_score":       queue_score.get(pid),
            "service_rate_tps":  service_rate.get(pid),
            "throughput_tps":    throughput.get(pid),
            "requests":          requests.get(pid),
            "dispatched":        dispatched.get(pid),
            "input_tokens":      input_tokens.get(pid),
            "output_tokens":     output_tokens.get(pid),
        }
        for pid in sorted(all_ids)
    }

    return {
        "fairness_index": fairness_index,
        "pick_latency":   pick_latency,
        "per_program":    per_program,
    }


def collect_flow_control(metrics: Dict[str, float]) -> dict:
    """Build the framework flow-control metric block.

    Per-flow keys are the fairness_id label value. Cluster-level signals
    (dispatch_cycle, pool_saturation) live alongside the per_flow map.
    """
    queue_size = _try_prefixes(metrics, FLOW_CONTROL_PREFIXES, "queue_size", "by_label_fairness") or {}
    queue_bytes = _try_prefixes(metrics, FLOW_CONTROL_PREFIXES, "queue_bytes", "by_label_fairness") or {}

    pool_saturation = _try_prefixes(metrics, FLOW_CONTROL_PREFIXES, "pool_saturation", "by_label_pool") or {}

    dispatch_cycle = _try_prefixes(metrics, FLOW_CONTROL_PREFIXES, "dispatch_cycle_duration_seconds", "histogram_aggregate")
    request_queue = _try_prefixes(metrics, FLOW_CONTROL_PREFIXES, "request_queue_duration_seconds", "histogram_aggregate")
    request_enqueue = _try_prefixes(metrics, FLOW_CONTROL_PREFIXES, "request_enqueue_duration_seconds", "histogram_aggregate")

    all_fids = set(queue_size) | set(queue_bytes)
    per_flow = {
        fid: {
            "queue_size":  queue_size.get(fid),
            "queue_bytes": queue_bytes.get(fid),
        }
        for fid in sorted(all_fids)
    }

    return {
        "per_flow":                 per_flow,
        "pool_saturation":          pool_saturation,
        "dispatch_cycle_seconds":   dispatch_cycle,
        "request_queue_seconds":    request_queue,
        "request_enqueue_seconds":  request_enqueue,
    }


# ---------------------------------------------------------------------------
# Single scrape
# ---------------------------------------------------------------------------

def scrape_once(url: str) -> dict:
    ts = time.time()
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            text = resp.read().decode("utf-8")
    except Exception as e:
        return {"ts": ts, "error": str(e)}

    metrics = parse_prometheus(text)

    pa = collect_program_aware(metrics)
    fc = collect_flow_control(metrics)

    return {
        "ts":             ts,
        "fairness_index": pa["fairness_index"],
        "pick_latency":   pa["pick_latency"],
        "per_program":    pa["per_program"],
        "flow_control":   fc,
    }


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Prometheus metrics scraper for test/load-test")
    parser.add_argument("--url",       required=True,                    help="Metrics endpoint URL")
    parser.add_argument("--subsystem", default="program_aware",          help="(legacy, ignored: both v6 and v7 prefixes are queried)")
    parser.add_argument("--duration",  type=int, required=True,          help="How long to scrape (seconds)")
    parser.add_argument("--output",    required=True,                    help="Output JSONL file path")
    parser.add_argument("--interval",  type=float, default=0.5,          help="Scrape interval (seconds)")
    args = parser.parse_args()

    print(f"[scraper] url={args.url}  duration={args.duration}s  output={args.output}  (subsystem flag ignored)")

    t0       = time.time()
    end_time = t0 + args.duration
    count    = 0
    next_t   = t0

    with open(args.output, "w") as f:
        while time.time() < end_time:
            now = time.time()
            if now >= next_t:
                record = scrape_once(args.url)
                f.write(json.dumps(record) + "\n")
                f.flush()
                count += 1
                next_t += args.interval

                # Print summary every 10 scrapes.
                if count % 10 == 0:
                    elapsed = now - t0
                    fi = record.get("fairness_index")
                    fi_str = f"{fi:.4f}" if fi is not None else "N/A"
                    pp = record.get("per_program", {}) or {}
                    fc_pf = (record.get("flow_control") or {}).get("per_flow", {}) or {}
                    print(f"[T+{elapsed:5.0f}s] fairness_index={fi_str}  programs={len(pp)}  flows={len(fc_pf)}")

            sleep_for = next_t - time.time()
            if sleep_for > 0:
                time.sleep(min(sleep_for, 0.1))

    print(f"[scraper] Done. {count} samples written to {args.output}")


if __name__ == "__main__":
    main()