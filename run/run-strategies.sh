#!/usr/bin/env bash
# =============================================================================
# Run experiment across all configured scheduling strategies on the cluster.
#
# Usage: ./run-strategies.sh <run-name-prefix> [strategy-dir]
#
# For each strategy-*.yaml file found in the strategy directory:
#   1. Patches the EPP configmap with the strategy config
#   2. Runs a single experiment via run.sh
#
# After all strategies complete, downloads reports and runs comparison analysis.
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
MANIFESTS_DIR="${ROOT_DIR}/manifests"

# --- Source configuration ---
if [[ ! -f "${ROOT_DIR}/env.sh" ]]; then
    echo "ERROR: ${ROOT_DIR}/env.sh not found."
    echo "  Copy env.example.sh to env.sh and configure it."
    exit 1
fi
source "${ROOT_DIR}/env.sh"

CLI="${K8S_CLI:-oc}"

# --- Parse arguments ---
if [[ $# -lt 1 ]]; then
    echo "Usage: $0 <run-name-prefix> [strategy-dir]"
    echo ""
    echo "  run-name-prefix : base name for runs (creates <prefix>-<strategy> per strategy)"
    echo "  strategy-dir    : directory with strategy-*.yaml files (default: ../configs/)"
    exit 1
fi

RUN_PREFIX="$1"
STRATEGY_DIR="${2:-${ROOT_DIR}/configs}"

# --- Validate ---
if [[ ! -d "${STRATEGY_DIR}" ]]; then
    echo "ERROR: Strategy directory not found: ${STRATEGY_DIR}"
    exit 1
fi

shopt -s nullglob
STRATEGY_FILES=("${STRATEGY_DIR}"/strategy-*.yaml)
shopt -u nullglob

if [[ ${#STRATEGY_FILES[@]} -eq 0 ]]; then
    echo "ERROR: No strategy-*.yaml files found in ${STRATEGY_DIR}"
    exit 1
fi

echo "Found ${#STRATEGY_FILES[@]} strategies in ${STRATEGY_DIR}"

# --- ConfigMap patching helper ---
# Reads the current EPP ConfigMap, replaces featureGates + flowControl + the
# fairness plugin (first plugin in the list), keeps everything else intact.
patch_configmap() {
    local strategy_yaml="$1"

    local current
    current=$(${CLI} -n "${K8S_NAMESPACE}" get cm "${K8S_EPP_CONFIGMAP}" -o json | python3 -c "
import sys, json
cm = json.load(sys.stdin)
print(cm['data']['${K8S_EPP_CONFIGMAP_KEY}'])
")

    local strategy_content
    strategy_content=$(cat "$strategy_yaml")

    local updated
    updated=$(export _STRATEGY="$strategy_content"; echo "$current" | python3 -c "
import sys, os, yaml

doc = yaml.safe_load(sys.stdin)
strategy = yaml.safe_load(os.environ['_STRATEGY'])

# Replace featureGates and flowControl
doc['featureGates'] = strategy.get('featureGates', [])
doc['flowControl'] = strategy.get('flowControl', {})

# Replace the fairness plugin(s)
fairness_types = {'program-aware-fairness', 'round-robin-fairness-policy'}
new_plugins = strategy.get('plugins', [])

# Remove old fairness plugin(s) from the list
remaining = [p for p in doc.get('plugins', []) if p.get('type') not in fairness_types]

# Insert new fairness plugin(s) at position 0
doc['plugins'] = new_plugins + remaining

yaml.dump(doc, sys.stdout, default_flow_style=False, sort_keys=False)
")

    ${CLI} -n "${K8S_NAMESPACE}" patch cm "${K8S_EPP_CONFIGMAP}" \
        --type merge \
        -p "{\"data\":{\"${K8S_EPP_CONFIGMAP_KEY}\":$(echo "$updated" | python3 -c 'import sys,json; print(json.dumps(sys.stdin.read()))')}}"
}

# --- Run each strategy ---
STRATEGY_NAMES=()

for strategy_file in "${STRATEGY_FILES[@]}"; do
    strategy_name=$(basename "$strategy_file" .yaml | sed 's/^strategy-//')
    run_name="${RUN_PREFIX}-${strategy_name}"
    STRATEGY_NAMES+=("$strategy_name")

    echo ""
    echo "========================================"
    echo "Strategy: ${strategy_name} | Run: ${run_name}"
    echo "========================================"

    # Skip if reports already downloaded
    local_reports="${RESULTS_BASE_DIR}/${RUN_PREFIX}/${strategy_name}/reports"
    if [[ -d "${local_reports}" ]] && [[ "$(ls -A "${local_reports}" 2>/dev/null)" ]]; then
        echo "SKIP: ${strategy_name} reports already exist at ${local_reports}"
        continue
    fi

    # Skip if job already completed on cluster
    job_name="inference-perf-${run_name}"
    if ${CLI} -n "${K8S_NAMESPACE}" get job "${job_name}" -o jsonpath='{.status.conditions[?(@.type=="Complete")].status}' 2>/dev/null | grep -q "True"; then
        echo "SKIP: Job already completed on cluster. Will download later."
        continue
    fi

    # Patch configmap with strategy (if configured)
    if [[ -n "${K8S_EPP_CONFIGMAP:-}" ]]; then
        echo "Patching EPP configmap → strategy: ${strategy_name}"
        patch_configmap "$strategy_file"
    fi

    # Run the experiment
    "${SCRIPT_DIR}/run.sh" "${run_name}" "${strategy_name}"
    echo "Done: ${strategy_name}"
done

# --- Download remaining reports ---
# After all strategies complete, some may have been skipped earlier (already
# completed on cluster but not yet downloaded). This section downloads any
# missing reports from the shared PVC.
echo ""
echo "Downloading any remaining reports..."
PVC_NAME="inference-perf-results-${RUN_PREFIX}"
HELPER_POD="report-download-${RUN_PREFIX}"
export PVC_NAME HELPER_POD K8S_NAMESPACE

${CLI} -n "${K8S_NAMESPACE}" delete pod "${HELPER_POD}" --ignore-not-found --wait=true 2>/dev/null

envsubst < "${MANIFESTS_DIR}/helper-pod.yaml" | ${CLI} apply -f -

${CLI} -n "${K8S_NAMESPACE}" wait --for=condition=ready pod/"${HELPER_POD}" --timeout=120s

for strategy_name in "${STRATEGY_NAMES[@]}"; do
    local_dir="${RESULTS_BASE_DIR}/${RUN_PREFIX}/${strategy_name}/reports"
    if [[ -d "${local_dir}" ]] && [[ "$(ls -A "${local_dir}" 2>/dev/null)" ]]; then
        :
    else
        mkdir -p "${local_dir}"
        ${CLI} -n "${K8S_NAMESPACE}" cp "${HELPER_POD}:/data/${strategy_name}/reports/" "${local_dir}/" 2>/dev/null && \
            echo "Downloaded reports: ${strategy_name}" || \
            echo "Not found: ${strategy_name} reports (job may not have run)"
    fi

    # Download metrics if scraper was enabled
    if [[ -n "${EPP_METRICS_URL:-}" ]]; then
        local_metrics="${RESULTS_BASE_DIR}/${RUN_PREFIX}/${strategy_name}/metrics"
        if [[ ! -f "${local_metrics}/metrics.jsonl" ]]; then
            mkdir -p "${local_metrics}"
            ${CLI} -n "${K8S_NAMESPACE}" cp "${HELPER_POD}:/data/${strategy_name}/metrics/" "${local_metrics}/" 2>/dev/null && \
                echo "Downloaded metrics: ${strategy_name}" || \
                echo "Not found: ${strategy_name} metrics"
        fi
    fi
done

${CLI} -n "${K8S_NAMESPACE}" delete pod "${HELPER_POD}" --wait=false 2>/dev/null

# --- Generate comparison plots ---
echo ""
echo "Generating comparison plots..."
COMPARE_OUT="${RESULTS_BASE_DIR}/${RUN_PREFIX}/comparison/"
mkdir -p "${COMPARE_OUT}"

COMPARE_ARGS=()
for name in "${STRATEGY_NAMES[@]}"; do
    report_dir="${RESULTS_BASE_DIR}/${RUN_PREFIX}/${name}/reports"
    if [[ -d "$report_dir" ]] && [[ "$(ls -A "$report_dir" 2>/dev/null)" ]]; then
        COMPARE_ARGS+=("--${name}" "$report_dir")
    fi
done

if [[ ${#COMPARE_ARGS[@]} -lt 4 ]]; then
    echo "WARNING: Fewer than 2 strategies have results, skipping comparison."
else
    python3 "${ROOT_DIR}/analysis/compare_reports.py" \
        "${COMPARE_ARGS[@]}" \
        -o "${COMPARE_OUT}"
    echo "Comparison plots saved to: ${COMPARE_OUT}"
fi

echo ""
echo "All strategies complete."
echo "Results in: ${RESULTS_BASE_DIR}/${RUN_PREFIX}/"
