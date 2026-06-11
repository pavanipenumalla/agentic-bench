#!/usr/bin/env bash
# =============================================================================
# Run a single agentic trace replay experiment on the cluster.
#
# Usage: ./run.sh <run-name> [strategy-name]
#
# This script runs FROM YOUR LAPTOP but the actual load generation happens
# inside the cluster as a Kubernetes Job. Laptop sleep / VPN drops don't
# affect the run. Reports are stored on a PVC.
#
# Prerequisites: env.sh configured (see env.example.sh)
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
MANIFESTS_DIR="${ROOT_DIR}/manifests"

# --- Source configuration ---
if [[ ! -f "${ROOT_DIR}/env.sh" ]]; then
    echo "ERROR: ${ROOT_DIR}/env.sh not found."
    echo "  Copy env.example.sh to env.sh and configure it:"
    echo "    cp ${ROOT_DIR}/env.example.sh ${ROOT_DIR}/env.sh"
    exit 1
fi
source "${ROOT_DIR}/env.sh"

# --- Parse arguments ---
if [[ $# -lt 1 ]]; then
    echo "Usage: $0 <run-name> [strategy-name]"
    echo "  run-name      : identifier for this experiment run"
    echo "  strategy-name : subdirectory on PVC for results (default: run-name)"
    exit 1
fi

RUN_NAME="$1"
STRATEGY_NAME="${2:-${RUN_NAME}}"
CLI="${K8S_CLI:-oc}"
JOB_NAME="inference-perf-${RUN_NAME}"

# --- Auth check ---
if ! ${CLI} whoami &>/dev/null 2>&1; then
    echo "ERROR: Not authenticated. Run:"
    echo "  ${CLI} login --token=<token> --server=<api-server>"
    exit 1
fi
echo "Authenticated as: $(${CLI} whoami)"

# --- Create PVC if it doesn't exist ---
PVC_NAME="inference-perf-results-${RUN_NAME%%-*}"
export PVC_NAME K8S_NAMESPACE HELPER_POD

if ! ${CLI} -n "${K8S_NAMESPACE}" get pvc "${PVC_NAME}" &>/dev/null; then
    echo "Creating PVC '${PVC_NAME}'..."
    envsubst < "${MANIFESTS_DIR}/pvc.yaml" | ${CLI} apply -f -
else
    echo "PVC '${PVC_NAME}' already exists."
fi

# --- Upload scraper script as ConfigMap (if metrics URL configured) ---
if [[ -n "${EPP_METRICS_URL:-}" ]]; then
    SCRAPER_DIR="${ROOT_DIR}/scraper"
    echo "Creating/updating scraper ConfigMap '${SCRAPER_CONFIGMAP}'..."
    ${CLI} -n "${K8S_NAMESPACE}" create configmap "${SCRAPER_CONFIGMAP}" \
        --from-file=scrape_metrics.py="${SCRAPER_DIR}/scrape_metrics.py" \
        --from-file=run_scraper.sh="${SCRAPER_DIR}/run_scraper.sh" \
        --dry-run=client -o yaml | ${CLI} apply -f -
fi

# --- Restart model service (scale 0 → 1 for clean KV cache) ---
if [[ -n "${K8S_MODEL_DEPLOYMENT:-}" ]]; then
    echo "Cycling model service (scale 0 → 1)..."
    ${CLI} -n "${K8S_NAMESPACE}" scale deployment/"${K8S_MODEL_DEPLOYMENT}" --replicas=0
    ${CLI} -n "${K8S_NAMESPACE}" rollout status deployment/"${K8S_MODEL_DEPLOYMENT}" --timeout=120s

    ${CLI} -n "${K8S_NAMESPACE}" scale deployment/"${K8S_MODEL_DEPLOYMENT}" --replicas=1

    MAX_RETRIES=30
    for ((i=1; i<=MAX_RETRIES; i++)); do
        if ${CLI} -n "${K8S_NAMESPACE}" rollout status deployment/"${K8S_MODEL_DEPLOYMENT}" --timeout=60s 2>/dev/null; then
            echo "Model service ready."
            break
        fi
        if [[ "$i" -eq "$MAX_RETRIES" ]]; then
            echo "ERROR: Model service did not become ready after ${MAX_RETRIES} attempts."
            exit 1
        fi
        echo "  Attempt ${i}/${MAX_RETRIES} — retrying in 30s..."
        sleep 30
    done
fi

# --- Restart EPP (pick up any config changes) ---
if [[ -n "${K8S_EPP_DEPLOYMENT:-}" ]]; then
    if [[ -n "${K8S_EPP_IMAGE:-}" ]]; then
        local_image=$(${CLI} -n "${K8S_NAMESPACE}" get deployment/"${K8S_EPP_DEPLOYMENT}" \
            -o jsonpath='{.spec.template.spec.containers[?(@.name=="epp")].image}' 2>/dev/null)
        if [[ "$local_image" != "${K8S_EPP_IMAGE}" ]]; then
            echo "Updating EPP image to: ${K8S_EPP_IMAGE}"
            ${CLI} -n "${K8S_NAMESPACE}" set image deployment/"${K8S_EPP_DEPLOYMENT}" epp="${K8S_EPP_IMAGE}"
        fi
    fi

    echo "Restarting EPP deployment..."
    ${CLI} -n "${K8S_NAMESPACE}" rollout restart deployment/"${K8S_EPP_DEPLOYMENT}"
    ${CLI} -n "${K8S_NAMESPACE}" rollout status deployment/"${K8S_EPP_DEPLOYMENT}" --timeout=300s
    echo "EPP ready."
fi

# --- Delete old job if it exists (from a previous failed attempt) ---
${CLI} -n "${K8S_NAMESPACE}" delete job "${JOB_NAME}" --ignore-not-found

# --- Launch inference-perf Job ---
echo "Launching Job '${JOB_NAME}'..."
export JOB_NAME STRATEGY_NAME JOB_DEADLINE_SECONDS INFERENCE_PERF_IMAGE
export HF_SECRET_NAME HF_SECRET_KEY
export JOB_CPU_REQUEST JOB_MEMORY_REQUEST JOB_CPU_LIMIT JOB_MEMORY_LIMIT
export INFERENCE_PERF_CONFIGMAP SCRAPER_CONFIGMAP
export EPP_METRICS_URL SCRAPE_INTERVAL

envsubst < "${MANIFESTS_DIR}/job.yaml" | ${CLI} apply -f -
echo "Job '${JOB_NAME}' launched."

# --- Wait for completion ---
echo "Waiting for job to complete (laptop can sleep — job runs on cluster)..."
if ! ${CLI} -n "${K8S_NAMESPACE}" wait --for=condition=complete job/"${JOB_NAME}" --timeout="${JOB_DEADLINE_SECONDS}s" 2>/dev/null; then
    echo "WARNING: Job may have failed. Checking status..."
    ${CLI} -n "${K8S_NAMESPACE}" get job "${JOB_NAME}"
    ${CLI} -n "${K8S_NAMESPACE}" logs "job/${JOB_NAME}" --tail=50
    echo ""
    echo "Full logs: ${CLI} -n ${K8S_NAMESPACE} logs job/${JOB_NAME}"
    exit 1
fi

echo "Job completed successfully."

# --- Download reports and metrics ---
LOCAL_REPORTS="${RESULTS_BASE_DIR}/${RUN_NAME}/${STRATEGY_NAME}/reports"
LOCAL_METRICS="${RESULTS_BASE_DIR}/${RUN_NAME}/${STRATEGY_NAME}/metrics"
mkdir -p "${LOCAL_REPORTS}" "${LOCAL_METRICS}"

HELPER_POD="report-download-${RUN_NAME}"
export HELPER_POD
${CLI} -n "${K8S_NAMESPACE}" delete pod "${HELPER_POD}" --ignore-not-found --wait=true 2>/dev/null

envsubst < "${MANIFESTS_DIR}/helper-pod.yaml" | ${CLI} apply -f -

echo "Waiting for download helper pod..."
${CLI} -n "${K8S_NAMESPACE}" wait --for=condition=ready pod/"${HELPER_POD}" --timeout=120s

echo "Downloading reports..."
${CLI} -n "${K8S_NAMESPACE}" cp "${HELPER_POD}:/data/${STRATEGY_NAME}/reports/" "${LOCAL_REPORTS}/" 2>/dev/null && \
    echo "Reports downloaded to: ${LOCAL_REPORTS}" || \
    echo "WARNING: Failed to download reports."

if [[ -n "${EPP_METRICS_URL:-}" ]]; then
    echo "Downloading metrics..."
    ${CLI} -n "${K8S_NAMESPACE}" cp "${HELPER_POD}:/data/${STRATEGY_NAME}/metrics/" "${LOCAL_METRICS}/" 2>/dev/null && \
        echo "Metrics downloaded to: ${LOCAL_METRICS}" || \
        echo "WARNING: Failed to download metrics."
fi

${CLI} -n "${K8S_NAMESPACE}" delete pod "${HELPER_POD}" --wait=false 2>/dev/null

# --- Run per-strategy analysis ---
STRATEGY_DIR="${RESULTS_BASE_DIR}/${RUN_NAME}/${STRATEGY_NAME}"

if [[ -d "${LOCAL_REPORTS}" ]] && [[ "$(ls -A "${LOCAL_REPORTS}" 2>/dev/null)" ]]; then
    echo "Generating session plots..."
    python3 "${ROOT_DIR}/analysis/analyze_reports.py" \
        "${LOCAL_REPORTS}" \
        -o "${STRATEGY_DIR}/" 2>&1 | tail -3
fi

if [[ -f "${LOCAL_METRICS}/metrics.jsonl" ]]; then
    echo "Generating EPP metrics plots..."
    python3 "${ROOT_DIR}/analysis/analyze_epp.py" \
        "${LOCAL_METRICS}/metrics.jsonl" \
        -o "${LOCAL_METRICS}/" 2>&1 | tail -3
fi

echo "Run complete: ${RUN_NAME} (strategy: ${STRATEGY_NAME})"
echo "Results: ${STRATEGY_DIR}/"
