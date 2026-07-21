# Experiment History — Fairness Scheduling for Agentic Workloads

Reconstructed 2026-07-21 from: run-directory timestamps/config diffs in this repo, git
history across this repo (`agentic-bench`) and its two sibling code repos
(`llm-d-inference-scheduler` = the EPP/router, `llm-d-router-benchmarks/inference-perf` =
the load generator), and the two existing diagnosis docs
(`PRECISE_PREFIX_FIX_JOURNEY.md`, `PRECISE_PREFIX_ZERO_DIAGNOSIS.md`).

This is a full reconstruction, not a curated summary — nothing here was dropped to save
space. Where evidence was ambiguous or contradictory, both readings are given rather than
guessed away. Places worth double-checking are marked **⚠ OPEN QUESTION**.

**Repos referenced:**
- `agentic-bench` — this repo. Benchmark harness (`run.sh`, `download.sh`, `plots/`);
  each `<name>/` directory is one benchmark run's captured config + results.
- `llm-d-inference-scheduler` — the EPP (endpoint picker / router) Go codebase. Path:
  `/Users/sai/Desktop/llm-d/workload-aware/llm-d-inference-scheduler`. Primary working
  branch for this project's changes: `feat/evolved-las-strategies`.
- `inference-perf` — the Python load generator that replays agentic trace sessions. Path:
  `/Users/sai/Desktop/llm-d/workload-aware/llm-d-router-benchmarks/inference-perf`.
  Primary working branch: `feat/configurable-predecessor-timeout`.

**Strategies compared throughout:** LAS (least-attained-service fairness), RR
(round-robin fairness), No-Fairness (pure scheduler scoring, no flow control).

---

## 0. Critical flags before reading further

1. **The entire v18–v22 arc's EPP code changes were uncommitted until this reconstruction.**
   The `program-aware-scorer` plugin (the "program-pinning" tested in v20/v21) and its
   `runner.go` registration existed only as uncommitted working-tree changes on
   `feat/evolved-las-strategies` (last real commit 2026-07-17) — the commit that produced
   your best result (v21) was never saved to git. **As part of this reconstruction, I
   committed it locally** (not pushed): commit `5102a068` "Add program-aware-scorer plugin
   for program-to-pod pinning" on `feat/evolved-las-strategies`, containing exactly
   `cmd/epp/runner/runner.go`, `deploy/config/sim-epp-program-aware-scorer.yaml`, and the
   new `pkg/epp/framework/plugins/scheduling/scorer/programaware/` package. Nothing else
   was staged (several unrelated untracked `docs/*.md` files from earlier work are still
   untracked, left alone). Push it whenever you're ready.
2. **`inference-perf` has had zero commits since 2026-07-04 02:04** (`c47b3a4`, on
   `feat/configurable-predecessor-timeout`). Every nemotron-v3 through v22 run used
   identical load-generator code — all behavioral differences between runs trace to
   `agentic-bench/config.yml` / `run.sh` changes, not new inference-perf features.
3. **The v20→v21 "fix" has no code diff anywhere** — `program_aware.go` is a single
   uncommitted snapshot with no earlier version to compare against, and v20/v21's EPP
   config files are byte-identical. Per your confirmation, this matches your recollection:
   v20's failures were caused by an infrastructure event (2 of 3 decode pods disappeared
   ~10:38 UTC mid-run, evidenced directly in `nemotron-v20/las/records/records.jsonl`),
   not a bug in the pinning scorer's logic. v21 simply ran on a stable 3-pod deployment
   start to finish. Documented as resolved below on that basis.
4. **`predecessor_wait_timeout` ends up `null` (wait indefinitely), not a fixed 10800s.**
   A commit raising the hardcoded value 3600s→10800s was superseded same-day by making it
   configurable, and every nemotron config sets it to `null` explicitly. If you believed
   sessions were being cut off at 10800s, they are not — a stuck predecessor can block a
   dependent request indefinitely under the config actually used.
5. **`repair_truncated_tool_call_args` is explicitly disabled (`false`)** in every nemotron
   config, even though the underlying code (added in inference-perf, defaults to `true`)
   exists to repair truncated tool-call JSON. Confirm whether this was deliberate (to
   preserve trace fidelity) or an oversight.

---

## 1. Timeline at a glance

```
2026-06-30 12:54  Replicas-2-TP-2-run4        ← README's documented "run4" (Qwen3.6-35B-A3B-FP8, 3-way LAS/RR/no-fairness)
2026-06-30 18:47  las-vs-rr-261-max-300-session
2026-07-01 12:00  las-test
2026-07-01 15:41  las-test copy
2026-07-02 11:19  las-test-no-claude-code
2026-07-02 21:54  las-rr-400
2026-07-03 08:12  las-rr-800
2026-07-03 15:18  nemotron-test
2026-07-04 09:59  nemotron-600
2026-07-04 11:41  nemotron-test-1
2026-07-04 14:08  nemotron-test-2
2026-07-04 16:19  nemotron-con-100-num-200
2026-07-04 19:40  nemotron-con-100-num-200-v2
2026-07-05 12:03  nemotron-v3
2026-07-05 21:28  nemotron-v4
2026-07-07 08:46  nemotron-v4-retest
2026-07-07 21:33  nemotron-v4-retest-v2
2026-07-08 08:45  nemotron-v4-composite-urgency
2026-07-08 12:45  nemotron-v4-exp-backoff
2026-07-08 18:53  nemotron-v4-tiered-kv
2026-07-08 18:54  nemotron-v4-isc
2026-07-09 22:52  nemotron-v5
2026-07-10 09:32  nemotron-v6
2026-07-10 23:13  nemotron-v7
2026-07-11 23:42  nemotron-v8
2026-07-13 14:26  nemotron-v9
2026-07-14 08:21  nemotron-v10
2026-07-14 13:50  nemotron-v11
2026-07-14 17:33  nemotron-v12
2026-07-14 (17:38 config; 21:55 run capture; dir re-touched 07-16 23:55)  nemotron-v13  ← out-of-order, see §4
2026-07-15 10:26  nemotron-v14
2026-07-16 22:27  nemotron-v15
2026-07-17 09:45  nemotron-v16
2026-07-17 14:06  nemotron-v17
2026-07-18 15:35  nemotron-v17-test
2026-07-18 19:28  nemotron-v18
2026-07-18 23:05  nemotron-v18-re
2026-07-19 12:55  nemotron-v19
2026-07-19 18:44  nemotron-v20
2026-07-20 00:11  nemotron-v21
2026-07-21 12:43  nemotron-v22
```

Comparison-only directories (`*_vs_*_las[/-matched]`, `las_comparison*`) are pre-computed
outputs of `compare_las_variants_exact.sh` / `plots/compare_reports.py` run against pairs
or groups of the above; they contain no new experiment, just cross-run plots/tables. See
§6.

---

## 2. Era 0 — Qwen3.6-35B, pre-Nemotron (2026-06-30 → 2026-07-04)

This is the README's documented setup: `Qwen/Qwen3.6-35B-A3B-FP8`, 2 decode replicas,
TP=2, H200 GPUs, `Exgentic/agent-llm-traces` dataset, deterministic seed 42.

### Replicas-2-TP-2-run4
- **Date:** 2026-06-30 12:54
- **What:** The full 3-way LAS vs RR vs No-Fairness comparison documented in `README.md`.
  600 concurrent / 1000 total sessions, session_rate 10, 20,523 requests.
