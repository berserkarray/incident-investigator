# Technical Note

## Key Decisions

- No agent framework. Planner, scheduler, Critic loop, retries, circuit breaker, budgets and termination are plain asyncio code on an injectable clock, tested without real sleeps.
- One Blackboard. Only the Tool_Layer creates evidence, from tool results; the Orchestrator commits validated Pydantic outputs. A Fact is something a tool returned; everything else is an Assumption.
- Collaboration through the Critic. It accepts the rank-1 hypothesis or sends targeted Evidence_Requests back to the Specialists, which triggers new evidence and a Reasoning revision, for up to 3 rounds.
- Scoring uses the answer key's structured fields. Only `evaluation` reads answer keys; canary scans cover every prompt and artifact.
- Baseline parity. After the first real smoke run the baseline stopped after 1–3 tool calls. Both systems now share `agents/opening.py` (the same four opening tool requests, same Tool_Layer and budget) and one investigation checklist. The checklist replaced overlapping sentences in the Specialist and Reasoning prompts, so the multi-agent prompts changed too.

## Trade-offs

- Multi-agent cost: 7.3× the tokens (82.7k vs 11.4k per Run) and 4× the latency (27.5 s vs 7.0 s) of the baseline, for 0.42 vs 0.14 top-1 accuracy.
- Seeded failure injection gives both systems identical failures, but real outages fail in correlated ways.
- One repair prompt bounds cost; a second invalid output becomes an agent failure and a Data_Gap.
- A conclusive Verdict needs Critic acceptance plus complete, current support. The multi-agent system still made 10 confident wrong calls in 36 Runs: four with the right component but the wrong cause type, five blaming another service.

## Limitations

- The Confidence_Policy applies one uniform per-Data_Gap penalty to every Hypothesis regardless of the Data_Gap's relevance to that Hypothesis.
- The model invents evidence IDs. In the multi-agent Runs the Citation_Validator removed 254 cited IDs not in the ledger: 233 finding IDs (`F005`), 16 Data_Gap IDs (`G001`) and 5 evidence IDs that did not exist when cited. It also restored 14 claim statuses left without sufficient citations. This keeps reports honest but can leave a hypothesis under-supported.
- The baseline answered on step 1 in all 36 Runs and declared 34 inconclusive, often with the right rank-1 hypothesis. The comparison mostly measures "investigate further, then commit" against "answer from the openings".
- The offline FakeLLM (`provider = "fake"`) is a rule-based smoke test of the pipeline; its results do not measure real accuracy.
- Twelve synthetic scenarios, one seed and 3 Runs each give wide spreads. Only `gpt-5.4-mini` was evaluated; the Anthropic adapter is not built.

## Potential Improvements

- Weight each Data_Gap penalty by its overlap with the hypothesis's components and time range.
- Send a repair prompt naming absent or non-evidence IDs instead of silently removing them.
- Let the Critic challenge the cause type, not only the component, with cause-specific checks (memory trend, provider errors).
- Evaluate more seeds, models and correlated failure modes.
- Write the remaining property tests and the Anthropic adapter.
