#!/bin/sh
# Wrapper that runs scrape_metrics.py in the background and stops it
# when the main inference-perf container signals completion via a shared file.
#
# Environment variables (set via container env in job.yaml):
#   METRICS_URL       — EPP metrics endpoint
#   SCRAPE_DURATION   — max scrape time (seconds)
#   SCRAPE_INTERVAL   — time between scrapes (seconds)
#   METRICS_OUTPUT    — output JSONL path

echo "[scraper-wrapper] Waiting 10s for EPP metrics endpoint..."
sleep 10

python3 /scripts/scrape_metrics.py \
    --url "${METRICS_URL}" \
    --duration "${SCRAPE_DURATION}" \
    --interval "${SCRAPE_INTERVAL}" \
    --output "${METRICS_OUTPUT}" &
SCRAPER_PID=$!

while [ ! -f /data/shared/done ]; do
    sleep 2
done

echo "[scraper-wrapper] Main container done, stopping scraper..."
kill $SCRAPER_PID 2>/dev/null || true
wait $SCRAPER_PID 2>/dev/null || true
echo "[scraper-wrapper] Exited."
