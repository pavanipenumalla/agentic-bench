# Agentic Trace Replay — On-Cluster Evaluation Setup

Evaluate scheduling strategies for agentic inference workloads on Kubernetes. Uses [inference-perf](https://github.com/llm-d/inference-perf)'s OTel trace replay to benchmark [llm-d](https://github.com/llm-d) with real agentic workload patterns.

**For the motivation and context behind this setup, read the [blog post](blog.md).**

---

## Overview

This repo provides everything needed to:

1. Replay real agentic OTel traces against an llm-d deployment on a Kubernetes cluster
2. Automatically cycle through different [scheduling strategies](#understanding-the-strategy-configs) (LAS, Round-Robin)
3. Collect per-session metrics and produce comparison plots

The evaluation runs entirely on-cluster as Kubernetes Jobs. The laptop only sends commands — it can sleep or disconnect without affecting the experiment.

### What's in this repo

```
.
├── env.example.sh                   ← Configuration template (all cluster settings)
├── configs/
│   ├── inference-perf.yml           ← How inference-perf replays traces
│   ├── strategy-las-halflife.yaml   ← LAS scheduling (half-life decay variant)
│   ├── strategy-las-default.yaml    ← LAS scheduling (exponential decay variant)
│   └── strategy-rr.yaml            ← Round-robin scheduling (baseline)
├── manifests/
│   ├── pvc.yaml                     ← PersistentVolumeClaim for storing results
│   ├── job.yaml                     ← inference-perf Kubernetes Job
│   └── helper-pod.yaml              ← Temporary pod for downloading results from PVC
├── run/
│   ├── run.sh                       ← Run one experiment for one strategy
│   └── run-strategies.sh            ← Run all strategies and compare
├── analysis/
│   ├── analyze_reports.py           ← Plots for a single strategy's results
│   └── compare_reports.py           ← Cross-strategy comparison (CDFs, tables)
└── results/                         ← Where downloaded results land (gitignored)
```

---

## Prerequisites

Before starting, ensure the following are in place:

| Requirement | Why it's needed |
|-------------|-----------------|
| Kubernetes or OpenShift cluster | Where the model, EPP, and benchmark Job run |
| `oc` or `kubectl` CLI, authenticated | Scripts use this to manage resources on the cluster |
| Container registry (e.g., `quay.io`) | inference-perf image must be pullable from the cluster |
| `podman` or `docker` | To build the inference-perf container image |
| llm-d deployed on the cluster | The serving layer being benchmarked (model server + EPP) |
| Python 3.9+ with matplotlib, numpy | For running analysis scripts locally after downloading results |

### Install analysis dependencies

```bash
pip install -r requirements.txt
```

---

## Step 1: Deploy llm-d on the Cluster

This evaluation targets an llm-d deployment as the serving layer. If llm-d is not yet deployed, follow the [official llm-d quickstart](https://github.com/llm-d/llm-d/blob/main/docs/quickstart.md).

For scheduling strategy evaluation specifically, the following adjustments are needed on top of the standard deployment:

**Use the program-aware EPP image.** The standard EPP does not include the LAS fairness policy. Clone and build from the [program-aware-las branch](https://github.com/praveingk/llm-d-inference-scheduler/tree/program-aware-las):

```bash
git clone https://github.com/praveingk/llm-d-inference-scheduler.git
cd llm-d-inference-scheduler
git checkout program-aware-las

podman build -t quay.io/<your-org>/llm-d-router-endpoint-picker:program-aware-las .
podman push quay.io/<your-org>/llm-d-router-endpoint-picker:program-aware-las
```

Then update the EPP deployment to use this image (or set `K8S_EPP_IMAGE` in `env.sh` and the scripts handle it automatically).

**Note the cluster-internal service URL.** This is how inference-perf reaches the EPP from inside the cluster. It looks like:

```
http://<epp-service-name>.<namespace>.svc.cluster.local
```

Find it with:
```bash
oc -n <namespace> get svc | grep epp
```

**Ensure the model is loaded and ready.** The model server should be serving the target model (e.g., `Qwen/Qwen3.6-35B-A3B-FP8`) and responding to requests before running the benchmark.

---

## Step 2: Build and Push the inference-perf Image

inference-perf runs inside the cluster as a Kubernetes Job. It needs a container image:

```bash
# Clone inference-perf
git clone https://github.com/llm-d/inference-perf.git
cd inference-perf

# Build — uses a multi-stage Dockerfile (Alpine 3.22 + Python 3.12)
podman build -t quay.io/<your-org>/inference-perf:latest .

# Push to a registry the cluster can pull from
podman push quay.io/<your-org>/inference-perf:latest
```

This step only needs to be repeated if the inference-perf code changes.

---

## Step 3: Configure the Environment

All cluster-specific settings are centralized in a single file. This keeps the scripts, configs, and analysis code portable — shareable without leaking cluster details.

```bash
cp env.example.sh env.sh
```

Edit `env.sh` with your cluster values:

```bash
# Required — the scripts won't work without these
K8S_NAMESPACE="your-namespace"
K8S_EPP_DEPLOYMENT="your-epp-deployment-name"
K8S_MODEL_DEPLOYMENT="your-model-deployment-name"
K8S_EPP_CONFIGMAP="your-epp-configmap-name"
K8S_EPP_CONFIGMAP_KEY="your-configmap-key"
INFERENCE_PERF_IMAGE="quay.io/<your-org>/inference-perf:latest"

# Required for strategy evaluation — the custom EPP image
K8S_EPP_IMAGE="quay.io/<your-org>/llm-d-router-endpoint-picker:program-aware-las"

# Optional — defaults are usually fine
K8S_CLI="oc"                    # or "kubectl"
INFERENCE_PERF_CONFIGMAP="inference-perf-config"
JOB_DEADLINE_SECONDS="43200"    # 12 hours max per job
```

**How to find these values:**

```bash
# Namespace
oc projects  # or: kubectl get namespaces

# EPP deployment name
oc -n <namespace> get deployments | grep epp

# Model deployment name
oc -n <namespace> get deployments | grep -i vllm  # or grep for model name

# EPP configmap name and key
oc -n <namespace> get configmaps | grep epp
oc -n <namespace> get cm <configmap-name> -o yaml  # inspect the keys
```

---

## Step 4: Configure inference-perf

The file `configs/inference-perf.yml` tells inference-perf what traces to replay, how many sessions to run, and where to send requests. Edit it to match your deployment:

**What to change:**

| Field | What to set it to |
|-------|-------------------|
| `static_model_name` | The model deployed on your cluster (e.g., `Qwen/Qwen3.6-35B-A3B-FP8`) |
| `server.model_name` | Same as above |
| `server.base_url` | The cluster-internal EPP service URL from Step 1 |
| `concurrent_sessions` | How many sessions to run in parallel (start small: 5-10) |
| `num_sessions` | Total sessions to execute (start small: 5-10 for testing) |

**What to leave as-is (unless you know why you'd change it):**

| Field | Default | Why |
|-------|---------|-----|
| `session_id_header_key` | `x-gateway-inference-fairness-id` | The EPP uses this header to identify sessions. Changing it breaks fairness tracking. |
| `worker_max_concurrency` | `100` | Must be high. All events in a session are enqueued immediately — events waiting for predecessors hold concurrency slots (via asyncio, zero threads, negligible cost). Set to at least `concurrent_sessions × avg_events_per_session`. Since the cost is near-zero, erring high is safe. |
| `storage.local_storage.path` | `/data/reports` | Must match the PVC mount point in the Job spec. The scripts assume this path. |
| `hf_dataset_path` | `Exgentic/agent-llm-traces` | Public dataset of real agentic traces. Change only if using your own traces. |

See the [annotated config file](configs/inference-perf.yml) for explanations of every field.

---

## Step 5: Upload Configuration to the Cluster

The inference-perf Job reads its config from a Kubernetes ConfigMap. Upload it:

```bash
oc -n <namespace> create configmap inference-perf-config \
  --from-file=config.yml=configs/inference-perf.yml \
  --dry-run=client -o yaml | oc apply -f -
```

If the dataset requires authentication (private HuggingFace dataset), also create a secret:

```bash
oc -n <namespace> create secret generic hf-secret \
  --from-literal=hf_api_token=hf_XXXXXXXX \
  --dry-run=client -o yaml | oc apply -f -
```

Skip the secret if using the public `Exgentic/agent-llm-traces` dataset.

---

## Step 6: Run the Evaluation

### Option A: Run all strategies (recommended)

This is the typical workflow — evaluate all strategies and produce a comparison:

```bash
cd run && ./run-strategies.sh my-experiment
```

What happens:

1. The script finds all `strategy-*.yaml` files in `configs/` (las-halflife, las-default, rr)
2. For each strategy:
   - Patches the EPP configmap to activate that strategy's scheduling policy
   - Restarts the model server (scale 0→1) to clear KV cache — ensures a clean baseline
   - Restarts the EPP deployment to pick up the new config
   - Launches an inference-perf Job that replays traces and writes results to a PVC
   - Waits for the Job to complete
3. After all strategies finish, downloads results from the PVC to `results/my-experiment/`
4. Runs the comparison analysis script to produce plots

**This takes hours** (depends on session count and model speed). Each individual Job runs independently on the cluster, but the script needs an active `oc`/`kubectl` connection between strategies to patch the configmap and restart deployments. If the connection drops mid-run, re-authenticate and re-run — completed strategies are automatically skipped.

### Option B: Run a single strategy

For testing or re-running one strategy:

```bash
cd run && ./run.sh my-experiment-las las-halflife
```

### Resilience

| Scenario | What happens |
|----------|--------------|
| Laptop sleeps / closes | Jobs keep running on cluster. Re-run the script to download results. |
| `oc` token expires mid-wait | Re-login (`oc login ...`), re-run the script. Completed strategies are skipped. |
| Job fails | Check logs: `oc -n <ns> logs job/<name>`. Delete failed job, re-run. |
| Want to re-download results | Re-run the same command. It skips jobs, only downloads. |

---

## Step 7: Monitor While It's Running

While experiments are in progress:

```bash
# See which pods are running
oc -n <namespace> get pods -l app=inference-perf

# Stream live logs from a specific strategy's job
oc -n <namespace> logs -f job/inference-perf-my-experiment-las-halflife

# Check job status (Complete / Failed / Running)
oc -n <namespace> get jobs -l app=inference-perf

# Quick status of all jobs
oc -n <namespace> get jobs -l app=inference-perf -o custom-columns=NAME:.metadata.name,STATUS:.status.conditions[0].type
```

---

## Step 8: Analyze Results

After download, results land in:

```
results/my-experiment/
├── las-halflife/reports/
│   ├── summary_session_lifecycle_metrics.json   ← aggregate stats
│   ├── per_session_lifecycle_metrics.json       ← one entry per session
│   └── summary_request_lifecycle_metrics.json   ← per-request aggregates
├── las-default/reports/
│   └── ...
├── rr/reports/
│   └── ...
└── comparison/                                  ← auto-generated by run-strategies.sh
    ├── session_duration_cdf.png
    ├── success_rate_comparison.png
    ├── avg_session_duration.png
    └── percentile_table.txt
```

`run-strategies.sh` runs the comparison automatically. To re-run analysis manually or with different options:

```bash
# Single strategy — scatter plot of session duration vs. start time
python3 analysis/analyze_reports.py results/my-experiment/las-halflife/reports \
    -o results/my-experiment/las-halflife/

# Cross-strategy comparison — CDFs, bar charts, percentile table
python3 analysis/compare_reports.py \
    --las-halflife results/my-experiment/las-halflife/reports \
    --las-default results/my-experiment/las-default/reports \
    --rr results/my-experiment/rr/reports \
    -o results/my-experiment/comparison/
```

### Understanding the output

| Metric | What it tells you | What to look for |
|--------|-------------------|------------------|
| Session duration CDF | Distribution of end-to-end session times | Tighter = fairer. Long tail = some sessions starved. |
| Success rate | % of sessions completing all LLM calls | Low rate under load = starvation or timeouts |
| Avg session duration | Mean completion time per strategy | Lower is better, but fairness matters more than raw speed |
| Percentile table | p50/p90/p99 breakdown | Large gap between p50 and p99 = unfair tail behavior |

---

## Understanding the Strategy Configs

As described in the [evaluation overview](#overview), this setup compares different scheduling strategies under the same agentic workload. Each file in `configs/strategy-*.yaml` defines a scheduling policy that the EPP uses to decide request ordering. The scripts read these files and patch the EPP configmap accordingly — this is how the automated cycling in Step 6 works.

### LAS (Least Attained Service)

LAS tracks how much "service" (tokens generated) each session has received. Sessions with less cumulative service get priority. This ensures that sessions which just started aren't starved by sessions that have been consuming resources for minutes.

**`strategy-las-halflife.yaml`** — Service history decays with a time-based half-life (100 seconds). Recent activity counts more than older activity. A session that was busy 2 minutes ago is penalized less than one busy right now.

**`strategy-las-default.yaml`** — Service history decays with a per-tick multiplicative factor (0.99997). More gradual forgetting. Used to compare decay approaches.

Both variants use the same weighting: 80% weight on cumulative service, 20% on head-of-line wait time.

### Round-Robin

**`strategy-rr.yaml`** — Simple rotation across sessions. No service tracking, no priority. Serves as the baseline: if LAS doesn't beat round-robin on fairness metrics, it's not providing value.

---

## Adapting for Different Evaluations

### Different model

1. Deploy the new model on llm-d
2. Update `static_model_name` and `server.model_name` in `configs/inference-perf.yml`
3. Update `server.base_url` if the service name changed
4. Re-upload the configmap (Step 5)

### Different traces

Replace the trace source in `configs/inference-perf.yml`:

```yaml
# From a different HuggingFace dataset:
hf_dataset_path: "your-org/your-dataset"

# Or from local files (must be mounted into the Job — requires Job spec change):
trace_directory: "/data/traces/"
```

Traces must follow [OpenTelemetry Semantic Conventions for GenAI](https://opentelemetry.io/docs/specs/semconv/gen-ai/) — each file is one session with LLM call spans containing `gen_ai.input.messages` and `gen_ai.output.text`.

### Different scale

Adjust in `configs/inference-perf.yml`:

```yaml
stages:
  # Conservative (testing)
  - concurrent_sessions: 5
    num_sessions: 10

  # Production evaluation
  - concurrent_sessions: 20
    num_sessions: 500

  # Stress test (unlimited concurrency)
  - concurrent_sessions: 0
    num_sessions: 100
```

Multiple stages run sequentially. Remember to increase `worker_max_concurrency` proportionally (50-100x the highest `concurrent_sessions` value).

### Evaluating something other than scheduling

The framework works for any A/B comparison where:
- A cluster configuration changes between runs
- The same workload is applied to each variant
- Results are compared after all variants complete

Replace the strategy configs with whatever parameter varies (batch size, model replicas, quantization level). Update the `patch_configmap` function in `run-strategies.sh` to target the appropriate configmap/deployment.

---

## Troubleshooting

| Problem | Likely cause | Fix |
|---------|-------------|-----|
| `env.sh not found` | Forgot to copy template | `cp env.example.sh env.sh` and fill in values |
| `Not authenticated` | Token expired | `oc login --token=<new-token> --server=<api>` |
| Image pull error | Registry not accessible from cluster | Check: `oc get events \| grep pull` |
| Job stuck in Pending | Insufficient resources | Check: `oc describe pod <pod>` for resource errors |
| Model not ready after 30 retries | GPU node issues | Check: `oc -n <ns> describe pod <model-pod>` |
| `ClientConnectorDNSError` in logs | Wrong `base_url` | Must be `svc.cluster.local` format, not external route |
| Low success rate | Request timeout too short | Increase `request_timeout` in inference-perf config |
| All sessions fail immediately | Model not loaded | Verify: `oc -n <ns> logs <model-pod>` shows model ready |

---

## Cleanup

After experiments are complete and results are downloaded:

```bash
# Delete jobs
oc -n <namespace> delete jobs -l app=inference-perf

# Delete PVC (only after downloading all results you need)
oc -n <namespace> delete pvc inference-perf-results-<experiment-name>

# Delete configmap (if no longer needed)
oc -n <namespace> delete configmap inference-perf-config

# Delete HF secret (if created)
oc -n <namespace> delete secret hf-secret
```

---

## Related Projects

- [inference-perf](https://github.com/llm-d/inference-perf) — The benchmarking tool (OTel trace replay is one of its load types)
- [llm-d](https://github.com/llm-d) — Inference serving platform
- [Program-aware LAS](https://github.com/praveingk/llm-d-inference-scheduler/tree/program-aware-las) — The scheduling strategy being evaluated
- [Exgentic/agent-llm-traces](https://huggingface.co/datasets/Exgentic/agent-llm-traces) — Public dataset of real agentic OTel traces
