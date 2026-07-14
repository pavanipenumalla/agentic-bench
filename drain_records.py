#!/usr/bin/env python3
"""
Per-request record drainer for the EPP debug log.

The EPP (built with EPP_REQUEST_RECORDS=1) buffers one raw record per completed
request and serves them at GET /debug/request-records, returning and clearing
the buffer on each call. This script polls that endpoint on an interval and
appends the returned JSONL to an output file verbatim, so the append-only
records survive an EPP restart (the sidecar writes to a PVC, not the EPP pod).

Each record is already one JSON object per line; this drainer does not parse or
reshape them. The response's X-Records-Dropped header reports the EPP ring
buffer's cumulative drop count, logged whenever it increases so buffer overflow
is never silent.

Usage:
    python3 drain_records.py \
        --url http://localhost:9090/debug/request-records \
        --duration 43200 \
        --output results/records.jsonl \
        --interval 5
"""

import argparse
import os
import time
import urllib.request


def drain_once(url: str):
    """GET the drain endpoint. Returns (body_text, dropped_count).

    On any error returns ("", None) so a transient failure never kills the loop.
    """
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            body = resp.read().decode("utf-8")
            dropped = resp.headers.get("X-Records-Dropped")
            return body, (int(dropped) if dropped is not None else None)
    except Exception as e:
        print(f"[drainer] poll error: {e}", flush=True)
        return "", None


def main():
    parser = argparse.ArgumentParser(description="EPP per-request record drainer")
    parser.add_argument("--url",      required=True,             help="Drain endpoint URL (/debug/request-records)")
    parser.add_argument("--duration", type=int, required=True,   help="How long to drain (seconds)")
    parser.add_argument("--output",   required=True,             help="Output JSONL file path (appended)")
    parser.add_argument("--interval", type=float, default=5.0,   help="Drain interval (seconds)")
    parser.add_argument("--done-file", default=None,             help="Exit cleanly (running a final drain) when this file appears")
    args = parser.parse_args()

    print(f"[drainer] url={args.url}  duration={args.duration}s  output={args.output}  interval={args.interval}s", flush=True)

    t0        = time.time()
    end_time  = t0 + args.duration
    next_t    = t0
    total     = 0
    last_drop = 0

    # Append so a restart of this sidecar (or the EPP) never truncates prior
    # records. One final drain after the loop catches the tail.
    with open(args.output, "a") as f:
        while time.time() < end_time:
            if args.done_file and os.path.exists(args.done_file):
                print("[drainer] done file present; stopping after final drain", flush=True)
                break
            now = time.time()
            if now >= next_t:
                body, dropped = drain_once(args.url)
                if body:
                    f.write(body if body.endswith("\n") else body + "\n")
                    f.flush()
                    total += body.count("\n")
                if dropped is not None and dropped > last_drop:
                    print(f"[drainer] WARNING: EPP dropped {dropped - last_drop} records "
                          f"(buffer overflow, cumulative {dropped})", flush=True)
                    last_drop = dropped
                next_t += args.interval

            sleep_for = next_t - time.time()
            if sleep_for > 0:
                time.sleep(min(sleep_for, 0.1))

        # Final drain to capture records buffered since the last poll.
        body, _ = drain_once(args.url)
        if body:
            f.write(body if body.endswith("\n") else body + "\n")
            f.flush()
            total += body.count("\n")

    print(f"[drainer] Done. {total} records written to {args.output}", flush=True)


if __name__ == "__main__":
    main()
