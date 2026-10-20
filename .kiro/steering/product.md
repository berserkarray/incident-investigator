# Product

## Goal

A multi-agent system that investigates a past production incident from simulated logs, metrics, deploy history, service dependencies and past incidents. It finds the probable root cause, or honestly reports "inconclusive", and recommends evidence-backed fixes. It is compared against a single-agent ReAct baseline that uses the same model, tools, budgets and data.

## Users

- Assignment reviewers assessing the architecture, reliability and evaluation.
- SREs reading the generated incident reports.

## What is assessed

- Genuine agent collaboration: one shared evidence board, Critic-driven evidence requests back to specialists, hypotheses revised as evidence arrives. Chained prompts are not enough.
- Orchestration and reliability: parallel independent tasks, task dependencies, bounded retries, circuit breaker, execution limits, termination conditions, recovery from tool and agent failures.
- Evidence-based conclusions: ranked hypotheses with supporting and contradicting evidence IDs, confidence, facts vs assumptions, data gaps, and actions tied to hypotheses. "Inconclusive" when evidence is insufficient.
- Honest evaluation: 12 synthetic scenarios with traps, an answer key only the evaluator reads, and metrics for accuracy (top-1/top-3), evidence grounding, tool calls, latency and tokens, 3 runs per incident.
- No hardcoded answers for any scenario.

## Assumptions summary

- Post-incident, read-only analysis of a fixed synthetic snapshot; the incident report is a set of claims to verify.
- 8–15 services per incident; telemetry covers 2–24 hours and change history reaches back 7–30 days; one primary root cause; misleading data is coincidental, never adversarial.
- All six agents are built. Facts are tool outputs; everything else is an assumption.
- Tool failures are seeded and deterministic; a permanently failed tool becomes a data gap and never aborts the run.
- CLI only (JSON + Markdown). Real runs need one LLM API key; tests use a fake LLM.

The full, binding list:

#[[file:ASSUMPTIONS.md]]