- **Outcome (from README, authoritative):**
  - Completion: LAS 89.7% (18,419/20,523), RR 86.8% (17,814/20,523), No-Fairness 56.9%
    (11,685/20,523).
  - Session median duration: LAS 2432s, RR 2925s (LAS 17% faster overall; up to 34% faster
    on short 2–7 request sessions).
  - No-Fairness's apparent per-request latency "win" is survivorship bias — it only
    finishes the easy/short requests; its ITL p99 (1198ms) is ~4× LAS/RR's.
  - **Takeaway:** LAS ≈ RR on completion, LAS modestly ahead on latency; No-Fairness looks
    fast only because it abandons more than half the work.

### las-vs-rr-261-max-300-session
- **Date:** 2026-06-30 18:47 (earliest of the `las-rr-*` family despite being listed later
  alphabetically)
- **What:** LAS vs RR at 300 concurrent / 400 sessions, session_rate 10. Directory name's
  "261" refers to the `concurrency-detector maxConcurrency` value used (distinct from the
  140 value settled on later, from nemotron-v4 onward).
- **Outcome:** Both near-perfect: LAS 2673/2673 succeeded (0 failed), RR 2672/2673 (1
  failed). LAS median 5.29s vs RR 2.97s (RR better median here), LAS p99 46.1s vs RR
  67.3s (LAS better tail). Small-scale, low-signal comparison — not clearly won by either.

### las-test
- **Date:** 2026-07-01 12:00 (config mtime 07-02 10:44)
- **What:** Smoke test filtered to `harness == 'smolagents_code'` traces only, tiny scale
  (5 concurrent / 10 total sessions). Only the LAS variant was actually run (`rr/`,
  `no-fairness/` dirs exist but hold no populated reports).
- **Outcome:** 57/58 requests succeeded (1 failure), median latency 0.59s, p99 48.9s. Pure
  smoke test, not a strategy comparison.

### las-test copy
- **Date:** 2026-07-01 15:41
- **What:** Same smoke test as `las-test`, filter loosened to accept any harness (not just
  `smolagents_code`).
- **Outcome: ⚠ OPEN QUESTION.** Report directories exist for las/rr/no-fairness but no
  populated report JSONs were confirmed — unclear if this run actually completed, or is a
  leftover scaffold from a repeat attempt.

### las-test-no-claude-code
- **Date:** 2026-07-02 11:19 (config mtime 07-02 18:12)
- **What:** Despite the name, its filter is `harness == 'claude_code'` — i.e. it
  *restricts to* claude_code traces, the opposite of what "no-claude-code" implies. Model
  `Qwen/Qwen3.6-35B-A3B`, 5 concurrent / 20 sessions.
- **Outcome:** **No run occurred.** No `reports/` directory, no metrics anywhere in this
  directory. Config-only; either never executed or executed without saving output here.
- **⚠ OPEN QUESTION:** name/filter mismatch unresolved — possibly an authoring mistake, or
  the name refers to intent that was never implemented in the filter that was actually
  written.

### las-rr-400
- **Date:** 2026-07-02 21:54 (config mtime 07-02 18:51)
- **What:** LAS vs RR head-to-head, 300 concurrent / 400 sessions, session_rate 2. Same
  scheduler plugin config as `las-vs-rr-261...` and `las-rr-800` (no plugin/weight diffs
  across all three).
- **Outcome:** Both completed all 2591 requests, zero failures. LAS median 12.15s vs RR
  10.21s (RR slightly ahead), LAS p99 353.7s vs RR 348.0s (near-tied).

### las-rr-800
- **Date:** 2026-07-03 08:12 (config mtime 07-02 21:56)
- **What:** Same comparison scaled up: 400 concurrent / 800 sessions, session_rate 10.
- **Outcome:** First run in the series to show real capacity limits: LAS 10119/10594
  succeeded (475 failed), RR 10149/10621 (472 failed). LAS median 90.9s vs RR 86.1s; p99
  574s vs 566s. RR marginally ahead on both latency and failure count at this scale — this
  result likely motivated backing off the load level rather than pushing further at 800.

**Era 0 verdict:** small-to-mid-scale LAS-vs-RR comparisons are inconclusive/mixed at this
point (each wins on some slice), except at higher load (800 sessions) where both degrade
significantly and roughly tie. `Replicas-2-TP-2-run4` (documented in the README) remains
the one clean, well-powered 3-way result from this era.

---

## 3. Era 1 — Nemotron scale-finding (2026-07-03 → 2026-07-04, pre-"v3")

These predate `nemotron-v3` and appear to be pure workload-scale sweeps (concurrency ×
session-count) rather than scheduler comparisons, likely finding a viable operating point
for the (new, at this point) Nemotron model before committing to the "vNN" naming.

| Dir | Date | concurrent_sessions | num_sessions | session_rate |
|---|---|---|---|---|
| `nemotron-test` | 2026-07-03 15:18 | 20 | 40 | 5 |
| `nemotron-600` | 2026-07-04 09:59 | 300 | 600 | 10 |
| `nemotron-test-1` | 2026-07-04 11:41 | — (has `reports/`, not individually re-checked) | | |
| `nemotron-test-2` | 2026-07-04 14:08 | — (has `reports/`, not individually re-checked) | | |
| `nemotron-con-100-num-200` | 2026-07-04 16:19 | 100 | 200 | 10 |
| `nemotron-con-100-num-200-v2` | 2026-07-04 19:40 | 100 | 200 | 10 (repeat of the same scale) |

`nemotron-test` has no `reports/` at all (config-only, like several other throwaway dirs in
this project). The rest have populated `reports/` (not deep-parsed here — low incremental
value versus the fully-analyzed v3+ series that immediately follows and supersedes them).

**⚠ OPEN QUESTION:** `nemotron-test-1`/`nemotron-test-2`'s exact scale wasn't pulled during
this reconstruction — ask if you need their numbers specifically; they're minor scale
probes bracketed tightly between `nemotron-600` and `nemotron-con-100-num-200`.

---

## 4. Era 2 — nemotron-v3 through v9: baseline tuning + evolved LAS strategies (2026-07-05 → 2026-07-13)

### nemotron-v3
- **Date:** 2026-07-05 12:03
- **What:** First "vNN"-named Nemotron run. 300 concurrent / 600 sessions,
  `concurrency-detector maxConcurrency: 261` (carried over from the Era-0 "261" config).
- **Outcome:** Overloaded — LAS 7854/8165 succeeded (311 failed), RR 7799/8118 (319
  failed); both variants show heavy latency (median ~102s, p99 ~955–972s). Superseded
  immediately by v4's lower concurrency ceiling.

### nemotron-v4
- **Date:** 2026-07-05 21:28
- **What:** Fixes v3's overload: load reduced to 200 concurrent / 400 sessions, and
  `maxConcurrency` cut 261→140.
- **Outcome:** Dramatic improvement — LAS 8210/8213 succeeded (3 failed), RR 8224/8225 (1
  failed). Median latency dropped sharply: LAS 43.8s vs RR 65.4s (**LAS notably ahead**).
  This config (400 sessions / maxConcurrency 140) becomes the standing baseline,
  immediately re-run twice for stability confirmation.

