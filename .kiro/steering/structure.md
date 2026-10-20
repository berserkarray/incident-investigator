# Structure

## Proposed layout

```
incident-investigator/
├── pyproject.toml            # pinned deps, console script `incident-investigator`
├── config.toml               # example config (provider, model, budgets, seed)
├── README.md                 # setup, Mermaid diagram, coordination, deps, incomplete items
├── ASSUMPTIONS.md
├── TECHNICAL_NOTE.md         # ≤500 words
├── src/incident_investigator/
│   ├── models/               # shared Pydantic models: incident, evidence, hypothesis,
│   │                         # tasks, gaps, budgets, tool I/O, report, config
│   ├── llm/                  # LLMClient interface, anthropic.py, openai.py, fake.py,
│   │                         # structured output + repair retry
│   ├── tools/                # five tools, snapshot loader, failure injector,
│   │                         # retry/backoff, circuit breaker
│   ├── state/                # blackboard, budgets, citation validator, confidence policy
│   ├── orchestrator/         # task graph, scheduler, critic loop, termination, verdict
│   ├── agents/               # observability, dependency, reasoning, critic, reporting + prompts
│   ├── baseline/             # single-agent ReAct loop
│   ├── report/               # JSON + Markdown rendering, deterministic report generator
│   ├── datagen/              # scenario spec schema, fault templates, generator
│   ├── evaluation/           # ONLY place that loads answer keys; runner, metrics, results table
│   └── cli.py                # generate / investigate / eval
├── scenarios/                # scenario specs (generator input; outside snapshot root)
├── data/
│   ├── snapshots/<incident_id>/   # snapshot root: tool-readable data + failure profile
│   └── answer_keys/<incident_id>.json   # answer-key root: evaluator only
├── results/                  # evaluation results table + per-run reports and traces
├── transcripts/              # coding assistant transcript
└── tests/                    # pytest, fake LLM only
```

Dependency direction: `models` ← everything; `evaluation` may import anything; nothing imports `evaluation` except `cli`.

## Work split and branching

1. **Shared foundation first, on `main`:** `models/`, the `LLMClient` and tool interfaces, the fake LLM skeleton and project packaging. Interfaces change only through `main`.
2. **Then parallel workstreams, each on its own branch and worktree** (`git worktree add ../ii-<name> -b ws/<name>`). Each worktree gets its own `.venv` with `pip install -e ".[dev]"` of that worktree; never reuse the main folder's `.venv`, whose editable install points at the main folder's `src`. Workstreams:
   - `ws/tools-data`: tools, failure injection, retry, circuit breaker, data generator and scenarios;
   - `ws/orchestrator-state`: blackboard, task graph, scheduler, critic loop, budgets, termination;
   - `ws/agents`: the six agents and prompts;
   - `ws/eval-baseline`: ReAct baseline, evaluation runner, metrics, results table;
   - `ws/tests`: cross-cutting tests (isolation, termination, end-to-end with fake LLM).
3. **Merge only when tests pass** on the branch, rebased on the latest `main`, and again after the merge.
