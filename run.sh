#!/bin/bash
set -euo pipefail

# =============================================================================
# run.sh — Submit autonomous orchestrator Job to the cluster
#
# Runs all strategies without needing a laptop connection.
# The orchestrator pod patches EPP, restarts model server, and runs jobs.
#
# Usage:
#   ./experiments/run.sh <run-name-prefix> [strategies]
#   ./experiments/run.sh guide-run-1 "rr no-fairness"
#
# After all strategies finish:
#   ./experiments/download.sh <run-name-prefix>
# =============================================================================

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Load environment config
if [ ! -f "${SCRIPT_DIR}/.env" ]; then
  echo "ERROR: ${SCRIPT_DIR}/.env not found."
  echo "  cp ${SCRIPT_DIR}/.env.example ${SCRIPT_DIR}/.env"
  echo "  Then fill in your values."
  exit 1
fi
source "${SCRIPT_DIR}/.env"

NS="${NAMESPACE}"
GUIDE_NAME="${GUIDE_NAME}"
MODEL_DEPLOY="${MODEL_DEPLOY}"
INFERENCE_PERF_IMAGE="${INFERENCE_PERF_IMAGE}"
INFERENCE_PERF_CONFIGMAP="inference-perf-config"
SCRAPER_CONFIGMAP="epp-metrics-scraper"
EPP_METRICS_URL="http://${GUIDE_NAME}-epp.${NS}.svc.cluster.local:9090/metrics"

SCRAPER_SCRIPT="${SCRIPT_DIR}/scrape_metrics.py"
CONFIG_FILE="${SCRIPT_DIR}/config.yml"

if [ $# -lt 1 ]; then
  echo "Usage: $0 <run-name-prefix> [strategies]"
  echo "  strategies: space-separated list (default: \"las rr no-fairness\")"
  echo "  Example: $0 guide-run-1 \"rr no-fairness\""
  exit 1
fi

RUN_PREFIX="$1"
STRATEGIES="${2:-las rr no-fairness}"
PVC_NAME="inference-perf-results-${RUN_PREFIX}"
LOCAL_RESULTS="${PWD}/${RUN_PREFIX}"
ORCHESTRATOR_JOB="orchestrator-${RUN_PREFIX}"
ORCHESTRATOR_SA="orchestrator-sa"

mkdir -p "$LOCAL_RESULTS"
sed "s|GUIDE_NAME|${GUIDE_NAME}|g; s|NAMESPACE|${NS}|g" "$CONFIG_FILE" > "${LOCAL_RESULTS}/config.yml"

# Save strategy configs locally
for strat in $STRATEGIES; do
  case "$strat" in
    las) cat > "${LOCAL_RESULTS}/las-epp-plugins.yaml" << 'YAML'
apiVersion: inference.networking.x-k8s.io/v1alpha1
kind: EndpointPickerConfig
plugins:
- type: agent-identity
- type: queue-scorer
- type: kv-cache-utilization-scorer
- type: prefix-cache-scorer
- type: program-aware-fairness
featureGates:
- flowControl
flowControl:
  defaultPriorityBand:
    fairnessPolicyRef: program-aware-fairness
schedulingProfiles:
- name: default
  plugins:
  - pluginRef: queue-scorer
    weight: 2
  - pluginRef: kv-cache-utilization-scorer
    weight: 2
  - pluginRef: prefix-cache-scorer
    weight: 3
YAML
    ;;
    rr) cat > "${LOCAL_RESULTS}/rr-epp-plugins.yaml" << 'YAML'
apiVersion: inference.networking.x-k8s.io/v1alpha1
kind: EndpointPickerConfig
plugins:
- type: agent-identity
- type: queue-scorer
- type: kv-cache-utilization-scorer
- type: prefix-cache-scorer
- type: round-robin-fairness-policy
featureGates:
- flowControl
flowControl:
  defaultPriorityBand:
    fairnessPolicyRef: round-robin-fairness-policy
