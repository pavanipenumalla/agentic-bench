# Fairness Scheduling Experiments

Compare scheduling strategies on llm-d:
- **LAS** (Least Attained Service) — program-aware fairness that prioritizes sessions that have received less service
- **Round-Robin** — classic turn-based fairness across programs
- **No Fairness** — baseline with no flow control (pure scheduler scoring)

Each experiment replays 1000+ real agentic LLM traces against a model server, measuring per-session latency, throughput, and fairness.

---

## Directory Layout

```
experiments/
├── README.md                  ← you are here
├── .env.example               ← config template (copy to .env)
├── config.yml                 ← inference-perf config (load shape, timeouts)
├── scrape_metrics.py          ← EPP metrics sidecar (scrapes during runs)
├── run.sh            ← submit experiment to cluster (walk away)
├── download.sh                ← download results from cluster
├── generate-plots.sh          ← regenerate plots locally
├── plots/
│   ├── analyze_reports.py     ← per-strategy latency/throughput plots
│   └── compare_reports.py     ← cross-strategy comparison plots
└── model-server/              ← reference config (applied from llm-d repo)
    ├── kustomization.yaml
    └── patch-vllm.yaml
```

---

## Setup

### 1. Configure environment

```bash
cp experiments/.env.example experiments/.env
# Edit experiments/.env with your namespace, image registry, etc.
```

The `.env` file controls:
| Variable | Description |
|----------|-------------|
| `NAMESPACE` | Kubernetes namespace for all resources |
| `GUIDE_NAME` | Helm release name (derives EPP service name) |
| `MODEL_DEPLOY` | Model server deployment name |
| `INFERENCE_PERF_IMAGE` | inference-perf container image |
| `EPP_IMAGE_REGISTRY` | Registry for EPP image |
| `EPP_IMAGE_REPO` | EPP image repository name |
| `EPP_IMAGE_TAG` | EPP image tag |

### 2. Prerequisites

- OpenShift cluster with GPU nodes
- `oc` CLI authenticated (`oc login ...`)
- `helm` v3+
- Python 3.10+ with `matplotlib`, `numpy` (for local plot generation)

### 3. Cluster setup (one-time)

#### Install Gateway API CRDs

```bash
kubectl get crd | grep inference
# If missing:
kubectl apply -k "https://github.com/kubernetes-sigs/gateway-api-inference-extension/config/crd?ref=v1.5.0"
```

#### Add RBAC for llm-d CRDs

The GAIE Helm chart doesn't grant permissions for llm-d-specific CRDs.
Without this, EPP crash-loops with "failed waiting for InferenceObjective Informer to sync".

```bash
cat <<EOF | oc apply -f -
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: ${GUIDE_NAME}-epp-llmd-extras
rules:
- apiGroups: ["llm-d.ai"]
  resources: ["inferenceobjectives"]
  verbs: ["get", "list", "watch"]
- apiGroups: ["inference.networking.x-k8s.io"]
  resources: ["inferencemodelrewrites"]
  verbs: ["get", "list", "watch"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: ${GUIDE_NAME}-epp-llmd-extras
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: ${GUIDE_NAME}-epp-llmd-extras
subjects:
- kind: ServiceAccount
  name: ${GUIDE_NAME}-epp
  namespace: ${NAMESPACE}
EOF
```

#### Deploy the EPP (router) via Helm

```bash
cd /path/to/llm-d

cat > /tmp/epp-image-override.yaml << 'EOF'
inferenceExtension:
  image:
    registry: ${EPP_IMAGE_REGISTRY}
    repository: ${EPP_IMAGE_REPO}
    tag: ${EPP_IMAGE_TAG}
    pullPolicy: Always
  extraServicePorts:
    - name: http
      port: 80
      protocol: TCP
      targetPort: 8081
    - name: metrics
      port: 9090
      protocol: TCP
      targetPort: 9090
EOF

helm install ${GUIDE_NAME} \
    oci://registry.k8s.io/gateway-api-inference-extension/charts/standalone \
    -f guides/agentic-serving/router/agentic-workloads.values.yaml \
    -f /tmp/epp-image-override.yaml \
    -n ${NAMESPACE} --version v1.5.0

oc rollout status deployment/${GUIDE_NAME}-epp -n ${NAMESPACE}
```

#### Deploy the model server

