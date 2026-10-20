# Tech

## Stack

- Python 3.11+, `asyncio` for concurrency, Pydantic v2 for every model crossing a boundary.
- No agent framework (no LangChain, LangGraph, CrewAI, AutoGen, etc.). The orchestration logic is what is assessed, so write it directly.
- Prefer the standard library: `argparse` for the CLI, `tomllib` for config, `json` for data files.
- Tests: `pytest`, `pytest-asyncio`; `hypothesis` for property-based tests (do not confuse with the `Hypothesis` domain model).
- Pin every dependency to an exact version (`==`) in `pyproject.toml`. Choose versions at implementation time and keep the README dependency list in sync.

## LLM access

- One provider-agnostic `LLMClient` interface. Provider and model are set in config.
- Delivery provider: OpenAI, model `gpt-5.4-mini`. The OpenAI adapter and `FakeLLM` are required; the Anthropic adapter is optional and, if skipped, is listed under README "Incomplete Functionality".
- `FakeLLM` returns scripted, deterministic responses with synthetic usage data and records every prompt. All automated tests use it; no network, no API key.
- The API key comes only from `OPENAI_API_KEY` (and `ANTHROPIC_API_KEY` if that adapter is built) and is never written to traces, reports or logs. `.env` and `.env.*` are git-ignored from the first commit.
- Token counts come from provider usage data.
- Tool-using prompts (Observability, Dependency, Baseline) ask for `limit` ≤ 25 and the Tool's filters on every Tool_Request, to keep token use down.

## Implementation scope

`requirements.md` and `design.md` are the target spec; `tasks.md` implements a reduced scope. Every criterion not implemented is listed by number in README "Incomplete Functionality" (Req 31.4). Property tests in scope: Blackboard concurrent appends, Failure_Injector determinism and Citation_Validator idempotence, each at 25 examples (`derandomize=True`, `database=None`). All other properties are optional, lower-priority tasks.

## Structured outputs

- Every agent output is a Pydantic model. Validate it; if invalid, retry once with a repair prompt containing the validation errors. A second failure is an agent failure (retried once at the task level, then recorded as a data gap).
- Evidence items are created by the tool layer from tool results, never by the LLM. Uncited claims are labelled assumptions automatically.

## Answer-key isolation (hard rule)

- Scenario specs live in `scenarios/`, answer keys under the Answer_Key_Root (`data/answer_keys/`), and tool-readable snapshots under the Snapshot_Root (`data/snapshots/`). Neither `scenarios/` nor the Answer_Key_Root may be inside the Snapshot_Root. Only `datagen` reads scenario specs; only `evaluation` reads answer keys.
- Only the `evaluation` package may load answer keys. Agents, tools, the orchestrator and the baseline must not import it or receive its paths.
- Each answer key carries a canary token; tests assert it never appears in prompts, tool results, state, traces or reports.
- No scenario-specific logic or scenario names in agent prompts or investigator code.

## Determinism

- One seed drives data generation, failure injection and retry jitter.
- Retry backoff, circuit-breaker cooldown and timeouts use an injectable clock so tests never really sleep.

## Common commands

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

pytest                                   # full suite, fake LLM, no key needed

incident-investigator generate --seed 42
incident-investigator investigate --incident data/snapshots/<incident_id> \
    --system multi --config config.toml --out runs/<incident_id>
incident-investigator eval --config config.toml --runs 3 --out results/
```

Use `--system baseline` for the single-agent baseline and `provider = "fake"` in config for an offline end-to-end run.