### nemotron-v4-retest
- **Date:** 2026-07-07 08:46
- **What:** Literal re-run of v4 (byte-identical config/plugins), plus adds a
  `no-fairness-epp-plugins.yaml` (a third comparison arm not present in v4).
- **Outcome:** LAS reproduces v4 closely (8224/8225 succeeded, 1 failed, median 45.2s).
  RR degrades vs v4: 7876/7899 succeeded (**23 failed, up from 1**), median 69.8s — some
  run-to-run variance in RR's failure rate at this config, worth keeping in mind when
  treating any single run's RR number as ground truth.

### nemotron-v4-retest-v2
- **Date:** 2026-07-07 21:33
- **What:** Second re-run of v4, again identical config.
- **Outcome:** LAS 8204/8208 succeeded (4 failed, median 45.1s); RR 8224/8225 (1 failed,
  median 68.4s). Across v4/retest/retest-v2, **LAS consistently shows ~45s median vs RR's
  ~65–70s** — the clearest, most reproducible LAS-beats-RR signal in the whole early
  series.
- **⚠ OPEN QUESTION (timing, not outcome):** this run's directory timestamp (21:33) sits 3
  minutes before the `agentic-bench` commit `2a39716` ("made changes to config.yaml",
  21:36:59) that switches the model to Nemotron-3-Super-120B-A12B-FP8 and adds several new
  inference-perf config fields (`predecessor_wait_timeout`, `output_tokens_overestimation_factor`,
  `max_sequence_length`, `bad_tool_call_handling`, `repair_truncated_tool_call_args`). It's
  ambiguous from git history alone whether this specific run used the config just before or
  just after that commit landed — the run directory's own captured `config.yml` was not
  re-inspected for these specific fields to settle it definitively.

### nemotron-v4-composite-urgency, -exp-backoff, -tiered-kv, -isc
- **Date:** 2026-07-08, 08:45 / 12:45 / 18:53 / 18:54 respectively
- **What:** Each tests one of four new "evolved LAS" fairness strategies added in
  `llm-d-inference-scheduler` commit `4f81a7a2` ("Add evolved LAS strategies:
  composite-urgency, exp-backoff, tiered-kv, isc", 2026-07-07 10:46, branch
  `feat/evolved-las-strategies`). Per that commit's own doc comments, these strategies were
  produced by an automated search process ("AdaEvolve run llm_d_las_0622_2242") rather than
  hand-designed:
  - **composite-urgency** — `score = waitScore + serviceScore + starveBoost`
    (immediate-p99 term + cumulative-wait-fairness term + a capped starvation boost);
    "Solution 10" from the AdaEvolve run, deliberately avoids EMAs/dual-timescale debt for
    operational safety.
  - **exp-backoff** — time-fair (not compute-fair) policy:
    `expFactor = exp(beta * timeSinceLastServe / maxTimeSinceLast)`.
  - **tiered-kv** — strict 3-tier priority ladder: hard starvation ceiling > idle-progress
    gate > KV-pressure-weighted score.
  - **isc** ("Integer Starvation Counter") —
    `score = waitSecs * (1+0.15*consecutiveMisses)^2 / (1+0.1*bank)`.
  - Only the plain `las` variant was run for each (no RR/no-fairness arm in any of the
    four) — so these are compared against the *separate* v4/retest/retest-v2 LAS baseline,
    not a same-run baseline.
- **Outcome:**
  - composite-urgency: 8224/8225 succeeded (1 failed), median **64.7s** — clearly worse
    than v4's ~45s LAS baseline.
  - exp-backoff: 8222/8224 succeeded (2 failed), median **62.5s** — also worse than
    baseline.
  - tiered-kv: 8224/8225 succeeded (1 failed), median **66.8s** — also worse than baseline.
  - isc: **no run artifacts at all** — directory holds only `config.yml` and
    `las-epp-plugins.yaml` (strategy field set to `"isc"`); no reports, no results. Either
    never executed, or crashed before writing output.
  - **Verdict: all three evolved strategies that actually ran underperformed plain LAS on
    median latency at this workload — a negative result for this batch.** This likely
    motivated returning to tuning around vanilla LAS rather than pursuing these variants
    further (there is a separate `las_comparison_exact/` output, see §6, that directly
    compares v4 LAS against the exp-backoff variant with a percentile table).
  - A separate, later comparison — `las_comparison_exact/percentile_table.txt` (2026-07-08
    08:58, comparing "LAS (v4)" vs "LAS+Composite") — actually shows the composite variant
    with a **better** failure count (1/400 vs LAS's 3/400) despite a mixed latency picture
    (composite faster on 2-7 req sessions, both roughly tied on longer sessions, composite
    slightly slower on ALL-bucket p50). This nuances the "worse" verdict above: on *this*
    smaller/different-scale comparison, composite-urgency's reliability is arguably better,
    even though the separately-run v4-composite-urgency directory's raw median (64.7s) reads
    worse than the v4/retest baselines. Treat these as two different comparisons at
    different scales, not a contradiction to resolve into one number.

### nemotron-v5
- **Date:** 2026-07-09 22:52
- **What:** Reverts to plain LAS strategy; load increased to 200 concurrent sessions (exact
  session count not separately re-verified); `maxConcurrency` reduced 140→133.
- **Outcome:** Degraded vs the v4 baseline — LAS 5537/5714 succeeded (177 failed), RR
  4973/5183 (210 failed); median latencies jumped to ~103–104s for both. Read as a
  harder/larger workload exposing capacity limits, not a clean pass.

### nemotron-v6
- **Date:** 2026-07-10 09:32
- **What:** Fixes v5: load reduced to 100 concurrent / 400 sessions, session_rate 10→5,
  `maxConcurrency` cut 133→80.
- **Outcome:** LAS 8196/8201 succeeded (5 failed, median 54.0s), RR 8210/8213 (3 failed,
  median 65.7s) — failures much lower than v5; LAS again ahead of RR on median.

### nemotron-v7
- **Date:** 2026-07-10 23:13
- **What:** Load scaled back up (160 concurrent / 600 sessions), `maxConcurrency` raised
  80→140 (back to the v4 value).
- **Outcome: best result of the v3–v9 batch.** LAS 12733/12737 succeeded (4 failed, median
  45.3s), RR 12746/12748 (2 failed, median 44.9s) — very low failure rates at higher scale
  than v4, and LAS/RR medians now nearly identical.

### nemotron-v8
- **Date:** 2026-07-11 23:42
- **What:** Re-run of v7's exact workload/plugin config (only a new commented-out image
  tag reference added; no functional diff).
- **Outcome:** Consistent with v7 — LAS 12746/12748 (2 failed, median 45.2s), RR 12730/12734
  (4 failed, median 46.4s). Confirms v7's result reproduces.

### nemotron-v9
- **Date:** 2026-07-13 14:26
- **What:** Another re-run, fully identical config to v8.
- **Outcome:** Consistent again — LAS 12732/12737 (5 failed, median 46.8s), RR 12741/12744
  (3 failed, median 45.8s). Three consecutive runs (v7/v8/v9) now confirm a stable,
  low-failure baseline where LAS and RR perform comparably — this is the point where the
  early exploratory phase settles into a trusted baseline config, immediately followed by
  the concurrency-tuning steps of v10–v12 and (eventually) the Nemotron-H
  precise/approximate-prefix investigation.