The model server config is in `model-server/` for reference. To deploy from the llm-d repo:

```bash
cd /path/to/llm-d
oc apply -n ${NAMESPACE} -k guides/agentic-serving/modelserver/gpu/vllm/
oc -n ${NAMESPACE} rollout status deployment/${MODEL_DEPLOY} --timeout=3600s
```

**Model server specs** (see `model-server/patch-vllm.yaml`):
| Setting | Value |
|---------|-------|
| Model | `Qwen/Qwen3.6-35B-A3B-FP8` |
| GPUs | 2 (tensor-parallel-size=2) |
| Max context | 200k tokens |
| Max concurrent sequences | 64 |
| KV cache dtype | FP8 |
| GPU memory utilization | 80% |

#### Verify everything works

```bash
oc get pods -n ${NAMESPACE}
oc logs deploy/${GUIDE_NAME}-epp -n ${NAMESPACE} | head -20

# Quick inference test
oc port-forward svc/${GUIDE_NAME}-epp 8080:80 -n ${NAMESPACE} &
curl -s http://localhost:8080/v1/chat/completions \
    -H 'Content-Type: application/json' \
    -d '{"model":"Qwen/Qwen3.6-35B-A3B-FP8","messages":[{"role":"user","content":"Hello"}],"max_tokens":20}' | jq
kill %1
```

---

## Running Experiments

All commands below assume you're in the **inference-perf repo root**.

### Step 1: Submit the experiment

```bash
./experiments/run.sh guide-run-1

# Monitor (optional):
oc -n ${NAMESPACE} logs job/orchestrator-guide-run-1 -f
```

The orchestrator runs entirely on-cluster: for each strategy it patches EPP, restarts the model server (to flush KV cache), launches the inference-perf job, and waits for completion. You only need your laptop to submit and later download.

By default it runs all three strategies (las, rr, no-fairness). To run a subset:
```bash
./experiments/run.sh guide-run-1 "rr no-fairness"
```

### Step 2: Download results

```bash
./experiments/download.sh guide-run-1
```

Results land in `guide-run-1/`:
```
guide-run-1/
├── las/reports/                         # raw session reports
├── rr/reports/
├── no-fairness/reports/
├── las/metrics/metrics.jsonl            # EPP metrics time-series
├── comparison/                          # cross-strategy plots (all sessions)
└── comparison-matched/                  # cross-strategy plots (matched only)
```

### Step 3: Regenerate plots (optional)

If you want to re-generate plots without re-downloading:

```bash
./experiments/generate-plots.sh guide-run-1
```

Or run individual scripts directly:

```bash
# Per-strategy
python3 experiments/plots/analyze_reports.py \
    guide-run-1/las/reports/ \
    -o guide-run-1/las/

# Cross-strategy comparison
python3 experiments/plots/compare_reports.py \
    --las guide-run-1/las/reports \
    --rr guide-run-1/rr/reports \
    --no-fairness guide-run-1/no-fairness/reports \
    -o guide-run-1/comparison/

# Matched-only (programs that succeeded in ALL strategies)
python3 experiments/plots/compare_reports.py \
    --las guide-run-1/las/reports \
    --rr guide-run-1/rr/reports \
    --matched-only \
    -o guide-run-1/comparison-matched/
```

---

## Experiment Config

The load shape is defined in `config.yml`:

| Parameter | Value | Meaning |
|-----------|-------|---------|
| `concurrent_sessions` | 600 | Max sessions running at once |
| `num_sessions` | 1000 | Total sessions to replay |
| `session_rate` | 10 | New sessions launched per second |
| `request_timeout` | 900s | Per-request timeout |
| `num_workers` | 8 | Parallel async workers |

The dataset is `Exgentic/agent-llm-traces` from HuggingFace — real multi-turn agentic conversations.

---

## Cleanup

```bash
# Remove experiment jobs + PVC
oc -n ${NAMESPACE} delete job -l app=inference-perf
oc -n ${NAMESPACE} delete job -l app=fairness-orchestrator
oc -n ${NAMESPACE} delete pvc inference-perf-results-guide-run-1

# Tear down the entire deployment
helm uninstall ${GUIDE_NAME} -n ${NAMESPACE}
oc delete -n ${NAMESPACE} -k /path/to/llm-d/guides/agentic-serving/modelserver/gpu/vllm/
```
