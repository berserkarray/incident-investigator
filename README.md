# Incident Investigator

A multi-agent system that investigates a past production incident from simulated logs, metrics, deploy history, service dependencies and past incidents. It finds the probable root cause, or reports "inconclusive" when the evidence is insufficient, and recommends fixes tied to evidence-backed hypotheses. A single-agent ReAct baseline with the same model, tools, budgets and data is the comparison.

Plain Python 3.11+, `asyncio` and Pydantic v2; no agent framework. The binding assumptions are in [ASSUMPTIONS.md](ASSUMPTIONS.md); the spec is in [.kiro/specs/incident-investigator/](.kiro/specs/incident-investigator/).

## Setup

Python 3.11 or later is required (development used 3.12.7).

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

API keys come only from the environment and are never written to traces, reports or logs:

- `OPENAI_API_KEY` for the OpenAI adapter (`llm.provider = "openai"`), used for every real Run.
- `ANTHROPIC_API_KEY` for the Anthropic adapter. That adapter is not built (see Incomplete Functionality); `provider = "anthropic"` stops with an error. The variable is still redacted from outputs if it is set.

`.env` and `.env.*` are git-ignored. One way to load a key for a shell session is `set -a; source .env; set +a`.

### Commands

```bash
# 1. Generate the 12 scenario snapshots and answer keys (seed 42)
incident-investigator generate --seed 42 --config config.toml

# 2. Investigate one incident (bad-config-deploy is inc-e4c5349a46f3 for seed 42)
incident-investigator investigate --incident data/snapshots/inc-e4c5349a46f3 \
    --system multi --config config.toml --out runs/inc-e4c5349a46f3-multi
incident-investigator investigate --incident data/snapshots/inc-e4c5349a46f3 \
    --system baseline --config config.toml --out runs/inc-e4c5349a46f3-baseline

# 3. Evaluate both systems on all 12 scenarios, 3 Runs each
incident-investigator eval --config config.toml --runs 3 --out runs/eval

# 4. Run the test suite (fake LLM, no key, no network)
pytest
```

`investigate` writes `report.json`, `report.md`, `trace.json` and `ledger.json` to `--out`. `eval` also takes `--scenario NAME` (repeatable), `--system multi|baseline` and `--runs 1..10`, and writes `results.md`, `results.json` and one folder per Run under `<out>/runs/<scenario>/<system>/run-<k>/`. Exit status is 0 whenever the reports are written, whatever the Verdict.

`config.fake.toml` is the same configuration with `llm.provider = "fake"`: the rule-based offline responder needs no key and makes no network call. Its results are smoke results; they do not measure real LLM accuracy.

### Configuration

One TOML file holds every field. Unknown, missing or out-of-range fields are all reported by dotted path before any Run starts. Durations are in seconds; relative paths resolve against the config file's directory. `llm.provider` and `llm.model` have no default.