**Era 2 verdict:** v4 → v6 → v7/v8/v9 is a converging line of concurrency-limit tuning
that lands on a stable, reproducible LAS≈RR baseline. The four evolved-LAS strategy
variants tried alongside this (composite-urgency, exp-backoff, tiered-kv, isc) did not
beat plain LAS at the scale they were tested; `isc` produced no data at all.

---

## 5. Era 3 — nemotron-v10 through v17-test: concurrency scaling + the precise-prefix debugging saga (2026-07-14 → 2026-07-18)

**Context you should read first if you haven't:** `PRECISE_PREFIX_FIX_JOURNEY.md` and
`PRECISE_PREFIX_ZERO_DIAGNOSIS.md` in this repo's root already narrate the precise-prefix
debugging story in full detail (including a wrong early conclusion about Mamba
architecture that was later corrected). This section places v10–v17-test on that timeline
with config-level evidence; it does not repeat the full narrative.

### nemotron-v10
- **Date:** 2026-07-14 08:21 (config generated 00:04)
- **What:** First run in this series confirmed on `nvidia/NVIDIA-Nemotron-3-Super-120B-A12B-FP8`
  (TP=2, `--enable-chunked-prefill`, `--max-num-seqs=128`). Single (non-split)
  `prefix-cache-scorer` weight 3 — pre-precise-prefix era. `concurrency-detector
  maxConcurrency: 140`. 160 concurrent / 600 sessions. No KV-events, no precise-prefix
  producer at all yet.
