#!/usr/bin/env bash
# =============================================================================
# Environment configuration for on-cluster agentic trace replay experiments.
#
# Usage:
#   1. Copy this file:  cp env.example.sh env.sh
#   2. Edit env.sh with your cluster-specific values
#   3. The run scripts source env.sh automatically
#
# All variables use ${VAR:-default} so they can be overridden via environment
# variables without editing this file.
# =============================================================================

# --- Paths ---
# Root directory of this guide repo
GUIDE_DIR="${GUIDE_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
# Directory where experiment results are downloaded from the cluster
RESULTS_BASE_DIR="${RESULTS_BASE_DIR:-${GUIDE_DIR}/results}"

# --- Container Image ---
# inference-perf image to run as a Job on the cluster
INFERENCE_PERF_IMAGE="${INFERENCE_PERF_IMAGE:-quay.io/<your-org>/inference-perf:latest}"

# --- Kubernetes ---
# Namespace where llm-d and inference-perf resources are deployed
K8S_NAMESPACE="${K8S_NAMESPACE:-default}"
# CLI tool: "oc" for OpenShift, "kubectl" for vanilla Kubernetes
K8S_CLI="${K8S_CLI:-oc}"

# Name of the EPP (Endpoint Picker Plugin) deployment
K8S_EPP_DEPLOYMENT="${K8S_EPP_DEPLOYMENT:-}"
# Name of the model server deployment
K8S_MODEL_DEPLOYMENT="${K8S_MODEL_DEPLOYMENT:-}"
# ConfigMap name used by EPP for routing/scheduling config
K8S_EPP_CONFIGMAP="${K8S_EPP_CONFIGMAP:-}"
# Key within the EPP configmap that holds the plugin config YAML
K8S_EPP_CONFIGMAP_KEY="${K8S_EPP_CONFIGMAP_KEY:-}"

# EPP image (use custom image for strategy evaluation, leave empty to keep current)
K8S_EPP_IMAGE="${K8S_EPP_IMAGE:-}"

# --- inference-perf Job ---
# ConfigMap name for the inference-perf config file
INFERENCE_PERF_CONFIGMAP="${INFERENCE_PERF_CONFIGMAP:-inference-perf-config}"
# Resource requests/limits for the Job pod
JOB_CPU_REQUEST="${JOB_CPU_REQUEST:-2}"
JOB_CPU_LIMIT="${JOB_CPU_LIMIT:-4}"
JOB_MEMORY_REQUEST="${JOB_MEMORY_REQUEST:-16Gi}"
JOB_MEMORY_LIMIT="${JOB_MEMORY_LIMIT:-32Gi}"
# Job timeout (seconds) — kills the Job if it exceeds this
JOB_DEADLINE_SECONDS="${JOB_DEADLINE_SECONDS:-43200}"

# --- HuggingFace (optional) ---
# Secret name containing the HF token (set to empty if dataset is public)
HF_SECRET_NAME="${HF_SECRET_NAME:-hf-secret}"
# Key within the secret that holds the token
HF_SECRET_KEY="${HF_SECRET_KEY:-hf_api_token}"

# --- Metrics Scraper (optional, runs as sidecar in Job pod) ---
# Cluster-internal URL to EPP metrics endpoint. Leave empty to disable scraping.
# Format: http://<epp-service>.<namespace>.svc.cluster.local:9090/metrics
EPP_METRICS_URL="${EPP_METRICS_URL:-}"
# ConfigMap name for the scraper script (auto-created by run.sh)
SCRAPER_CONFIGMAP="${SCRAPER_CONFIGMAP:-epp-metrics-scraper}"
# Scrape interval in seconds
SCRAPE_INTERVAL="${SCRAPE_INTERVAL:-0.5}"
