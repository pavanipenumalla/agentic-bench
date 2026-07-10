#!/usr/bin/env python3
"""
Prometheus metrics scraper for test/load-test.

Polls the EPP metrics endpoint every second during a phase run and writes
timestamped JSONL records for later analysis.

Per-program metrics (try BOTH subsystem prefixes for forward/back compat
across the v6 → v7 PR-707-split metric rename):
  Old prefix (v6 builds):  program_aware_*
  New prefix (v7 builds):  llm_d_router_epp_program_aware_*

Per-program signals captured:
  jains_fairness_index                  scalar
  avg_wait_time_milliseconds            per program_id  (back-compat:
                                          ewma_wait_time_milliseconds)
  attained_service_tokens               per program_id  (LAS only)
  deficit_tokens                        per program_id  (DRR only)

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

Cache metrics (all histograms under the llm_d_epp subsystem):
  prefix_indexer_hit_ratio              histogram per plugin_name (prefix
                                          length matched / total, router-side)
  prefix_indexer_hit_bytes              histogram per plugin_name (matched
                                          prefix length in bytes)
  request_cached_tokens                 histogram per fairness_id (prompt
                                          tokens served from cache, as reported
                                          by the model server)
  Raw sum/count are stored per scrape so the visualizer can render a
  cumulative mean or a step delta without re-scraping.

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
PROGRAM_AWARE_PREFIXES = ("llm_d_epp_program_aware", "llm_d_router_epp_program_aware", "program_aware")

# Subsystem prefixes for framework flow-control metrics.
FLOW_CONTROL_PREFIXES = ("llm_d_epp_flow_control", "llm_d_router_epp_flow_control", "inference_extension_flow_control")


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


def sum_count_by_label(metrics: Dict[str, float], metric_name: str, label: str) -> dict:
    """Return {label_value: {"sum", "count"}} for a labeled histogram.

    Series sharing a label value are summed, so a metric with extra labels
    beyond `label` (e.g. plugin_type alongside plugin_name) rolls up per
    label_value. Storing raw sum/count lets downstream code derive either a
    cumulative mean or a per-window delta.
    """
    result: Dict[str, dict] = {}
    lbl_re = re.compile(rf'{label}="([^"]+)"')
    sum_re = re.compile(rf'^{re.escape(metric_name)}_sum\{{')
    count_re = re.compile(rf'^{re.escape(metric_name)}_count\{{')
    for key, val in metrics.items():
        m = lbl_re.search(key)
        if not m:
            continue
        lv = m.group(1)
        if sum_re.match(key):
            result.setdefault(lv, {})["sum"] = result.get(lv, {}).get("sum", 0.0) + val
        elif count_re.match(key):
            result.setdefault(lv, {})["count"] = result.get(lv, {}).get("count", 0.0) + val
    return result


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

    all_ids = set(avg_wait) | set(attained_svc) | set(deficit)

    per_program = {
        pid: {
            "avg_wait_ms":           avg_wait.get(pid),
            "attained_service_tokens": attained_svc.get(pid),
            "deficit_tokens":        deficit.get(pid),
        }
        for pid in sorted(all_ids)
    }

    return {
        "fairness_index": fairness_index,
        "per_program":    per_program,
    }


def collect_endpoint_pool(metrics: Dict[str, float]) -> dict:
    """Build the inference-pool / endpoint metric block.

    Values are means across all ready endpoints in the pool, computed by the
    EPP datalayer logger from per-endpoint scraped metrics. The pool name is
    carried in the 'name' label.
    """
    PREFIX = "llm_d_epp"
    kv_cache    = extract_by_label(metrics, f"{PREFIX}_average_kv_cache_utilization", "name")
    queue_size  = extract_by_label(metrics, f"{PREFIX}_average_queue_size",            "name")
    running_req = extract_by_label(metrics, f"{PREFIX}_average_running_requests",      "name")
    ready_ep    = extract_by_label(metrics, f"{PREFIX}_ready_endpoints",               "name")

    all_pools = set(kv_cache) | set(queue_size) | set(running_req) | set(ready_ep)
    per_pool = {
        pool: {
            "avg_kv_cache_utilization": kv_cache.get(pool),
            "avg_queue_size":           queue_size.get(pool),
            "avg_running_requests":     running_req.get(pool),
            "ready_endpoints":          ready_ep.get(pool),
        }
        for pool in sorted(all_pools)
    }
    return {"per_pool": per_pool}


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


def collect_request_metrics(metrics: Dict[str, float]) -> dict:
    """Build per-fairness_id request signals from llm_d_epp_request_* metrics.

    These are always present when the EPP is running, regardless of which
    fairness plugin (LAS/DRR/RR/none) is loaded. Keyed by fairness_id label.
    """
    PREFIX = "llm_d_epp"
    req_total   = extract_by_label(metrics, f"{PREFIX}_request_total",   "fairness_id")
    req_running = extract_by_label(metrics, f"{PREFIX}_request_running", "fairness_id")

    # All token/latency metrics are histograms — derive mean from sum/count.
    def _sum_count_by_fid(metric_name):
        result = {}
        label_re = re.compile(r'fairness_id="([^"]+)"')
        sum_re   = re.compile(rf'^{re.escape(metric_name)}_sum\{{')
        count_re = re.compile(rf'^{re.escape(metric_name)}_count\{{')
        for key, val in metrics.items():
            fid_m = label_re.search(key)
            if not fid_m:
                continue
            fid = fid_m.group(1)
            if sum_re.match(key):
                result.setdefault(fid, {})
                result[fid]["sum"] = result[fid].get("sum", 0.0) + val
            elif count_re.match(key):
                result.setdefault(fid, {})
                result[fid]["count"] = result[fid].get("count", 0.0) + val
        return result

    input_tok_sc  = _sum_count_by_fid(f"{PREFIX}_request_input_tokens")
    output_tok_sc = _sum_count_by_fid(f"{PREFIX}_request_output_tokens")
    duration_sc   = _sum_count_by_fid(f"{PREFIX}_request_duration_seconds")
    ttft_sc       = _sum_count_by_fid(f"{PREFIX}_request_ttft_seconds")
    ntpot_sc      = _sum_count_by_fid(f"{PREFIX}_request_ntpot_seconds")

    # Scheduler e2e (no fairness_id label — cluster-level histogram)
    sched_e2e = extract_histogram(metrics, f"{PREFIX}_scheduler_e2e_duration_seconds")

    all_fids = (set(req_total) | set(req_running)
                | set(input_tok_sc) | set(output_tok_sc)
                | set(duration_sc) | set(ttft_sc) | set(ntpot_sc))

    def _mean(sc, fid):
        d = sc.get(fid)
        if not d:
            return None
        c = d.get("count", 0)
        return d["sum"] / c if c else None

    per_fid = {
        fid: {
            "request_total":           req_total.get(fid),
            "request_running":         req_running.get(fid),
            "input_tokens":            _mean(input_tok_sc, fid),
            "output_tokens":           _mean(output_tok_sc, fid),
            "mean_duration_seconds":   _mean(duration_sc, fid),
            "mean_ttft_seconds":       _mean(ttft_sc, fid),
            "mean_ntpot_seconds":      _mean(ntpot_sc, fid),
        }
        for fid in sorted(all_fids)
    }

    return {
        "per_fid":        per_fid,
        "sched_e2e":      sched_e2e,
    }


def collect_cache_metrics(metrics: Dict[str, float]) -> dict:
    """Build the cache-reuse metric block.

    prefix_indexer_* are keyed by plugin_name (the prefix scorer); one entry
    per loaded scorer. request_cached_tokens is keyed by fairness_id. Raw
    sum/count are preserved per key for downstream mean/delta computation.
    """
    PREFIX = "llm_d_epp"
    hit_ratio = sum_count_by_label(metrics, f"{PREFIX}_prefix_indexer_hit_ratio", "plugin_name")
    hit_bytes = sum_count_by_label(metrics, f"{PREFIX}_prefix_indexer_hit_bytes", "plugin_name")
    cached_tokens = sum_count_by_label(metrics, f"{PREFIX}_request_cached_tokens", "fairness_id")
    return {
        "prefix_hit_ratio": hit_ratio,      # {plugin_name: {sum, count}}
        "prefix_hit_bytes": hit_bytes,      # {plugin_name: {sum, count}}
        "cached_tokens":    cached_tokens,  # {fairness_id: {sum, count}}
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
    ep = collect_endpoint_pool(metrics)
    rm = collect_request_metrics(metrics)
    cm = collect_cache_metrics(metrics)

    return {
        "ts":              ts,
        "fairness_index":  pa["fairness_index"],
        "per_program":     pa["per_program"],
        "flow_control":    fc,
        "endpoint_pool":   ep,
        "request_metrics": rm,
        "cache_metrics":   cm,
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