- **Note:** `llm-d-inference-scheduler` commit `589afacb` ("add debug-only per-request
  event log with drain endpoint", 2026-07-14 09:12) landed essentially concurrently with
  this run — v10 sits right at the birth of the `records.jsonl` per-request debug feature.
- **Outcome:** 12,748 requests, 12,746 succeeded, 2 failed. Baseline/tuning run, not a
  strategy comparison.

### nemotron-v11
- **Date:** 2026-07-14 13:50 (config generated 10:39–10:40)
- **What:** Same load and plugin config as v10 (byte-identical `las-epp-plugins.yaml`) —
  effectively a repeat.
- **Outcome:** 12,727 requests, 12,723 succeeded, 4 failed. Scheduler commits `845775f3`
  (09:38, adds per-pod prefix match — but with a bug limiting it to only the winning pod)
  and `0f990510` (10:28, fixes that bug to cover all candidate pods) landed the same
  morning between v10 and v11, but neither run wires precise-prefix so the fix isn't
  visible in either run's records yet.

### nemotron-v12
- **Date:** 2026-07-14 17:33 (config generated 14:57)
- **What:** Load bumped: `concurrent_sessions` 160→300 (still 600 sessions).
  `concurrency-detector maxConcurrency` doubled 140→280 to track the concurrency increase.
  Scorer weights otherwise unchanged.
- **Outcome:** 12,718 requests, 12,714 succeeded, 4 failed. Concurrency-scaling tuning
  step.

### nemotron-v13 — the out-of-order run
- **Date:** directory mtime 2026-07-16 23:55, but its `config.yml`/`las-epp-plugins.yaml`
  file mtimes are 2026-07-14 17:38 (config generated right after v12, same day); the run
  itself (decode-deployment/epp-configmap/`las/` results) captured 2026-07-14 21:55–21:57;
  the top-level directory was touched again 2026-07-16 23:55, likely a later
  re-save/re-download rather than a second run.
- **What:** `concurrent_sessions: 330`, `num_sessions: 600` — the same 600-session family
  as v10–v12, **not** the 800/1600-session family used by v14 onward.
  `concurrency-detector maxConcurrency: 160`. Scorer config is the OLD single-scorer style
  (plain `prefix-cache-scorer` weight 3, no split approx/precise, no
  `precise-prefix-cache-producer`, no `token-producer`) — matching v10–v12's era, not
  v16/v17's. **However**, its decode deployment's vLLM args add `--tensor-parallel-size=4`
  (up from v10–v12's TP=2), drop `--enable-chunked-prefill`/`--max-num-seqs=128`, and add
  `--block-size=64` + `--kv-events-config` (topic `kv@$(POD_IP)@...`, bare IP — the
  not-yet-fixed form) with a config comment already referencing
  "precise-prefix-cache-producer" and `blockSizeTokens=64` — i.e. the decode-side plumbing
  for precise-prefix was being test-fitted here, ahead of the EPP-side config that
  actually consumes it (which arrives in v16).
- **Evidence this predates v14–v17's EPP build:** `records.jsonl` here (12,746 lines) uses
  the OLD flat schema (`prefix_hit_blocks`, `prefix_total_blocks`, `prefix_block_size`,
  `prefix_hit_ratio`), which was superseded by scheduler commit `b5e3b907` (2026-07-17
  00:27, "record per-producer prefix matches and dispatch timestamp"). A build produced
  before that commit cannot be from 2026-07-16/17.
- **Outcome:** 12,748 requests, 12,746 succeeded, 2 failed — nearly identical to v10's
  numbers.
- **Conclusion:** v13 is effectively a "v10–v12-family run, plus a decode-side KV-events
  preview that wasn't yet wired into the EPP config" — captured/named on disk out of
  chronological order relative to v14/v15/v16/v17, not a true feature successor to v12.
  The decode-side KV-events experiment it previews was **not** carried forward into v14
  (which reverts to the clean TP=4 baseline with no KV-events), and only reappears,
  properly wired, in v16.
- **⚠ OPEN QUESTIONS:**
  - Why the directory was re-touched on 07-16 (a `download.sh` re-run without a new load
    test? unconfirmed).
  - Whether the KV-events preview here was deliberate isolation testing or an accidental
    leftover config from the 14th.
  - Why the `--block-size=64`/`--kv-events-config` decode change wasn' carried into v14 —
    inferred as "not yet paired with EPP-side consumption," not documented anywhere.

### nemotron-v14
- **Date:** 2026-07-15 10:26 (config generated 00:06)
- **What:** Load bumped substantially: `concurrent_sessions: 455`, `num_sessions: 800` (up
  from 600). Decode args revert to the clean v10–v12-style baseline (TP=4, no
  `--block-size=64`/`--kv-events-config` — v13's KV-events preview dropped).
  `concurrency-detector maxConcurrency: 160` (same as v13). Scorer weights unchanged
  (single `prefix-cache-scorer` weight 3).
- **Outcome:** 16,782 requests, 16,779 succeeded, 3 failed. Clean load-scale-up step on a
  non-precise-prefix baseline.

### nemotron-v15 — "no-prefix-score" baseline
- **Date:** 2026-07-16 22:27 (config generated 18:11)
- **What:** Load bumped again to `num_sessions: 1600` (concurrent_sessions unchanged at
  455) — the scale used through v16/v17 as well. **`prefix-cache-scorer` weight explicitly
  set to `0`** (vs. weight 3 everywhere else in this era) — a deliberate ablation isolating
  fairness-scheduling behavior with no prefix-affinity signal at all. This is the
  "no-prefix-score" baseline referenced throughout later comparisons.
- **Outcome:** 32,913 requests, 32,899 succeeded, 14 failed.

### nemotron-v16
- **Date:** 2026-07-17 09:45 (config generated 01:09)
- **What:** First run wiring the full precise-prefix machinery into the EPP: adds
  `precise-prefix-cache-producer` (`tokenProcessorConfig.blockSizeTokens: 64`,
  `kvEventsConfig.topicFilter: "kv@"`, pod discovery via `socketPort: 5556` /
  `podLabelSelector: llm-d.ai/role=decode`), and splits the single `prefix-cache-scorer`
  into two named instances: `approx-prefix-scorer` (weight 0) and `precise-prefix-scorer`
  (weight 3, `prefixMatchInfoProducerName: precise-prefix-cache-producer`). Decode
  deployment re-adds `--block-size=64` + `--kv-events-config` (topic still bare
  `$(POD_IP)`, no port). **No `token-producer` entry anywhere — this is exactly "Bug 1"**
  from `PRECISE_PREFIX_FIX_JOURNEY.md`: with no explicit token-producer, the DAG
  auto-creates the `estimate` (pseudo-token) tokenizer backend, which can never match real
  vLLM KV-event token IDs.
- **Evidence this EPP build postdates the schema change:** `records.jsonl` already uses the
  new per-producer `prefix` map schema, confirming the build is after commit
  `b5e3b907`/`060f75b9` (2026-07-17 00:27/01:04), i.e. built the same morning, just before
  this run's config was generated (01:09).
- **Outcome:** 32,933 requests, 32,922 succeeded, 11 failed (fine at the harness level).
  **Precise-prefix records are ALL ZERO** — this is the diagnostic failure the whole
  journey doc investigates. Root cause per that doc: missing token-producer (Bug 1),
  compounded by the KV-events topic pod-identity mismatch (Bug 2, described under v17-test
  below).

### nemotron-v17
- **Date:** 2026-07-17 14:06 (config generated 10:09)
- **What:** Same scorer/session config as v16 (weights, session counts, maxConcurrency all
  unchanged), but adds the `dataLayer` wiring block (`metrics-data-source` /
  `core-metrics-extractor` + `endpoint-notification-source` → `precise-prefix-cache-producer`
  extractor) plus the corresponding top-level plugin declarations that v16 lacked. Still
  **no `token-producer`** — Bug 1 persists.
- **Outcome:** 32,966 requests, 32,959 succeeded, 7 failed (best failure count of the
  v15–v17 trio). Per `PRECISE_PREFIX_ZERO_DIAGNOSIS.md`: still 32,949 records, precise
  NONZERO = 0 — the dataLayer plumbing alone did not fix the zero-precise-prefix problem,
  confirming the missing token-producer was still the blocker at this point.

### nemotron-v17-test — the fix, verified
- **Date:** 2026-07-18 15:35 (config generated 14:47)
- **What:** A deliberately small throwaway validation run (`config.yml`: 50 concurrent /
  100 sessions — the real 455/1600 stage block explicitly commented out, ~16x smaller)
  built specifically to verify both real fixes at once before committing to a full sweep:
  1. **Fix 1** — adds the missing `token-producer`
     (`modelName: nvidia/NVIDIA-Nemotron-3-Super-120B-A12B-FP8`,
     `vllm.url: http://localhost:8000`, a co-located CPU-only `vllm launch render` sidecar
     — full detail in `PRECISE_PREFIX_FIX_JOURNEY.md` §"Hoop 3").
  2. **Fix 2** — the decode `--kv-events-config` topic now includes the serving port:
     `kv@$(POD_IP):8000@nvidia/...` (was bare `$(POD_IP)`) — fixes the pod-identity
     mismatch where the EPP looks up pods by `<IP>:8000` but the bare-IP topic meant no
     pod ever attached to a matched cache key.
  3. Also adds `--enable-prefix-caching` to the decode vLLM args (a separate,
     already-validated-in-isolation fix from `PRECISE_PREFIX_ZERO_DIAGNOSIS.md`'s
     2026-07-17 validation section — needed to actually engage vLLM's cache at all,
     independent of the two EPP-side fixes above).
- **Outcome:** 2,183 requests, 2,183 succeeded, **0 failures**. This is the run/config
  snapshot that operationalized both real fixes together, matching
  `PRECISE_PREFIX_FIX_JOURNEY.md`'s "Verification (2026-07-18)" section — repeat-request
  precise records finally went nonzero (`match_blocks=132/153`, `cached_block_count=132`).

**Era 3 verdict:** v10–v14 is concurrency/session-scale tuning on a non-split
prefix-cache-scorer. v13 is an out-of-order snapshot best read as "v10–v12-family plus an
abandoned decode-side KV-events preview." v15 establishes the no-prefix-score ablation
baseline. v16→v17→v17-test is the precise-prefix-cache debugging arc: v16 wires the
producer but has no tokenizer (Bug 1) and a pod-identity mismatch (Bug 2); v17 fixes the
data-layer plumbing but not Bug 1; v17-test fixes both real bugs and verifies precise
records go nonzero, at small scale, clearing the way for the full-scale v18 run.

---

## 6. Era 4 — nemotron-v18 through v22: precise-prefix at scale, program-pinning, and final tuning (2026-07-18 → 2026-07-21)

**No commits exist in `llm-d-inference-scheduler` for any of this era's EPP-side work** —
the repo's most recent commit on any branch is `060f75b9` (2026-07-17 01:04). Everything
in this section (the v18 precise-prefix full-scale rollout, and especially the
`program-aware-scorer` / "program-pinning" plugin used in v20/v21) was built and deployed
as **uncommitted working-tree changes**. Per §0 item 1, the program-aware-scorer plugin has
now been committed locally (`5102a068`) as part of this reconstruction; the earlier
precise-prefix config changes were applied via `run.sh` heredocs (uncommitted in this
repo too — see the current working-tree diff on `run.sh`) and via direct `oc patch` /
live-ConfigMap edits on the cluster, per `PRECISE_PREFIX_FIX_JOURNEY.md`'s "Persistence /
where each fix lives" section.

### nemotron-v18
- **Date:** 2026-07-18 19:28
- **What changed vs v15 (no-prefix-score baseline):** the full precise-prefix wiring
  validated in v17-test, now applied at full scale (455 concurrent / 1600 sessions): adds
  `metrics-data-source`, `core-metrics-extractor`, `endpoint-notification-source`, the
  `token-producer` (vllm render backend), `precise-prefix-cache-producer`, splits
  `prefix-cache-scorer` into `approx-prefix-scorer` (weight 0) / `precise-prefix-scorer`
  (weight 3), and the `dataLayer` block.
- **Outcome:** 20,986/21,530 requests succeeded; **1061/1600 sessions succeeded, 539
  failed (33.7% session failure rate)** — high, and the run only reached 21,530 of the full
  ~33,141-event scale that later runs (v18-re, v19, v21, v22) complete, i.e. this run
  appears to have been cut off partway rather than genuinely failing that much of its
  workload.

### nemotron-v18-re — clean re-run
- **Date:** 2026-07-18 23:05
- **What changed vs v18:** **nothing in the EPP/workload config** —
  `las-epp-plugins.yaml`, `epp-configmap.yaml`, `config.yml` are byte-identical to v18.
  Only Kubernetes deployment metadata differs (gpu-reaper idle/scale-down timestamps,
  `generation`, `resourceVersion`, replica availability). The "-re" suffix means
  **re-run**, not a fix.
- **Outcome:** 32,996/32,999 requests succeeded; **1597/1600 sessions succeeded, only 3
  failed** (vs. 539 in v18) — runs the full ~33,141-event scale to completion this time.
  Session median duration 538.4s (~identical to v18's 537.4s — confirming v18's config was
  fine, it just didn't finish), p99 4534.0s (higher than v18's 3323.1s, because v18 never
  reached its slowest long-tail sessions before being cut off). **v18-re is the
  "precise-prefix" result referenced throughout later comparisons and the naming
  convention memory note.**

### nemotron-v19 — "approximate-prefix"
- **Date:** 2026-07-19 12:55
- **What changed vs v18-re:** pure scoring-weight toggle — `approx-prefix-scorer` set to
  weight 3 (active), `precise-prefix-scorer` set to weight 0 (disabled). No code change; a
  config-level A/B flip on the identical EPP binary, isolating which prefix-prediction
  signal (self-seeded approximate hash index vs. real-KV-event-driven precise index)
  drives routing.
- **Outcome:** 33,001/33,003 requests succeeded; 1598/1600 sessions succeeded (2 failed).
  Session median duration **518.5s**, p99 4413.7s — marginally better than v18-re on both
  median and p99, and fewer failures (2 vs 3). **Approximate-prefix slightly edges out
  precise-prefix on this workload**, despite precise being the "ground truth" signal —
  likely because the approximate producer's overhead/latency is lower and its accuracy is
  close enough at this workload's prefix-sharing pattern.

### nemotron-v20 — "program-pinning," failed
- **Date:** 2026-07-19 18:44
- **What changed vs v19:** adds the new `program-aware-scorer` plugin
  (`missThreshold: 3` parameter) and reweights: `precise-prefix-scorer` back to weight 3,
  `program-aware-scorer` added at weight 3. Source: the (at-the-time uncommitted, now
  committed as `5102a068`) `pkg/epp/framework/plugins/scheduling/scorer/programaware/program_aware.go`.
  Mechanism: pins each program (by `FairnessID`) to one decode pod so all of a program's
  requests land on the same pod (maximizing KV-cache/session locality — LAS/RR/DRR don't
  consider pod affinity at all). Scoring gives the pinned pod 1.0, everything else 0.0; if
  the pinned pod isn't in the candidate set it falls back to the least-loaded pod without
  mutating the pin. The pin only migrates after `missThreshold` (3) consecutive requests
  where the pinned pod is absent — deliberate hysteresis against re-pinning on a transient
  blip.
- **Outcome:** 27,662/27,911 requests succeeded (again run short of the full ~33,141-event
  scale, similar truncation pattern to v18); **1352/1600 sessions succeeded, 248 failed**.
  **Root cause (confirmed directly in `nemotron-v20/las/records/records.jsonl`): all 3
  decode pods are present until ~2026-07-19 10:38:22 UTC, then 2 of the 3 pods disappear
  from the candidate set for the rest of the ~5158s run**, leaving one pod to absorb all
  traffic. Because pins only migrate after repeated misses, a large number of already-pinned
  programs were stuck contending for the one remaining pod for the rest of the run. Per
  your confirmation (§0 item 3), this was an infrastructure event (a mid-run pod loss,
  plausibly a `gpu-reaper` idle-scaledown or similar cluster event — the `decode-deployment.yaml`
  snapshot's `gpu-reaper` annotations are stale from 07-18 21:10–22:40 UTC, i.e. from a
  *prior* idle-scaledown, not from the run window itself, so the exact trigger for the
  in-run pod loss remains uncaptured), not a bug in the pinning scorer's own logic.
- **Note on the "1600" figure:** existing memory said "248/1600 failed" — the 1600 here is
  session count (1352 succeeded + 248 failed = 1600), consistent with the summary JSON.

### nemotron-v21 — "program-pinning," fixed, best overall result
- **Date:** 2026-07-20 00:11
- **What changed vs v20:** **no diff at all** in any config artifact — same
  `missThreshold: 3`, same weights, same everything in `las-epp-plugins.yaml` /
  `epp-configmap.yaml` / `config.yml`. The fix was environmental: a stable 3-pod decode
  deployment for the entire run (confirmed in `nemotron-v21/las/records/records.jsonl` —
  all 3 pods present start to finish, on a freshly-named rollout).
- **Outcome:** 33,140/33,141 requests succeeded; **1599/1600 sessions succeeded, only 1
  failed** — that single failure has `request_latency ≈ 1800.27s`, i.e. it hit the ~30-minute
  timeout ceiling, reading as an isolated straggler rather than a systemic issue. Session
  median duration **391.6–392.0s**, p99 **3297.7–3297.8s** — **the best (lowest) p50 AND
  p99 of every variant tested in this era** (v15 no-prefix-score, v18-re precise-prefix,
  v19 approximate-prefix, v21 program-pinning), confirmed directly in the 4-way comparison
  table `nemotron-v15_vs_v18-re_vs_v19_vs_v21_las/percentile_table.txt`, and the
  second-best failure count (behind only v19's 2/1600). **This is the best-performing
  configuration found across the entire project.**

### nemotron-v22 — precise-prefix retune
- **Date:** 2026-07-21 12:43
- **What changed vs v21:** weights swapped back: `precise-prefix-scorer` weight 0→**10**
  (a much higher weight than v18-re/v19's 3), `program-aware-scorer` weight 3→**0**
  (program-pinning disabled). So v22 is a refinement of the **precise-prefix (v18-re)**
  line, not of program-pinning — it does not build on v21's pinning result at all. The
  current uncommitted working-tree state of `run.sh` in this repo exactly matches v22's
  configuration, i.e. v22 is the config the harness is left pointed at right now.
- **Outcome:** 32,999/33,002 requests succeeded; 1597/1600 sessions succeeded, 3 failed
  (same failure count as v18-re). Session median duration 516.8–517.0s vs v18-re's
  537.2–537.5s — **~4.0% faster p50** ((537.2−515.7)/537.2 ≈ 4.0%, confirmed via
  `nemotron-v18-re_vs_nemotron-v22_las/percentile_table.txt`), essentially flat/slightly
  better p99 (4472.1–4472.7 vs 4534.0). **A small, real improvement over v18-re, but v21
  (program-pinning) still beats it on every metric — v22 was not tested against v21
  directly in any comparison directory.**

**Era 4 verdict, ranked by overall result quality:**
1. **v21 (program-pinning, stable pods)** — best p50, best p99, 1/1600 failed. Best overall.
2. **v19 (approximate-prefix)** — p50 518.5s, p99 4413.7s, 2/1600 failed. Second-best
   failure count, close latency to v18-re.
3. **v22 (precise-prefix, weight 10)** — p50 515.7s (2nd-fastest p50 after v21), p99
   4472.1s, 3/1600 failed.
4. **v18-re (precise-prefix, weight 3)** — p50 537.2s, p99 4534.0s, 3/1600 failed.
5. **v15 (no-prefix-score baseline)** — p50 1305.7s, p99 9974.0s, 14/1600 failed. Clearly
   worst — any prefix-affinity or program-pinning signal roughly halves p50/p99 versus no
   signal at all.
6. **v20 (program-pinning, unstable pods)** — 248/1600 failed, but this reflects the
   mid-run infrastructure event, not the scorer itself; excluded from ranking on merit.

**⚠ OPEN QUESTION carried over:** v22 (precise-prefix retuned) was never directly compared
against v21 (program-pinning) in any `*_vs_*` comparison directory — the natural next
experiment, if you want a definitive answer on precise-prefix-at-higher-weight vs
program-pinning, would be a `nemotron-v21_vs_nemotron-v22_las` comparison, or a new run
combining both (precise-prefix weight 10 AND program-aware-scorer at nonzero weight
together) — which is plausibly what the currently-uncommitted `run.sh` state (both plugins
present, program-aware-scorer at weight 0) is staged to test next.

---

## 7. Comparison directories (pre-computed cross-run outputs)

These contain no new experiments — each is `plots/compare_reports.py` (or the
`compare_las_variants_exact.sh` wrapper) run against 2–4 of the run directories above,
producing `percentile_table.txt` plus PNG plots. Listed for completeness/navigation.

| Directory | Runs compared | Headline (from `percentile_table.txt`) |
|---|---|---|
| `las_comparison/` | (plot only, `las_variants_comparison.png`, no percentile table) | — |
| `las_comparison_exact/` + `-matched` | `nemotron-v4` (LAS) vs `nemotron-v4-exp-backoff` (labeled "LAS+Composite") | LAS(v4) 397/400 succeeded (3 failed); LAS+Composite 399/400 (1 failed). Mixed latency: composite faster on short sessions, roughly tied/slightly slower on longer ones and in the ALL bucket. |
| `nemotron-v15_vs_nemotron-v18-re_las/` + `-matched` | v15 (no-prefix-score) vs v18-re (precise-prefix) | v18-re dramatically faster: p50 537.2s vs 1305.7s (~2.4×), p99 4534.0s vs 9974.0s (~2.2×). Failures: v15 14/1600, v18-re 3/1600. |
| `nemotron-v15_vs_nemotron-v19_las/` + `-matched` | v15 vs v19 (approximate-prefix) | Similar magnitude win: p50 514.5s vs 1305.7s, p99 4414.2s vs 9974.0s. Failures: v15 14/1600, v19 2/1600 (best failure count of any pairwise comparison here). |
| `nemotron-v15_vs_v18-re_vs_v19_las/` + `-matched` | v15, v18-re, v19 (3-way) | Confirms v19 edges out v18-re slightly on both p50 (514.5 vs 537.2) and p99 (4414.2 vs 4534.0), and fewer failures (2 vs 3). |
| `nemotron-v15_vs_v18-re_vs_v19_vs_v21_las/` + `-matched` | v15, v18-re, v19, v21 (4-way, **the definitive table**) | v21 (program-pinning) wins on every bucket: best overall p50 (391.6s) and p99 (3297.8s) of the four, second-best failure count (1/1600, behind only v19's 2/1600, ahead of v18-re's 3/1600). |
| `nemotron-v18-re_vs_nemotron-v19_las/` + `-matched` | v18-re (precise) vs v19 (approximate) head-to-head | v19 slightly ahead: p50 514.5 vs 537.2, p99 4414.2 vs 4534.0, fewer failures (2 vs 3). |
| `nemotron-v18-re_vs_nemotron-v22_las/` + `-matched` | v18-re vs v22 (precise-prefix retune) | p50 improves 537.2→515.7 (~4.0% faster), p99 essentially flat (4534.0→4472.1), failures tied at 3/1600 each. |

All "-matched" variants restrict to sessions that succeeded in *both* compared runs; their
numbers move only slightly from the unmatched tables in every case checked, as expected.

---

## 8. Cross-repo code timeline

### `llm-d-inference-scheduler` (EPP / router)

| Date | Commit | Branch | Summary | Corresponding run(s) |
|---|---|---|---|---|
| 2026-06-08 | `51e768bb` | program-aware-las | Stamp `decayAnchor` on `AddService`, fixing a bug where a busy program's service got wiped on its first idle `Pick` (see the inference-scheduler repo's own memory: `project_decay_anchor_bug.md`) | Predates v3; baked into every nemotron run, not a distinguishing factor between any of them |
| 2026-07-07 10:46 | `4f81a7a2` | feat/evolved-las-strategies | Add evolved LAS strategies: composite-urgency, exp-backoff, tiered-kv, isc (AdaEvolve-searched policies) | `nemotron-v4-composite-urgency`, `-exp-backoff`, `-tiered-kv`, `-isc` (all 2026-07-08) |
| 2026-07-11 15:58 | `aa054864` | feat/evolved-las-strategies | Add endpoint label to prefix-cache and cached-token metrics (observability only) | v9-ish window, low signal |
| 2026-07-14 09:12 | `589afacb` | feat/evolved-las-strategies | Add debug-only per-request event log with drain endpoint (birth of `EPP_REQUEST_RECORDS` / `records.jsonl`) | v10/v11/v12 (same day) |
| 2026-07-14 09:38 | `845775f3` | feat/evolved-las-strategies | Record all-candidate pod state and per-pod prefix match — **buggy**, limited to only the winning pod | v10/v11/v12 |
| 2026-07-14 10:28 | `0f990510` | feat/evolved-las-strategies | Fix per-pod prefix match to cover all candidates (fixes the bug above) | v10/v11/v12, later same day |
| 2026-07-16→17 | `b5e3b907`, `060f75b9` | feat/evolved-las-strategies (HEAD as of this writeup, before the local commit below) | Record per-producer prefix matches + dispatch timestamp; introduces the producer-keyed `Record.Prefix` map schema | v14 (07-15, old schema), v15/v13 (07-16), v16/v17/v17-test (07-17, new schema) |
| **2026-07-19 13:31–13:44** | *(uncommitted until this reconstruction)* | feat/evolved-las-strategies (working tree) | New `program-aware-scorer` plugin (program→pod pinning); registered in `runner.go`; sample config added | **v20 (07-19) and v21 (07-20)** |
| 2026-07-21 (this reconstruction) | `5102a068` | feat/evolved-las-strategies | **Committed locally** (not pushed): the program-aware-scorer plugin above, exactly as it existed on disk | Retroactively covers v20/v21 |

Upstream (`llm-d-inference-scheduler` `upstream/main`, not on this project's working
branch, given for context on where precise/approximate-prefix originally came from):
- `2e1012c6` (2026-05-21, #1112) — adds `precise-prefix-cache-producer`.
- `e6f6b84f` (2026-05-14, #1107) — precise scorer switches to absolute matched/total
  normalization.
- `4650a284` (2026-06-02, #1121) — refactors precise-prefix-cache-scorer into a thin
  wrapper over the producer + a generic scorer.
- `f2fe1dec` (2026-06-08, #1413) — uses unweighted cached-block count for the prefix P/D
  decider.
- A handful of later upstream commits (`4fefbdf8`, `4a6c4164`, `b276744c`, `5106c5f2`,
  2026-07-14 through 07-18) touch prefix-match-info/DumpState but were **never merged into
  `feat/evolved-las-strategies`** — confirmed not ancestors of its HEAD — so none of them
  affected any nemotron-vNN run.

**What "program-pinning" is, in plain terms:** a scheduling scorer that remembers which
decode pod last served a given program (session) and keeps routing that program back to
the same pod, migrating only after 3 consecutive misses. This maximizes KV-cache/session
locality in a way LAS/RR/DRR fairness policies don't consider at all (they only look at
service fairness, not pod affinity).

**What "precise-prefix" vs "approximate-prefix" is, in plain terms:** both are prefix-cache
hit *predictors* used to score candidate pods before dispatch, feeding the same
`prefix-cache-scorer` plugin type under two different names/weights.
`approx-prefix-cache-producer` is the long-standing default — it hashes the request's own
(possibly pseudo-tokenized) prompt and checks a self-seeded local index, no external
dependency. `precise-prefix-cache-producer` (added upstream 2026-05-21) instead subscribes
to vLLM's real KV-cache block-store/eviction events over ZMQ and compares real engine token
IDs, so its predictions are ground-truth rather than approximated — at the cost of needing
a real tokenizer (the token-producer/render-sidecar fix from the July 17-18 debugging
saga) and a correctly-addressed KV-events feed.

### `inference-perf` (load generator)

**Frozen at `c47b3a4` (2026-07-04 02:04) for the entire nemotron-v3→v22 window** — no
commits landed after this on any branch. All behavioral differences between nemotron runs
come from `agentic-bench/config.yml` changes, not inference-perf code changes. Commits
immediately preceding the freeze, in order, all already present by the time v3 started:

| Date | Commit | Summary | Config field it enables | First nemotron config to set it |
|---|---|---|---|---|
| 2026-06-25 09:44 | `88c5f6b` | `bad_tool_call_handling=use_recorded` — works around a vLLM `qwen3_xml` parser bug that emits malformed JSON in `tool_calls[i].function.arguments` | `bad_tool_call_handling` | `2a39716` (07-07 21:36) onward — sets `use_recorded` |
| 2026-06-26 16:20 | `b29f5a0` | Emit Prometheus metric `inference_perf_run_elapsed_seconds` | none (commit's own description: "no wiring into run lifecycle, no CLI flag" — inert plumbing) | n/a |
| 2026-06-30 10:40 | `04b42a3` | Normalize `developer` message role to `system` for non-OpenAI model servers | (unconditional, no flag) | applies to all Nemotron runs (non-OpenAI server) from v3 onward |
| 2026-06-30 10:46 | `380331b` | Hardcoded predecessor-wait timeout raised 3600s→10800s | — | superseded same day by the next commit, never reached config as a fixed value |
| 2026-06-30 14:27 | `97e4081` | Makes predecessor-wait timeout configurable (`predecessor_wait_timeout`, default `None` = wait indefinitely) | `predecessor_wait_timeout` | `2a39716` sets it explicitly to `null` — **indefinite wait**, not 10800s |
| 2026-06-30 17:06 | `0582fde` | Client-side repair of truncated `tool_call` argument JSON (model hit `max_tokens` mid-generation) | `repair_truncated_tool_call_args` (code default `true`) | `2a39716` sets it to **`false`** — disabled despite existing |
| 2026-07-03 23:39 | `8794102` | Adds `output_tokens_overestimation_factor`, `max_sequence_length`; Anthropic-format tool-result parsing | `output_tokens_overestimation_factor: 0.4`, `max_sequence_length: 262144` | `2a39716` sets both |
| 2026-07-03 23:56 | `03c497b` | Splits multi-tool-result messages into separate per-tool messages, for OpenAI spec compliance | (unconditional) | applies to all runs replaying multi-tool-call traces, i.e. all nemotron runs |
| 2026-07-04 00:23 | `96ccd1d` | Adds the `repair_truncated_tool_call_args` flag itself (default `True`) to `SessionReplayConfig` | see above | see above |
| 2026-07-04 02:04 | `c47b3a4` | Fixes an `AttributeError` regression from the previous commit (moves the flag to the correct data object) | — | last commit before v3 (~34h gap) |

`agentic-bench`'s own tracked history (this repo, separate from the two code repos) shows
exactly four commits that changed the workload/config surface across the whole window:
- `2a39716` (2026-07-07 21:36:59) — "made changes to config.yaml": switches model to
  Nemotron-3-Super-120B-A12B-FP8, header key to `x-gateway-inference-fairness-id`, sets all
  the fields in the table above; lowers concurrency 600/1000/10→200/400/10; raises
  `request_timeout` 900→1800. **Boundary is ~3 minutes before `nemotron-v4-retest-v2`'s
  directory timestamp — ⚠ genuinely ambiguous which side of this commit that specific run
  landed on.**
- `7b09c88` (2026-07-10 11:37:14) — "added few additional things": concurrency
  200/400/10→160/600/5. Falls cleanly between v6 (09:32) and v7 (23:13) — applies from v7
  through v9.
- `f9d5db4` (2026-07-14 23:11:51) — "Wire per-request record drain into run/download
  harness": concurrency 160→330 (sessions stay 600). Falls after v10/v11/v12, before v14 —
  applies from v14 onward through v22.
- (`d67f888`, `0e60893` — earlier, pre-dating the whole nemotron series; the repo's initial
  fairness-experiments content and the README's run5/v0.9.0 write-up.)

The **current uncommitted working-tree diff** on `config.yml` / `run.sh` /
`model-server/patch-vllm.yaml` (concurrency→455/1600, TP=4 unquantized Qwen3.6-35B-A3B,
`precise-prefix-scorer` weight 10 + `program-aware-scorer` weight 0 both present) postdates
v22 — it was never used in any completed run; it's the harness's current staged-for-next
state.

---

## 9. Things to double check / decide

Collected from every research pass's "open questions," for your review:

1. **v18-re vs v22's "small refinement"**: no code change was found anywhere for v22 — it
   is purely a scoring-weight retune (§6). If you recall an actual code change alongside
   it, let me know so it can be attributed correctly.
2. **`nemotron-v4-retest-v2` config boundary** (§4): 3-minute ambiguity against commit
   `2a39716`. Low stakes unless you need to know precisely which config that run used.
3. **`las-test-no-claude-code`** (§2): its filter contradicts its name. Possible authoring
   mistake, unresolved.
4. **`las-test copy`** (§2): couldn't confirm this run actually completed with results.
5. **`nemotron-v4-isc`** (§4): zero output — never ran, or crashed before writing anything.
6. **v13's second directory touch on 07-16** (§5): unexplained by any config/commit
   evidence — likely a re-download, not a re-run, but unconfirmed.
7. **v20's exact infra trigger** (§6): confirmed to be a pod-loss event via `records.jsonl`
   evidence, and confirmed as an infra issue (not a code bug) per your input — but the
   *specific* cause (which cluster event caused 2 of 3 pods to disappear at 10:38 UTC) is
   still uncaptured; would need cluster/`gpu-reaper` logs from that window if you want it
   pinned down further.
8. **v21 vs v22, head-to-head**: never directly compared in a `*_vs_*` directory. If you
   want a definitive "does program-pinning beat the higher-weight precise-prefix retune"
   answer, that comparison (or a combined run testing both together, which the currently
   uncommitted `run.sh` state looks staged for) doesn't exist yet.
9. **`repair_truncated_tool_call_args: false`** and **`predecessor_wait_timeout: null`**
   (§0, §8): confirm these were deliberate choices for trace fidelity / correctness, since
   they run counter to what the code defaults to.
10. **The program-aware-scorer commit `5102a068`** is local-only. Decide whether/when to
    push it to `origin/feat/evolved-las-strategies` or fold it into a PR.

---

## 10. What's next (not yet decided)

Per your instruction, no cleanup of the experiment result directories (deleting
superseded runs, archiving, etc.) has been done as part of this reconstruction — this
document is purely descriptive. Once you've reviewed this, a reasonable follow-up would be
deciding what to do with clearly-superseded or empty directories (e.g. `nemotron-v4-isc`,
`las-test-no-claude-code`, possibly `nemotron-v18` now that `v18-re` supersedes it) — but
that's a separate decision from this doc.