| Field | Range | Default |
|---|---|---|
| `seed` | integer | 42 |
| `llm.provider` | `openai`, `fake` (`anthropic` not built) | required |
| `llm.model` | non-empty | required (`gpt-5.4-mini` in `config.toml`) |
| `llm.request_timeout_s` | > 0 | 60 |
| `llm.max_attempts` | integer ≥ 1 | 3 |
| `llm.base_delay_s` | > 0 | 1.0 |
| `llm.max_delay_s` | ≥ `base_delay_s` | 20.0 |
| `tools.retry.max_attempts` | 1–10 | 3 |
| `tools.retry.base_delay_s` | 0.01–10 | 0.5 |
| `tools.retry.max_delay_s` | `base_delay_s`–60 | 8.0 |
| `tools.circuit_breaker.failure_threshold` | 1–20 | 3 |
| `tools.circuit_breaker.cooldown_s` | 0.1–600 | 30 |
| `tools.failure_injection.random_enabled` | boolean | true |
| `tools.failure_injection.timeout_p` | 0–1 (sum of the three ≤ 1) | 0.05 |
| `tools.failure_injection.partial_p` | 0–1 | 0.05 |
| `tools.failure_injection.stale_p` | 0–1 | 0.05 |
| `orchestrator.concurrency_limit` | 1–16 | 4 |
| `orchestrator.per_task_step_limit` | integer ≥ 2 | 6 |
| `orchestrator.per_task_timeout_s` | > 0 | 180 |
| `orchestrator.pre_incident_margin_s` | ≥ 0 | 3600 |
| `orchestrator.change_lookback_s` | > 0 | 604800 (7 days) |
| `orchestrator.max_initial_tasks_per_specialist` | integer ≥ 1 | 2 |
| `orchestrator.max_evidence_requests_per_round` | 1–10 | 4 |
| `budgets.critic_rounds` | 1–3 | 3 |
| `budgets.tool_calls` | integer ≥ 1 | 60 |
| `budgets.tokens` | integer ≥ 1 | 400000 |
| `budgets.wall_clock_timeout_s` | > 0 | 900 |
| `budgets.reporting_reserve_tokens` | 0 to `tokens` − 1 | 20000 |
| `policy.data_gap_penalty` | 0–1 | 0.05 |
| `policy.conclusive_threshold` | 0–1 | 0.6 |
| `report.max_recommended_actions` | integer ≥ 1 | 6 |
| `report.observation_summary_size` | 1–20 | 5 |
| `paths.snapshot_root` | path | `data/snapshots` |
| `paths.answer_key_root` | path, neither equal to nor inside the snapshot root | `data/answer_keys` |

### OpenAI adapter

`llm/openai.py` (`openai==3.35.1`) calls the Responses API for `gpt-5.4-mini`. The request shape follows the OpenAI documentation as checked in October 2026:

