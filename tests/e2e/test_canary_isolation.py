"""Answer-key isolation across the catalogue (task 9.6; Req 2.5, 24.6, 24.7, 30.8).

For each catalogue scenario (seed 42), one Investigator Run and one Baseline Run with the
OfflineResponder. No canary (any case), Answer_Key_Root path (as configured or resolved) or
Scenario_Name may occur in any recorded prompt, Observation, ledger state, trace, Run input or
report, and the Snapshot_Root must be byte-for-byte unchanged.
"""

import pytest
from harness import CATALOGUE, generated_incident, run_system, tree_bytes

from incident_investigator.llm.offline import OfflineResponder
from incident_investigator.models.config import load_config

SEED = 42

KEY_ROOT_AS_CONFIGURED = "answer_keys"
CONFIG = f"""
seed = {SEED}
[llm]
provider = "fake"
model = "offline-responder"
[paths]
snapshot_root = "snapshots"
answer_key_root = "{KEY_ROOT_AS_CONFIGURED}"
"""


def cases():
    for scenario in CATALOGUE:
        for system in ("multi", "baseline"):
            yield pytest.param(scenario, system, id=f"{scenario}-{system}")


@pytest.mark.parametrize(("scenario", "system"), list(cases()))
async def test_isolation_across_every_artifact(scenario, system, tmp_path_factory):
    # A neutral directory name: tmp_path embeds the test ID, which would put the scenario name in every path.
    tmp_path = tmp_path_factory.mktemp("iso")
    root = tmp_path / "data"
    incident = generated_incident(root, scenario, SEED)
    (root / "config.toml").write_text(CONFIG, encoding="utf-8")
    config = load_config(root / "config.toml")
    assert config.paths.answer_key_root == incident.answer_key_root
    before = tree_bytes(incident.snapshot_root)

    outcome = await run_system(system, incident, OfflineResponder(), tmp_path / "out", config=config)

    assert outcome.writes == 1
    assert outcome.prompts, "the offline Run should make LLM calls"
    assert outcome.observations, "the offline Run should make Tool_Requests"
    key_root = incident.answer_key_root
    forbidden = {
        "canary": incident.canary,
        "answer key root as configured": KEY_ROOT_AS_CONFIGURED,
        "answer key root": str(key_root),
        "answer key root resolved": str(key_root.resolve()),
        "scenario name": scenario,
    }
    texts = outcome.artifact_texts()
    assert {"report.json", "report.md", "trace.json", "ledger.json", "ledger state", "trace", "run inputs"} <= set(texts)
    for artifact, text in texts.items():
        lowered = text.lower()
        for label, term in forbidden.items():
            assert term.lower() not in lowered, f"{label} {term!r} occurs in {artifact}"
    assert tree_bytes(incident.snapshot_root) == before, "a Run must leave the Snapshot_Root unchanged (Req 2.5)"


async def test_canary_scan_detects_a_leak(tmp_path):
    """Guard against a vacuous scan: the same check flags a canary planted in a recorded prompt."""
    incident = generated_incident(tmp_path / "data", "bad-config-deploy", SEED)
    llm = OfflineResponder()
    outcome = await run_system("multi", incident, llm, tmp_path / "out")
    planted = outcome.prompts[0].model_copy(update={"user": outcome.prompts[0].user + incident.canary.upper()})
    outcome.prompts[0] = planted
    leaks = [name for name, text in outcome.artifact_texts().items() if incident.canary.lower() in text.lower()]
    assert leaks == [next(iter(outcome.artifact_texts()))]