schedulingProfiles:
- name: default
  plugins:
  - pluginRef: queue-scorer
    weight: 2
  - pluginRef: kv-cache-utilization-scorer
    weight: 2
  - pluginRef: prefix-cache-scorer
    weight: 3
YAML
    ;;
    no-fairness) cat > "${LOCAL_RESULTS}/no-fairness-epp-plugins.yaml" << 'YAML'
apiVersion: inference.networking.x-k8s.io/v1alpha1
kind: EndpointPickerConfig
plugins:
- type: queue-scorer
- type: kv-cache-utilization-scorer
- type: prefix-cache-scorer
schedulingProfiles:
- name: default
  plugins:
  - pluginRef: queue-scorer
    weight: 2
  - pluginRef: kv-cache-utilization-scorer
    weight: 2
  - pluginRef: prefix-cache-scorer
    weight: 3
YAML
    ;;
  esac
done

# --- Pre-flight ---
if ! oc whoami &>/dev/null; then
  echo "ERROR: Not logged in. Run: oc login ..."
  exit 1
fi
echo "Authenticated as: $(oc whoami)"

# --- Ensure PVC ---
if ! oc -n "$NS" get pvc "$PVC_NAME" &>/dev/null; then
  echo "Creating PVC '$PVC_NAME'..."
  cat <<EOF | oc apply -f -
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: ${PVC_NAME}
  namespace: ${NS}
spec:
  accessModes: [ReadWriteOnce]
  resources:
    requests:
      storage: 10Gi
EOF
fi

# --- Ensure ConfigMaps ---
oc -n "$NS" create configmap "$SCRAPER_CONFIGMAP" \
  --from-file=scrape_metrics.py="$SCRAPER_SCRIPT" \
  --dry-run=client -o yaml | oc apply -f -

# Template config.yml with environment values before uploading
TEMPLATED_CONFIG=$(mktemp)
sed "s|GUIDE_NAME|${GUIDE_NAME}|g; s|NAMESPACE|${NS}|g" "$CONFIG_FILE" > "$TEMPLATED_CONFIG"
oc -n "$NS" create configmap "$INFERENCE_PERF_CONFIGMAP" \
  --from-file=config.yml="$TEMPLATED_CONFIG" \
  --dry-run=client -o yaml | oc apply -f -
rm -f "$TEMPLATED_CONFIG"

# --- Ensure ServiceAccount + RBAC for orchestrator ---
echo "Setting up orchestrator RBAC..."
cat <<EOF | oc apply -f -
apiVersion: v1
kind: ServiceAccount
metadata:
  name: ${ORCHESTRATOR_SA}
  namespace: ${NS}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: ${ORCHESTRATOR_SA}
  namespace: ${NS}
rules:
- apiGroups: [""]
  resources: ["configmaps"]
  verbs: ["get", "patch"]
- apiGroups: [""]
  resources: ["pods"]
  verbs: ["get", "list", "watch"]
- apiGroups: ["apps"]
  resources: ["deployments"]
  verbs: ["get", "list", "watch", "patch"]
- apiGroups: ["apps"]
  resources: ["deployments/scale"]
  verbs: ["get", "list", "watch", "patch", "update"]
- apiGroups: ["apps"]
  resources: ["replicasets"]
  verbs: ["get", "list", "watch"]
