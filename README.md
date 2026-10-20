# Incident Investigator

A multi-agent system that investigates a past production incident from simulated logs, metrics, deploy history, service dependencies and past incidents, compared against a single-agent ReAct baseline.

This README is a placeholder while the implementation is in progress. The full README (setup, architecture, incomplete functionality, external dependencies, deliverables) is written in task 10.4 of `.kiro/specs/incident-investigator/tasks.md`.

Binding assumptions are in [ASSUMPTIONS.md](ASSUMPTIONS.md).

## Dataset

Generated snapshots and answer keys are not committed. They are reproduced byte for byte from the scenario specs in `scenarios/` and one seed:

- Seed: `42`
- Command (from the repository root):

  ```bash
  incident-investigator generate --seed 42 --config config.toml
  ```

This writes one snapshot directory per scenario under `data/snapshots/` (the Snapshot_Root) and one answer key per scenario under `data/answer_keys/` (the Answer_Key_Root), both taken from the `[paths]` section of `config.toml`. Only the evaluator reads the answer keys.

## LLM provider

Real runs use OpenAI `gpt-5.4-mini` (`llm.provider = "openai"` in `config.toml`). The key is read only from the `OPENAI_API_KEY` environment variable. `provider = "fake"` uses the offline responder, which needs no key and makes no network calls; its results are smoke results and do not measure real accuracy.

The OpenAI adapter (`src/incident_investigator/llm/openai.py`, `openai==3.35.1`) sends each call to the Responses API. The request shape follows the OpenAI documentation for `gpt-5.4-mini` as checked in October 2026 (no API call was made to check it):

- Responses API, not Chat Completions. The [model page](https://developers.openai.com/api/docs/models/gpt-5.4-mini) lists both endpoints, and OpenAI's [model guidance](https://developers.openai.com/api/docs/guides/latest-model/) points reasoning workloads to Responses. On Chat Completions, GPT-5.4 rejects `max_tokens`, and `max_completion_tokens` combined with `reasoning_effort="none"` was [reported and confirmed](https://community.openai.com/t/gpt-5-4-ignores-reasoning-effort-none-when-max-completion-tokens-is-used/1378362) to spend the whole cap on reasoning tokens; Responses with `max_output_tokens` does not.
- `reasoning.effort = "none"`, the model's default (it supports `none`, `low`, `medium`, `high` and `xhigh`).
- `temperature = 0.0`. GPT-5.x reasoning models accept `temperature` only while reasoning effort is `none` ([openai-python #3073](https://github.com/openai/openai-python/issues/3073)).
- `max_output_tokens` caps each call's output.
- JSON mode (`text.format = {"type": "json_object"}`). Every output is then validated against its Pydantic model, with one repair prompt.
- `store = false`, so prompts and outputs are not stored on the provider side.
- Token counts come from `usage.input_tokens` and `usage.output_tokens`.

The SDK client runs with `max_retries=0`. Retries happen in `RetryingLLMClient` on the injectable clock: rate-limit, server and timeout errors, up to `llm.max_attempts` attempts, waiting `min(max_delay_s, base_delay_s * 2^(k-1))` before retry `k`, with `llm.request_timeout_s` per attempt.

The Anthropic adapter is optional and not built; `provider = "anthropic"` stops with a clear error.
