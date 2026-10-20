# Technical Note

## Key Decisions

- No agent framework. Planner, scheduler, Critic loop, retries, circuit breaker, budgets and termination are plain asyncio code on an injectable clock, tested without real sleeps.
- One Blackboard. Only the Tool_Layer creates evidence, from tool results; the Orchestrator commits validated Pydantic outputs. A Fact is something a tool returned; everything else is an Assumption.
- Collaboration through the Critic: it accepts the rank-1 hypothesis or sends targeted Evidence_Requests back to the Specialists, then Reasoning revises, for up to 3 rounds.
- Scoring uses the answer key's structured fields. Only `evaluation` reads answer keys; canary scans cover every prompt and artifact.
- Baseline parity. Both systems share `agents/opening.py` (four opening tool requests, same Tool_Layer and budget) and one investigation checklist, which also replaced sentences in the multi-agent prompts. A baseline-only rule makes it query its listed missing evidence before declaring inconclusive (in v1 it always answered at step 1).

## Trade-offs

- Multi-agent cost: 2.2× the tokens (63.9k vs 29.7k per Run) and 2.7× the latency (24.8 s vs 9.0 s) for the same top-1 accuracy (0.47 each, 17/36).
- Seeded failure injection gives both systems identical failures, but real outages fail in correlated ways.
- One repair prompt bounds cost; a second invalid output becomes an agent failure and a Data_Gap.
- A conclusive Verdict needs Critic acceptance plus complete, current support. The multi-agent system still made 12 confident wrong calls in 36 Runs against the baseline's 5: right component with wrong cause type, another service blamed, or the coincidental deploy.

## Limitations

- The Confidence_Policy applies one uniform per-Data_Gap penalty to every Hypothesis regardless of the Data_Gap's relevance to that Hypothesis.
- The model mixed ID types more than it invented IDs. In v1, 249 of 254 removed citations were finding IDs (`F005`) or Data_Gap IDs (`G001`) in evidence fields; only 5 were evidence IDs that did not exist when cited. Once every prompt said only `E…` IDs are evidence and the board prompts labelled each ID type, removals fell to 1. Stripping such IDs can leave a hypothesis under-supported.
- The baseline rule is a prompt, not machinery: 6 of 36 baseline Runs still answered at step 1, 4 of them inconclusive.
- The offline FakeLLM (`provider = "fake"`) is a rule-based smoke test of the pipeline; its results do not measure real accuracy.
- Twelve synthetic scenarios, one seed and 3 Runs each give wide spreads (top-1 ± 0.51). Only `gpt-5.4-mini` was evaluated.

## Potential Improvements

- Weight each Data_Gap penalty by its overlap with the hypothesis's components and time range.
- Reject non-evidence IDs in evidence fields with a repair prompt instead of silently removing them.
- Let the Critic challenge the cause type, not only the component, with cause-specific checks (memory trend, provider errors), and make it doubt changes that merely coincide with onset.
- Evaluate more seeds, models and correlated failure modes.
- Write the remaining property tests and the Anthropic adapter.
