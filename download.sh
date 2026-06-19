#!/bin/bash
set -euo pipefail

# =============================================================================
# download.sh — Download reports from PVC and run comparison analysis
#
# Usage: ./experiments/download.sh <run-name-prefix>
# =============================================================================

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PLOTS_DIR="${SCRIPT_DIR}/plots"

# Load environment config
if [ ! -f "${SCRIPT_DIR}/.env" ]; then
  echo "ERROR: ${SCRIPT_DIR}/.env not found."
  echo "  cp ${SCRIPT_DIR}/.env.example ${SCRIPT_DIR}/.env"
  echo "  Then fill in your values."
  exit 1
fi
source "${SCRIPT_DIR}/.env"

NS="${NAMESPACE}"

if [ $# -lt 1 ]; then
  echo "Usage: $0 <run-name-prefix>"
  exit 1
fi

RUN_PREFIX="$1"
PVC_NAME="inference-perf-results-${RUN_PREFIX}"
LOCAL_RESULTS="${PWD}/${RUN_PREFIX}"
mkdir -p "$LOCAL_RESULTS"

# --- Check what needs downloading ---
need_download=false
for strategy in las rr no-fairness; do
  local_dir="${LOCAL_RESULTS}/${strategy}/reports"
  if [ ! -d "$local_dir" ] || [ -z "$(ls -A "$local_dir" 2>/dev/null)" ]; then
    need_download=true
    break
  fi
done

# Save the current EPP config
if ! [ -f "${LOCAL_RESULTS}/epp-configmap.yaml" ]; then
  oc -n "$NS" get cm "agentic-serving-epp" -o yaml > "${LOCAL_RESULTS}/epp-configmap.yaml" 2>/dev/null || true
fi

if [ "$need_download" = true ]; then
  HELPER_POD="report-download-${RUN_PREFIX}"

  echo "Starting download helper pod..."
  oc -n "$NS" delete pod "$HELPER_POD" --ignore-not-found --wait=true 2>/dev/null

  cat <<EOF | oc apply -f -
apiVersion: v1
kind: Pod
metadata:
  name: ${HELPER_POD}
  namespace: ${NS}
spec:
  restartPolicy: Never
  containers:
    - name: downloader
      image: busybox:latest
      command: ["sleep", "600"]
      volumeMounts:
        - name: results-volume
          mountPath: /data
  volumes:
    - name: results-volume
      persistentVolumeClaim:
        claimName: ${PVC_NAME}
EOF

  oc -n "$NS" wait --for=condition=ready pod/"$HELPER_POD" --timeout=120s

  for strategy in las rr no-fairness; do
    echo "--- $strategy ---"

    local_dir="${LOCAL_RESULTS}/${strategy}/reports"
    if [ -d "$local_dir" ] && [ -n "$(ls -A "$local_dir" 2>/dev/null)" ]; then
      echo "  SKIP: reports already downloaded"
    else
      mkdir -p "$local_dir"
      oc -n "$NS" cp "${HELPER_POD}:/data/${strategy}/reports/" "$local_dir/" 2>/dev/null && \
        echo "  ✓ reports → $local_dir" || \
        echo "  ✗ reports not found"
    fi

    metrics_dir="${LOCAL_RESULTS}/${strategy}/metrics"
    if [ -f "${metrics_dir}/metrics.jsonl" ]; then
      echo "  SKIP: metrics already downloaded"
    else
      mkdir -p "$metrics_dir"
      oc -n "$NS" cp "${HELPER_POD}:/data/${strategy}/metrics/" "$metrics_dir/" 2>/dev/null && \
        echo "  ✓ metrics → $metrics_dir" || \
        echo "  ✗ metrics not found"
    fi
  done

  oc -n "$NS" delete pod "$HELPER_POD" --wait=false 2>/dev/null
  echo ""
  echo "Download complete."
else
  echo "All reports already downloaded locally. Skipping PVC access."
fi

# =============================================================================
# LOCAL ANALYSIS
# =============================================================================

echo ""
echo "Running local analysis..."

for strategy in las rr no-fairness; do
  reports_dir="${LOCAL_RESULTS}/${strategy}/reports"
  if [ -d "$reports_dir" ] && [ -n "$(ls -A "$reports_dir" 2>/dev/null)" ]; then
    echo "  Generating plots for $strategy..."
    python3 "${PLOTS_DIR}/analyze_reports.py" \
      "$reports_dir" \
      -o "${LOCAL_RESULTS}/${strategy}/" \
      2>&1 | tail -3
  fi
done

# Build comparison args from whichever strategies have reports
COMPARE_ARGS=""
for strategy in no-fairness rr las drr; do
  reports_dir="${LOCAL_RESULTS}/${strategy}/reports"
  if [ -d "$reports_dir" ] && [ -n "$(ls -A "$reports_dir" 2>/dev/null)" ]; then
    COMPARE_ARGS="${COMPARE_ARGS} --${strategy} ${reports_dir}"
  fi
done

# Count how many strategies we have (each --flag counts as 2 words)
num_strategies=$(echo $COMPARE_ARGS | wc -w)
num_strategies=$((num_strategies / 2))

if [ "$num_strategies" -ge 2 ]; then
  COMPARE_OUT="${LOCAL_RESULTS}/comparison"
  mkdir -p "$COMPARE_OUT"
  echo "  Generating comparison plots (all sessions)..."
  python3 "${PLOTS_DIR}/compare_reports.py" \
    $COMPARE_ARGS \
    -o "$COMPARE_OUT" \
    2>&1 | tail -5

  COMPARE_MATCHED="${LOCAL_RESULTS}/comparison-matched"
  mkdir -p "$COMPARE_MATCHED"
  echo "  Generating comparison plots (matched programs only)..."
  python3 "${PLOTS_DIR}/compare_reports.py" \
    $COMPARE_ARGS \
    --matched-only \
    -o "$COMPARE_MATCHED" \
    2>&1 | tail -5
else
  echo "  SKIP: Need at least 2 strategies with reports for comparison (found $num_strategies)."
fi

echo ""
echo "All done. Results in: $LOCAL_RESULTS"
