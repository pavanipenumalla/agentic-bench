# Benchmarking Agentic Inference with OTel Trace Replay on Kubernetes

> How to evaluate scheduling strategies for agentic LLM workloads using real production traces.

---

## The Problem: Agentic Workloads Break Traditional Benchmarks

Standard inference benchmarks fire independent requests at a fixed rate — constant throughput, Poisson arrivals, or ramp-up patterns. This works for chatbots and single-turn completions, but it fails for agentic applications.

An agentic workflow is not a stream of independent requests. It is a **dependency graph**: multiple LLM calls connected by causal relationships. Call B cannot start until Call A finishes, because B's prompt includes A's output. Some calls fan out in parallel (multiple tool calls), then fan back in (a synthesis step that needs all results). The graph has structure, and that structure determines how the workload hits the serving layer.

When a scheduling strategy claims to provide "fairness across sessions," evaluating it with independent requests tells nothing useful. The strategy never sees concurrent multi-step sessions competing for resources — which is the exact scenario it was designed for.

---

## What OTel Trace Replay Solves

[inference-perf](https://github.com/llm-d/inference-perf) includes an OTel trace replay feature that bridges this gap. Given a set of OpenTelemetry traces recorded from real agentic applications, it:

1. **Parses the traces** — Extracts LLM call spans with their inputs, outputs, timing, and model parameters
2. **Infers the dependency graph** — Detects causal edges (when one call's output appears in another's input) and temporal edges (ordering by time when causality isn't detectable)
3. **Replays with correct semantics** — Respects dependencies (B waits for A), substitutes actual generated output into downstream prompts, maintains session identity headers
4. **Reports session-level metrics** — Not just per-request latency, but end-to-end session duration, success/failure, and per-session token counts

The result is a benchmark that exercises the serving layer the way a real agentic application would — with concurrent sessions, dependency-driven request ordering, and realistic concurrency patterns.

---

## The Evaluation: Program-Aware Scheduling

The specific evaluation that motivated this setup compares scheduling strategies for [llm-d](https://github.com/llm-d), an inference serving platform built on Kubernetes:

- **Program-aware LAS (Least Attained Service)** — Tracks cumulative service per session. Sessions that have consumed fewer tokens get priority. This prevents long-running agentic workflows from being starved by short ones, and prevents chatty agents from monopolizing the serving layer. Two variants are tested with different decay functions.

- **Round-Robin (RR)** — Simple rotation. No service tracking. Baseline for comparison.

The [program-aware-las implementation](https://github.com/praveingk/llm-d-inference-scheduler/tree/program-aware-las) integrates into llm-d's EPP (Endpoint Picker Plugin) as a configurable fairness policy.

The evaluation setup itself is general-purpose. Any comparison that involves running the same workload against different serving configurations can reuse this framework.

---

## How the Evaluation Works

The entire evaluation runs on a Kubernetes cluster — no local GPU, no VPN dependency during the run. Here's the high-level flow:

```
┌─────────────────────────────────────────────────────────────────────┐
│                        Kubernetes Cluster                            │
│                                                                     │
│  ┌──────────────────┐         ┌─────────────────────────────────┐  │
│  │  inference-perf   │────────▶│  llm-d                           │  │
│  │  (K8s Job)        │         │  EPP with scheduling strategy    │  │
│  │                   │         │  vLLM model server                │  │
│  │  Replays OTel     │         └─────────────────────────────────┘  │
│  │  traces with      │                                              │
│  │  dependency       │         ┌─────────────────────────────────┐  │
│  │  graphs           │────────▶│  PVC                              │  │
│  └──────────────────┘         │  Reports persist here             │  │
│         ▲                      └─────────────────────────────────┘  │
│         │                                                           │
└─────────┼───────────────────────────────────────────────────────────┘
          │
  ┌───────┴────────┐
  │ OTel Traces     │
  │ (HuggingFace)   │
  └────────────────┘
```

The workflow for a multi-strategy comparison:

1. **Load traces** from a [HuggingFace dataset](https://huggingface.co/datasets/Exgentic/agent-llm-traces) containing real agentic OTel traces
2. **For each scheduling strategy:**
   - Switch the EPP configuration to the target strategy
   - Restart the model server (clean KV cache for a fair baseline)
   - Launch inference-perf as a Kubernetes Job that replays traces against the serving endpoint
   - Wait for completion — reports are written to persistent storage on the cluster
3. **Download results** and run comparison analysis (CDFs, percentile tables, bar charts)

The key design choices that make this practical:

- **On-cluster Jobs** eliminate laptop-as-a-dependency. Sleep, VPN drops, token expiry — none of these kill the experiment. Re-run the script to pick up where it left off.
- **Automated strategy cycling** means launching one command and walking away. Hours later, all strategies have been evaluated under identical conditions.
- **Environment-based configuration** keeps cluster-specific details out of the scripts. The repo is shareable as-is — just fill in `env.sh`.

---

## What the Evaluation Measures

The output isn't just per-request latency numbers. For fairness evaluation of agentic workloads, per-session metrics are what matters:

- **Session duration distribution (CDF)** — How completion times are spread across concurrent sessions. A fair scheduler produces tighter distributions; an unfair one creates long-tail outliers where some sessions are starved.
- **Session success rate** — Percentage of agentic workflows that complete all LLM calls without timeout or error. Starvation manifests as failed sessions, not just slow ones.
- **Per-session scatter plots** — Duration vs. start time, colored by session complexity (number of LLM calls). Reveals whether complex sessions are disproportionately penalized.
- **Percentile tables** — p50/p90/p99 of session duration per strategy, grouped by session complexity. Quick comparison across strategies.

These metrics directly answer the question: *does this scheduling strategy provide fair service to concurrent agentic sessions, regardless of their complexity?*

---

## Results

> Results from the current experiment run will be added here once complete.
>
> Expected comparison across LAS-halflife, LAS-default, and Round-Robin:
> - Session duration CDF overlay
> - Success rate bar chart
> - Percentile breakdown by session complexity quartile
> - Fairness index (variance of normalized session durations)

---

## Try It Yourself

The full evaluation setup — configs, scripts, and analysis tooling — is available as a companion repository:

**[github.com/\<org\>/otel-evaluation-guide](https://github.com/<org>/otel-evaluation-guide)**

The repo includes:
- Ready-to-use inference-perf configuration for OTel trace replay
- Scheduling strategy configs (LAS variants + Round-Robin)
- Automation scripts that handle the full lifecycle (patch → restart → run → download → compare)
- Analysis scripts that produce the comparison plots and tables shown above

The [README](https://github.com/<org>/otel-evaluation-guide) provides a detailed step-by-step guide covering everything from building the image to interpreting results. It's designed to be followed linearly — each step builds on the previous one with full context on what's happening and why.

The setup is adaptable beyond scheduling strategies. Any evaluation that involves running the same agentic workload against different serving configurations — models, batch sizes, routing algorithms, quantization — can reuse this framework by swapping the strategy configs for whatever parameters vary between runs.

---

## Links

- [inference-perf](https://github.com/llm-d/inference-perf) — Benchmarking tool with OTel trace replay
- [llm-d](https://github.com/llm-d) — Inference serving platform
- [Program-aware LAS](https://github.com/praveingk/llm-d-inference-scheduler/tree/program-aware-las) — Scheduling strategy under evaluation
- [Exgentic/agent-llm-traces](https://huggingface.co/datasets/Exgentic/agent-llm-traces) — OTel trace dataset
- [OpenTelemetry GenAI Semantic Conventions](https://opentelemetry.io/docs/specs/semconv/gen-ai/) — Trace format specification