- Responses API rather than Chat Completions. The [model page](https://developers.openai.com/api/docs/models/gpt-5.4-mini) lists both, OpenAI's [model guidance](https://developers.openai.com/api/docs/guides/latest-model/) points reasoning workloads to Responses, and on Chat Completions `max_completion_tokens` with `reasoning_effort="none"` was [reported](https://community.openai.com/t/gpt-5-4-ignores-reasoning-effort-none-when-max-completion-tokens-is-used/1378362) to spend the cap on reasoning tokens.
- `reasoning.effort = "none"` and `temperature = 0.0` (accepted only while effort is `none`, [openai-python #3073](https://github.com/openai/openai-python/issues/3073)); `max_output_tokens` caps each call.
- JSON mode, then Pydantic validation with one repair prompt. `store = false`.
- Token counts come from `usage.input_tokens` and `usage.output_tokens`.

The SDK client runs with `max_retries=0`. `RetryingLLMClient` retries rate-limit, server and timeout errors on the injectable clock, up to `llm.max_attempts`, waiting `min(max_delay_s, base_delay_s · 2^(k-1))` before retry `k`.

## Architecture

```mermaid
flowchart LR
  CLI["CLI<br/>generate · investigate · eval"]
  subgraph INV["Investigator"]
    ORCH["Orchestrator<br/>planner · scheduler · critic loop<br/>termination arbiter · verdict"]
    OBS["Observability_Agent"]
    DEP["Dependency_Agent"]
    REA["Reasoning_Agent"]
    CRI["Critic_Agent"]
    REP["Reporting_Agent"]
  end
  BB[("Blackboard<br/>evidence ledger + retained records<br/>hypotheses · tasks · requests<br/>data gaps · claims · budget usage")]
  subgraph TL["Tool_Layer"]
    PIPE["ToolLayer pipeline<br/>validate → breaker → budget → call"]
    FI["Failure_Injector"]
    RP["Retry_Policy"]
    CB["Circuit_Breaker"]
    TOOLS["query_logs · query_metrics<br/>get_deployments · get_dependencies<br/>search_incidents"]
    SNAP["SnapshotLoader + PathGuard"]
  end
  subgraph LLM["LLM layer"]
    GW["LLMGateway<br/>budget gate · usage · trace"]
    SOV["Structured_Output_Validator<br/>(one repair)"]
    LC["LLM_Client (retrying)"]
    OAI["OpenAI adapter"]
    ANT["Anthropic adapter<br/>(not built)"]
    FAKE["Fake_LLM<br/>scripted · offline"]
  end
  STATE["Citation_Validator · Support_Check<br/>Confidence_Policy · Verdict rule"]
  BASE["Baseline_Agent (ReAct)"]
  DRG["Deterministic_Report_Generator<br/>+ ReportAssembler"]
  MD["Markdown_Renderer"]
  DG["Data_Generator<br/>Fault_Templates · Traps · checks"]
  EV["Evaluator<br/>runner · metrics · Results_Table"]
  CLK["Injectable_Clock"]
  SR[("Snapshot_Root")]
  AKR[("Answer_Key_Root")]
  SC[("scenarios/")]

  CLI --> ORCH & BASE & DG & EV
  EV --> ORCH & BASE
  EV -. reads .-> AKR
  DG -. reads .-> SC
  DG -. writes .-> SR & AKR
  ORCH --> OBS & DEP & REA & CRI & REP
  OBS & DEP --> PIPE
  BASE --> PIPE
  PIPE --> FI & RP & CB & TOOLS
  TOOLS --> SNAP -. reads .-> SR
  PIPE --> BB
  ORCH & OBS & DEP & REA & CRI & REP <--> BB
  ORCH & OBS & DEP & REA & CRI & REP & BASE --> GW --> SOV --> LC --> OAI & FAKE
  LC -.-> ANT
  ORCH & BASE --> STATE
  ORCH & BASE --> DRG --> MD
  REP --> DRG
  CLK -.-> PIPE & LC & ORCH & BASE
```

### How the agents coordinate

- **Blackboard.** One shared state per Run: the evidence ledger, hypotheses and their revisions, tasks, Evidence_Requests, Data_Gaps, incident-report claim statuses and budget usage. Only the Tool_Layer appends evidence, and it does so from tool results, never from LLM text. Agents return structured outputs; the Orchestrator commits them after the Citation_Validator has removed any cited evidence ID that is not in the ledger. A claim that cites ledger evidence is a Fact; everything else is an Assumption.
- **Task_Graph.** The Orchestrator asks the LLM for a plan (1–2 Observability and 1–2 Dependency tasks, one Reasoning task that depends on all of them, one Critic task that depends on Reasoning, and one Reporting task). A plan that fails validation (cycle, duplicate ID, dangling dependency, wrong shape) is replaced by the Default_Task_Graph, with the cause in the trace.
- **Parallel dispatch.** An event-driven scheduler starts every ready task up to `concurrency_limit`. The Specialists run in parallel; Reasoning waits for all of them. Each attempt runs under `per_task_timeout_s`. A failed attempt is retried once; a second failure records a Data_Gap and the dependants still run.
- **Specialists.** The Observability_Agent (`query_logs`, `query_metrics`) and the Dependency_Agent (`get_deployments`, `get_dependencies`, `search_incidents`) start from code-issued opening requests, then run up to `per_task_step_limit` LLM steps, each one tool request or the findings. Tool errors come back as observations, never as agent failures.
- **Critic feedback loop.** The Reasoning_Agent ranks 2–5 competing hypotheses, each with supporting and contradicting evidence IDs. The Critic_Agent either accepts the rank-1 hypothesis or files up to 4 targeted Evidence_Requests (target Specialist, hypothesis, concern, suggested tool and parameters). It may not accept a hypothesis backed only by past incidents or only by partial or stale evidence. Each request becomes a new Specialist task, followed by a new Reasoning task that revises the hypotheses, then the next Critic round. Requests that repeat earlier work are discarded as duplicates.
- **Tool reliability.** The Failure_Injector injects timeouts, partial results and stale data deterministically from the seed. Timeouts are retried with jittered exponential backoff; a per-tool circuit breaker opens after 3 consecutive timeouts and admits one trial call after the cooldown. A request that finally fails becomes a Data_Gap, which lowers every hypothesis's confidence by `data_gap_penalty`. It never aborts the Run.
- **Budgets.** Per Run: at most 3 Critic rounds, 60 tool calls (each retry counts), 400,000 tokens (the last 20,000 reserved for the Reporting_Agent) and 900 s of wall-clock time. All waits use the injectable clock, so tests never sleep.
- **Termination.** The loop ends with exactly one Termination_Reason. When several hold at the same check, the first in this list wins:
  - `critic_accepted`: the Critic accepts the rank-1 hypothesis.
  - `max_critic_rounds`: the Critic files requests in the last allowed round; they are reported as missing evidence.
  - `duplicate_requests_only`: every request in an earlier round was a duplicate.
  - `tool_call_budget_exhausted`: the Tool_Layer rejected a request because the tool-call budget was spent.
  - `token_budget_exhausted`: token usage reached the budget minus the reporting reserve.
  - `wall_clock_timeout`: the watchdog fired; every task is cancelled and the deterministic report is written with no further LLM call.
  - `critic_failed`: the Critic task failed twice.
  - `no_hypotheses`: no hypothesis existed when the Critic would have run.
  - Baseline only: `final_answer` (a validated final answer) and `baseline_failed` (a step failed twice). The baseline can also end with `tool_call_budget_exhausted`, `token_budget_exhausted` or `wall_clock_timeout`.
- **Verdict and report.** The Verdict is conclusive only if the Critic accepted the rank-1 hypothesis (or, for the baseline, its final answer declares conclusive), the adjusted rank-1 confidence is at least `conclusive_threshold`, and rank 1 cites at least one complete, current (non-past-incident) evidence item. Otherwise it is inconclusive. The Reporting_Agent writes the recommended actions; when it fails, its reserve is short or the clock ran out, the Deterministic_Report_Generator builds the report instead. Both copy the Orchestrator's Verdict unchanged.
- **Baseline parity.** The Baseline_Agent is one ReAct loop over all five tools. It uses the same Tool_Layer, budgets, Citation_Validator, Confidence_Policy and report model. Before its first step it issues the same opening tool requests the Specialists make (`agents/opening.py`), counted against its budget. Its prompt carries the same investigation checklist as the Specialist and Reasoning prompts.

## Dataset

Generated data is not committed. It is reproduced byte for byte from `scenarios/` and one seed:

- Seed: `42`
- Command: `incident-investigator generate --seed 42 --config config.toml`

This writes one snapshot directory per scenario under `data/snapshots/` and one answer key per scenario under `data/answer_keys/`. Only the `evaluation` package reads the answer keys. Each key carries a canary token, and the tests check that it never reaches a prompt, tool result, state, trace or report.

Each incident has 8–15 services, 2–24 h of telemetry and 7–30 days of change history, plus noise and coincidental red herrings. Every scenario's generated data is checked against its catalogue condition before anything is written.

| Scenario | Root cause type | Trap or difficulty |
|---|---|---|
| bad-config-deploy | `config_error` on the affected service | none |
| database-failover | `database_failover` on the primary database | unrelated deploy 5 min before onset |
| third-party-api-outage | `third_party_outage` | noisy alerts, no changes in the window |
| slow-memory-leak | `memory_leak` upstream | innocent config change near onset; the real change is ≥ 48 h old |
| expired-tls-certificate | `certificate_expiry` upstream | noisy alerts, no deploys on the path |
| cache-stampede | `cache_stampede` | traffic spike |
| load-balancer-misconfiguration | `load_balancer_misconfiguration` | two monitors disagree |
| disk-full | `disk_full` on the primary database | look-alike past incident |
| logs-timeout | `connection_pool_exhaustion` | `query_logs` always times out |
| insufficient-evidence | none: only "inconclusive" is correct | unrelated deploy near onset |
| feature-flag-rollout | `feature_flag_regression` | errors only in one cohort |
| cascading-failure | `resource_exhaustion` upstream | the loudest service is a victim |

## Evaluation

Scoring uses the answer key's structured fields only (no LLM judge). Top-1 counts a Run as correct when the Verdict is conclusive and the rank-1 hypothesis matches the key's component and cause type, or when the key marks the evidence insufficient and the Verdict is inconclusive. An inconclusive Verdict with the right rank-1 hypothesis scores 0 and counts as over-cautious. Ranking hit@3 ignores the Verdict.

### Run

- Command: `incident-investigator eval --config config.toml --out runs/eval-full --runs 3`. The two Results_Tables were then copied from `runs/eval-full/` (git-ignored) to [`results/`](results/).
- Provider and model: OpenAI `gpt-5.4-mini`. Seed: `42`. Date: 2026-10-20.
- Code: commit `625913d3b8c24ed0bbfe1fe201ed5e867f59b353`.
- 12 scenarios × 2 systems × 3 Runs = 72 Runs: 0 failed, 0 isolation violations, 0 LLM retries, 0 output files containing the API key. There were 24 repair prompts (21 multi-agent, 3 baseline) and 1 agent failure, which its retry recovered.
- Multi-agent Runs ended with `critic_accepted` 24 times, `duplicate_requests_only` 8 and `max_critic_rounds` 4, after 1.9 Critic rounds on average. Every baseline Run ended with `final_answer`.
- Wall time 20 min 43 s. About $3.70 at list price ($0.75 per 1M input tokens and $4.50 per 1M output tokens, no cached-input discount): $3.19 multi-agent, $0.51 baseline.

### Results

Mean ± sample standard deviation over 36 Runs per system ([full table](results/results.md)):

| Metric | Multi-agent | Baseline |
|---|---|---|
| Top-1 accuracy | 0.42 ± 0.50 | 0.14 ± 0.35 |
| Top-3 accuracy | 0.50 ± 0.51 | 0.14 ± 0.35 |
| Ranking hit@3 (diagnostic) | 0.73 ± 0.45 | 0.64 ± 0.49 |
| Over-cautious rate (diagnostic) | 0.08 ± 0.28 | 0.36 ± 0.49 |
| Over-confident rate (diagnostic) | 0.28 ± 0.45 | 0.00 ± 0.00 |
| Citation precision | 0.80 ± 0.08 | 0.86 ± 0.09 |
| Key evidence recall | 0.48 ± 0.37 | 0.25 ± 0.36 |
| Tool calls | 9.6 ± 5.0 | 4.3 ± 0.7 |
| Latency (s) | 27.5 ± 11.8 | 7.0 ± 3.0 |
| Total tokens | 82,743 ± 45,144 | 11,405 ± 3,758 |

Correct Runs out of 3 per scenario (top-1, then top-3), with mean total tokens per Run:

| Scenario | Multi top-1 | Baseline top-1 | Multi top-3 | Baseline top-3 | Multi tokens | Baseline tokens |
|---|---|---|---|---|---|---|
| bad-config-deploy | 2/3 | 2/3 | 3/3 | 2/3 | 43,090 | 10,626 |
| cache-stampede | 0/3 | 0/3 | 0/3 | 0/3 | 145,958 | 10,526 |
| cascading-failure | 0/3 | 0/3 | 0/3 | 0/3 | 148,791 | 10,969 |
| database-failover | 3/3 | 0/3 | 3/3 | 0/3 | 60,939 | 19,182 |
| disk-full | 3/3 | 0/3 | 3/3 | 0/3 | 46,856 | 14,225 |
| expired-tls-certificate | 3/3 | 0/3 | 3/3 | 0/3 | 47,376 | 10,778 |
| feature-flag-rollout | 1/3 | 0/3 | 1/3 | 0/3 | 81,453 | 10,924 |
| insufficient-evidence | 2/3 | 3/3 | 2/3 | 3/3 | 69,902 | 9,993 |
| load-balancer-misconfiguration | 1/3 | 0/3 | 1/3 | 0/3 | 91,346 | 11,202 |
| logs-timeout | 0/3 | 0/3 | 0/3 | 0/3 | 60,085 | 8,609 |
| slow-memory-leak | 0/3 | 0/3 | 1/3 | 0/3 | 111,984 | 9,714 |
| third-party-api-outage | 0/3 | 0/3 | 1/3 | 0/3 | 85,140 | 10,108 |
| **All** | **15/36** | **5/36** | **18/36** | **5/36** | **82,743** | **11,405** |

### Reading the results

- **Where the multi-agent system wins.** database-failover, disk-full and expired-tls-certificate (3/3 against 0/3), plus one Run each of feature-flag-rollout and load-balancer-misconfiguration. On database-failover, disk-full, expired-tls-certificate and feature-flag-rollout the baseline's rank-1 hypothesis was right in every Run (over-cautious rate 1.0), but it declared the result inconclusive. The gap is mostly a decision gap, not a ranking gap: hit@3 is 0.73 against 0.64. The Critic loop gathers more evidence (9.6 tool calls, key evidence recall 0.48 against 0.25) and then commits.
- **Where the baseline is competitive.** bad-config-deploy (2/3 each) and insufficient-evidence, where the baseline is better (3/3 against 2/3): one multi-agent Run took the unrelated deploy near onset as a `deploy_regression`. Both systems score 0/3 on cache-stampede, cascading-failure, logs-timeout, slow-memory-leak and third-party-api-outage. The baseline also cites more precisely (0.86 against 0.80) and is never over-confident.
- **How each system fails.** The baseline gave its final answer on step 1 in all 36 Runs, using only the four opening requests, and declared 34 of them inconclusive. The multi-agent system made 10 confident wrong calls. Four named the right component with the wrong cause type (`resource_exhaustion` or `dependency_timeout` for a memory leak, `connection_pool_exhaustion` for a config error). Five blamed another service with `dependency_timeout` instead of the third-party provider or the cache. One took the insufficient-evidence trap. With `query_logs` always timing out (logs-timeout), neither system found the connection-pool cause.
- **Cost.** The multi-agent system used 7.3× the tokens (82.7k against 11.4k per Run), 4.0× the latency (27.5 s against 7.0 s) and 2.2× the tool calls. That works out to about $0.21 per correct top-1 Run, against $0.10 for the baseline.
- **Caveats.** One seed and 3 Runs per scenario give wide spreads (top-1 spread 0.50). The scenarios are synthetic and the misleading data is coincidental, not adversarial.

## Incomplete Functionality

`requirements.md` is the target spec; `tasks.md` implements an agreed reduced scope. Every criterion below is not satisfied or only partly satisfied.

### Scope cuts

- **Req 5.1, 5.2 (Anthropic part):** the Anthropic adapter is not built (optional task 7.9). `provider = "anthropic"` stops with a clear error; only OpenAI and the fake provider work.
- **Req 30.3:** of the 20 criteria labelled as properties, only 4.8 (Property 13) and 14.2 (Property 33) have property-based tests, plus Property 6 for Req 3.8. These have example tests at most: 2.5, 3.10, 6.6, 6.9, 7.3, 10.3, 13.10, 14.8, 17.5, 18.10, 21.3, 21.4, 21.8, 22.4, 22.6, 22.14, 24.6 and 27.9. 62 of the 65 design properties are unwritten.
- **Req 30.2:** core-behaviour tests only; the per-criterion traceability test for Req 4, 6, 7, 13, 14, 15 and 27.1–27.5/27.13–27.17 is not written.
- **Req 14.8:** the failure-type distribution holds by construction (one hash value mapped onto cumulative probabilities) but is not tested.
- **Req 21.8:** `report.json` and `report.md` are written from the same model, but the byte-sync property is not tested.
- **Req 24.3, 24.10:** basic path hardening only. Paths are resolved (including `..` and symlinks) and checked before reading, and overlapping roots are rejected; there is no descriptor-based open against a resolve-then-read race, and Properties 55 and 56 are unwritten.
- **Req 29.1, 29.2 (bounds):** every bound is enforced by Pydantic, but only these are tested: unknown and missing fields, `concurrency_limit` above 16, `critic_rounds` above 3, `reporting_reserve_tokens` ≥ `tokens`, the probability sum, the retry delay order and overlapping roots. The other per-field bounds in the Setup table are untested.

### Partly satisfied criteria

- **Req 1.1:** `start_time` also accepts a numeric string such as `"1700000000"`, which Pydantic parses as epoch seconds; naive timestamps are rejected.
- **Req 7.6, 29.2 (types):** integer and float config fields use Pydantic's lax mode, so `4.0`, `true` or `"4"` are coerced instead of rejected as the wrong type.
- **Req 5.12:** the scripted Fake_LLM used by tests raises an error naming the caller when its script runs out. `provider = "fake"` uses the rule-based offline responder instead; its unreachable no-rule error names the call kind, not the caller.
- **Req 12.4:** the Reporting_Agent's mitigation, fix and prevention actions must cite evidence, but the check runs before citation validation. If every cited ID is absent from the ledger, the Citation_Validator removes them and the action reaches the report with no evidence IDs.
- **Req 13.4:** `query_metrics` returns one bucket per non-empty step (as in the design); steps with no points are omitted.
- **Req 21.5, 21.6:** `report.writer.load_report` gives the line-and-column and field-path errors, but no CLI command loads a report file.
- **Req 28.5:** argparse errors print usage and exit 2. Checks after parsing (snapshot path, `--out` inside the snapshot root, unknown scenario, unreadable config) print an error naming the argument and exit 1, without the usage text.
- **Req 31.2:** the Mermaid block is not machine-checked for syntax (that needs a Mermaid CLI).
- **Req 31.12:** the fresh-clone install, `generate` and `pytest` check has not been run with Python 3.11.

### Implemented but not covered by a test

Req 1.3 (unreadable or malformed incident report), 1.5 (Reasoning claim coverage), 2.3, 2.4 (missing or invalid snapshot), 3.4 (partial and stale together), 5.4 (empty LLM output), 10.2, 11.3–11.6, 11.16, 11.17 (Critic acceptance and request rules), 12.1, 12.3, 20.2 (contributing factors), 21.3 (only headings, IDs, Verdict and reason are checked) and 22.13, 22.17 (generic-check failure paths).

### Optional tasks not done

1.12 (Properties 1, 47, 64), 5.6 (Properties 9, 30–32, 34, 35, 38, 39, 41), 5.7 (Property 36), 5.11 (Properties 37, 50–54), 6.10 (Properties 5, 7, 8, 10–12, 14, 18, 19, 27, 28, 43–45), 7.9 (Anthropic adapter), 7.10 (agent and LLM-layer tests, Properties 15, 16, 20–26), 8.7 (Properties 58–63), 9.8 (Properties 2–4, 17, 29, 40, 42, 46, 48, 57, 65 and the Req 30.2 traceability test) and 9.9 (Properties 49, 55, 56).

### Unfinished tasks and deliverables

- **Req 31.10:** no shortfall. The committed Results_Table covers all 12 scenarios with 3 Runs per system, all with the real LLM and none failed.
- **Task 12.1:** the offline smoke Results_Table (`config.fake.toml`) is not committed. The offline eval path is covered by `tests/e2e/test_eval_cli.py`.
- **Task 12.4, Req 31.7:** the coding-assistant transcript must be exported from the IDE by the user into `transcripts/`; the assistant cannot export its own session.

## External Dependencies

- **LLM API key.** Real Runs need one OpenAI API key in `OPENAI_API_KEY` (model `gpt-5.4-mini`). The Anthropic adapter is not built, so `ANTHROPIC_API_KEY` is not used for Runs.
- **No key for tests or offline runs.** `pytest` and every Run with `provider = "fake"` need no API key and open no network connection.
- **Pinned packages** (`pyproject.toml`):
  - runtime: `pydantic==2.14.1`, `openai==3.35.1`
  - dev: `pytest==9.1.1`, `pytest-asyncio==1.4.0`, `hypothesis==6.170.5`
  - build: `setuptools==84.2.0`

## Deliverables

| Deliverable | Path |
|---|---|
| Coding-assistant transcript | [`transcripts/`](transcripts/) |
| Markdown Results_Table | [`results/results.md`](results/results.md) |
| JSON Results_Table | [`results/results.json`](results/results.json) |
| Sample per-Run reports | [`results/samples/`](results/samples/) |
| Scenario catalogue | [`scenarios/`](scenarios/) |
| Test suite | [`tests/`](tests/) |
| Technical note | [`TECHNICAL_NOTE.md`](TECHNICAL_NOTE.md) |
| Assumptions | [`ASSUMPTIONS.md`](ASSUMPTIONS.md) |
| Spec (requirements, design, tasks) | [`.kiro/specs/incident-investigator/`](.kiro/specs/incident-investigator/) |
| Configurations | [`config.toml`](config.toml), [`config.fake.toml`](config.fake.toml) |
