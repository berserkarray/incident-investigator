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