- apiGroups: ["batch"]
  resources: ["jobs"]
  verbs: ["get", "list", "watch", "create", "delete"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: ${ORCHESTRATOR_SA}
  namespace: ${NS}
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: ${ORCHESTRATOR_SA}
subjects:
- kind: ServiceAccount
  name: ${ORCHESTRATOR_SA}
  namespace: ${NS}
EOF

# --- Delete old orchestrator job if exists ---
oc -n "$NS" delete job "$ORCHESTRATOR_JOB" --ignore-not-found

# --- Submit orchestrator Job ---
echo "Submitting orchestrator job..."
cat <<'JOBEOF' | sed "s|__NS__|${NS}|g; s|__GUIDE_NAME__|${GUIDE_NAME}|g; s|__MODEL_DEPLOY__|${MODEL_DEPLOY}|g; s|__PVC_NAME__|${PVC_NAME}|g; s|__RUN_PREFIX__|${RUN_PREFIX}|g; s|__STRATEGIES__|${STRATEGIES}|g; s|__INFERENCE_PERF_IMAGE__|${INFERENCE_PERF_IMAGE}|g; s|__INFERENCE_PERF_CONFIGMAP__|${INFERENCE_PERF_CONFIGMAP}|g; s|__SCRAPER_CONFIGMAP__|${SCRAPER_CONFIGMAP}|g; s|__EPP_METRICS_URL__|${EPP_METRICS_URL}|g; s|__ORCHESTRATOR_JOB__|${ORCHESTRATOR_JOB}|g; s|__ORCHESTRATOR_SA__|${ORCHESTRATOR_SA}|g" | oc apply -f -
apiVersion: batch/v1
kind: Job
metadata:
  name: __ORCHESTRATOR_JOB__
  namespace: __NS__
  labels:
    app: fairness-orchestrator
spec:
  backoffLimit: 0
  activeDeadlineSeconds: 172800
  template:
    spec:
      serviceAccountName: __ORCHESTRATOR_SA__
      restartPolicy: Never
      containers:
        - name: orchestrator
          image: bitnami/kubectl:latest
          command: ["/bin/bash", "-c"]
          args:
            - |
              set -euo pipefail

              NS="__NS__"
              GUIDE_NAME="__GUIDE_NAME__"
              MODEL_DEPLOY="__MODEL_DEPLOY__"
              PVC_NAME="__PVC_NAME__"
              RUN_PREFIX="__RUN_PREFIX__"
              INFERENCE_PERF_IMAGE="__INFERENCE_PERF_IMAGE__"
              INFERENCE_PERF_CONFIGMAP="__INFERENCE_PERF_CONFIGMAP__"
              SCRAPER_CONFIGMAP="__SCRAPER_CONFIGMAP__"
              EPP_METRICS_URL="__EPP_METRICS_URL__"
              EPP_CM="${GUIDE_NAME}-epp"
              EPP_CM_KEY="experiment-plugins.yaml"

              STRATEGIES="__STRATEGIES__"

              # --- Plugin configs ---
              read -r -d '' CONFIG_LAS << 'YAMLEOF' || true
              apiVersion: inference.networking.x-k8s.io/v1alpha1
              kind: EndpointPickerConfig
              plugins:
              - type: agent-identity
              - type: queue-scorer
              - type: kv-cache-utilization-scorer
              - type: prefix-cache-scorer
              - type: program-aware-fairness
              featureGates:
              - flowControl
              flowControl:
                defaultPriorityBand:
                  fairnessPolicyRef: program-aware-fairness
              schedulingProfiles:
              - name: default
                plugins:
                - pluginRef: queue-scorer
                  weight: 2
                - pluginRef: kv-cache-utilization-scorer
                  weight: 2
                - pluginRef: prefix-cache-scorer
                  weight: 3
              YAMLEOF

              read -r -d '' CONFIG_RR << 'YAMLEOF' || true
              apiVersion: inference.networking.x-k8s.io/v1alpha1
              kind: EndpointPickerConfig
              plugins:
              - type: agent-identity
              - type: queue-scorer
              - type: kv-cache-utilization-scorer
              - type: prefix-cache-scorer
              - type: round-robin-fairness-policy
              featureGates:
              - flowControl
              flowControl:
                defaultPriorityBand:
                  fairnessPolicyRef: round-robin-fairness-policy
              schedulingProfiles:
              - name: default
                plugins:
                - pluginRef: queue-scorer
                  weight: 2
                - pluginRef: kv-cache-utilization-scorer
                  weight: 2
                - pluginRef: prefix-cache-scorer
                  weight: 3
              YAMLEOF

              read -r -d '' CONFIG_NO_FAIRNESS << 'YAMLEOF' || true
              apiVersion: inference.networking.x-k8s.io/v1alpha1
              kind: EndpointPickerConfig
              plugins:
              - type: queue-scorer
              - type: kv-cache-utilization-scorer
              - type: prefix-cache-scorer
              schedulingProfiles:
              - name: default
                plugins:
                - pluginRef: queue-scorer
                  weight: 2
                - pluginRef: kv-cache-utilization-scorer
                  weight: 2
                - pluginRef: prefix-cache-scorer
                  weight: 3
              YAMLEOF

              get_config() {
                case "$1" in
                  las) echo "$CONFIG_LAS" ;;
                  rr) echo "$CONFIG_RR" ;;
                  no-fairness) echo "$CONFIG_NO_FAIRNESS" ;;
                esac
              }

              switch_strategy() {
                local strategy="$1"
                local config
                config=$(get_config "$strategy")

                echo "=== Switching EPP to: $strategy ==="

                local json_config
                json_config=$(echo "$config" | jq -Rs .)
                kubectl -n "$NS" patch cm "$EPP_CM" --type merge \
                  -p "{\"data\":{\"$EPP_CM_KEY\":$json_config}}"

                kubectl -n "$NS" rollout restart deployment/"${GUIDE_NAME}-epp"
                kubectl -n "$NS" rollout status deployment/"${GUIDE_NAME}-epp" --timeout=300s
                echo "EPP ready with: $strategy"
              }

              restart_model() {
                echo "=== Flushing model server ==="
                kubectl -n "$NS" scale deployment/"$MODEL_DEPLOY" --replicas=0
                kubectl -n "$NS" rollout status deployment/"$MODEL_DEPLOY" --timeout=120s
                kubectl -n "$NS" scale deployment/"$MODEL_DEPLOY" --replicas=2

                for i in $(seq 1 30); do
                  if kubectl -n "$NS" rollout status deployment/"$MODEL_DEPLOY" --timeout=60s 2>/dev/null; then
                    echo "Model server ready."
                    return 0
                  fi
                  echo "Attempt $i/30 — retrying in 30s..."
                  sleep 30
                done
                echo "ERROR: Model server not ready"
                exit 1
              }

              launch_and_wait() {
                local strategy="$1"
                local job_name="inference-perf-${RUN_PREFIX}-${strategy}"

                if kubectl -n "$NS" get job "$job_name" -o jsonpath='{.status.conditions[?(@.type=="Complete")].status}' 2>/dev/null | grep -q "True"; then
                  echo "=== SKIP: $strategy already completed ==="
                  return 0
                fi

                kubectl -n "$NS" delete job "$job_name" --ignore-not-found

                echo "=== Launching: $strategy ==="
                cat <<EOF | kubectl apply -f -
              apiVersion: batch/v1
              kind: Job
              metadata:
                name: ${job_name}
                namespace: ${NS}
                labels:
                  app: inference-perf
                  strategy: ${strategy}
              spec:
                backoffLimit: 0
                activeDeadlineSeconds: 43200
                template:
                  metadata:
                    labels:
                      app: inference-perf
                      job-name: ${job_name}
                  spec:
                    restartPolicy: Never
                    shareProcessNamespace: true
                    containers:
                      - name: inference-perf
                        image: ${INFERENCE_PERF_IMAGE}
                        imagePullPolicy: Always
                        command: ["/bin/sh", "-c"]
                        args:
                          - |
                            ulimit -n 65536 2>/dev/null || ulimit -n \$(ulimit -Hn) 2>/dev/null || true
                            /workspace/.venv/bin/inference-perf --config_file /etc/config/config.yml
                            EXIT_CODE=\$?
                            touch /data/shared/done
                            exit \$EXIT_CODE
                        env:
                          - name: PYTHONUNBUFFERED
                            value: "1"
                          - name: HF_TOKEN
                            valueFrom:
                              secretKeyRef:
                                name: hf-secret
                                key: hf_api_token
                                optional: true
                        resources:
                          requests: {cpu: "8", memory: "128Gi"}
                          limits: {cpu: "16", memory: "256Gi"}
                        volumeMounts:
                          - name: config-volume
                            mountPath: /etc/config
                            readOnly: true
                          - name: results-volume
                            mountPath: /data/reports
                            subPath: ${strategy}/reports
                          - name: shared
                            mountPath: /data/shared
                          - name: dshm
                            mountPath: /dev/shm
                      - name: epp-scraper
                        image: python:3.12-alpine
                        command: ["/bin/sh", "-c"]
                        args:
                          - |
                            sleep 10
                            python3 /scripts/scrape_metrics.py \
                              --url "${EPP_METRICS_URL}" \
                              --subsystem "program_aware" \
                              --interval 15 \
                              --duration 43200 \
                              --output /data/metrics/metrics.jsonl &
                            SCRAPER_PID=\$!
                            while [ ! -f /data/shared/done ]; do sleep 2; done
                            kill \$SCRAPER_PID 2>/dev/null || true
                            wait \$SCRAPER_PID 2>/dev/null || true
                        resources:
                          requests: {cpu: "100m", memory: "512Mi"}
                          limits: {cpu: "500m", memory: "1Gi"}
                        volumeMounts:
                          - name: scraper-script
                            mountPath: /scripts
                            readOnly: true
                          - name: results-volume
                            mountPath: /data/metrics
                            subPath: ${strategy}/metrics
                          - name: shared
                            mountPath: /data/shared
                    volumes:
                      - name: config-volume
                        configMap:
                          name: ${INFERENCE_PERF_CONFIGMAP}
                      - name: scraper-script
                        configMap:
                          name: ${SCRAPER_CONFIGMAP}
                      - name: results-volume
                        persistentVolumeClaim:
                          claimName: ${PVC_NAME}
                      - name: shared
                        emptyDir: {}
                      - name: dshm
                        emptyDir: {medium: Memory, sizeLimit: 2Gi}
              EOF

                echo "Waiting for job: $job_name"
                while true; do
                  if kubectl -n "$NS" get job "$job_name" -o jsonpath='{.status.conditions[?(@.type=="Complete")].status}' 2>/dev/null | grep -q "True"; then
                    echo "=== $strategy COMPLETED ==="
                    return 0
                  fi
                  if kubectl -n "$NS" get job "$job_name" -o jsonpath='{.status.conditions[?(@.type=="Failed")].status}' 2>/dev/null | grep -q "True"; then
                    echo "=== $strategy FAILED ==="
                    return 1
                  fi
                  sleep 30
                done
              }

              # =============================================
              # MAIN LOOP
              # =============================================
              echo "=========================================="
              echo "Orchestrator starting: strategies=$STRATEGIES"
              echo "=========================================="

              for strategy in $STRATEGIES; do
                echo ""
                echo "########################################"
                echo "# Strategy: $strategy"
                echo "########################################"

                switch_strategy "$strategy"
                restart_model
                launch_and_wait "$strategy"

                echo "Done: $strategy"
              done

              echo ""
              echo "=========================================="
              echo "ALL STRATEGIES COMPLETE"
              echo "=========================================="
          resources:
            requests: {cpu: "100m", memory: "128Mi"}
            limits: {cpu: "500m", memory: "256Mi"}
JOBEOF

echo ""
echo "=========================================="
echo "Orchestrator job submitted: $ORCHESTRATOR_JOB"
echo "Strategies: $STRATEGIES"
echo "=========================================="
echo ""
echo "Monitor progress:"
echo "  oc -n $NS logs job/$ORCHESTRATOR_JOB -f"
echo ""
echo "When all strategies complete, download reports:"
echo "  ./experiments/download.sh $RUN_PREFIX"
echo ""
