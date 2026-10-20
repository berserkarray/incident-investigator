"""Data_Generator: Scenario_Spec + Seed -> Incident_Snapshot, Incident_Report and Answer_Key (Req 22, 24.1).

Generation happens in memory and is verified before anything is written; a failed check writes nothing.
"""

import json
import shutil
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import ValidationError

from ..models.answer_key import AnswerKey
from ..models.base import TimeRange
from ..models.incident import IncidentReport
from ..models.scenario import ComponentRole, ScenarioSpec
from ..models.snapshot import ChangeEvent, IncidentSnapshot, LogRecord, MetricPoint, PastIncident
from ..tools.snapshot import FAILURE_PROFILE_FILE, INCIDENT_REPORT_FILE, SNAPSHOT_FILE
from . import rng as ids
from .background import background_logs, background_metrics, background_past_incidents, noise_changes, noise_logs
from .checks import generic_checks, leak_scan
from .context import GenContext
from .templates import TEMPLATES
from .topology import Topology, build_topology

EPOCH = datetime(2024, 1, 1, tzinfo=UTC)


class GenerationError(Exception):
    def __init__(self, scenario: str, seed: int, problems: list[str]):
        super().__init__(f"scenario {scenario} (seed {seed}) failed generation checks:\n" + "\n".join(f"  - {p}" for p in problems))
        self.scenario = scenario
        self.seed = seed
        self.problems = problems


@dataclass
class GeneratedIncident:
    spec: ScenarioSpec
    seed: int
    snapshot: IncidentSnapshot
    report: IncidentReport
    answer_key: AnswerKey
    topology: Topology
    onset: datetime
    lookback_start: datetime

    def snapshot_files(self) -> dict[str, str]:
        """The exact file contents written under ``<Snapshot_Root>/<incident_id>/``."""
        snapshot = self.snapshot.model_dump(mode="json", exclude={"failure_profile"})
        return {
            SNAPSHOT_FILE: _dump(snapshot),
            FAILURE_PROFILE_FILE: _dump(self.snapshot.failure_profile.model_dump(mode="json")),
            INCIDENT_REPORT_FILE: _dump(self.report.model_dump(mode="json")),
        }

    def answer_key_file(self) -> str:
        return _dump(self.answer_key.model_dump(mode="json"))


def _dump(data) -> str:
    return json.dumps(data, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def _fill(text: str, values: dict[str, str]) -> str:
    for key, value in values.items():
        text = text.replace("{" + key + "}", value)
    return text


def generate(spec: ScenarioSpec, seed: int) -> GeneratedIncident:
    topology = build_topology(ids.stream(seed, spec.name, "topology"), spec.service_count)
    window_rng = ids.stream(seed, spec.name, "window")
    window_start = EPOCH + timedelta(minutes=window_rng.randint(0, 30 * 24 * 60))
    window_end = window_start + timedelta(hours=spec.telemetry_window_hours)
    onset = window_start + timedelta(minutes=spec.symptom_onset_offset_minutes)
    report_start = onset + timedelta(minutes=spec.report.start_delay_minutes)
    ctx = GenContext(
        spec=spec,
        seed=seed,
        topology=topology,
        window_start=window_start,
        window_end=window_end,
        onset=onset,
        report_start=report_start,
    )

    template = TEMPLATES.get(spec.fault.template)
    if template is None:
        raise GenerationError(spec.name, seed, [f"fault.template: {spec.fault.template} is not available in this build"])
    if spec.trap is not None:
        raise GenerationError(spec.name, seed, ["trap: red herrings are not available in this build (task 5.8)"])
    try:
        params = template.params_model.model_validate(spec.fault.params)
    except ValidationError as exc:
        raise GenerationError(
            spec.name, seed, [f"fault.params.{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()]
        ) from None

    background_metrics(ctx)
    logs = background_logs(ctx)
    fault = template.generate(ctx, params)
    logs += fault.logs
    logs += noise_logs(ctx, fault.noise_constraints, fault.protected)
    changes = fault.changes + noise_changes(ctx, fault.noise_constraints, fault.protected)
    affected = topology.role(ComponentRole.AFFECTED)
    avoid = {affected} | ({fault.root_cause.component} if fault.root_cause else set())
    past = fault.past_incidents + background_past_incidents(ctx, avoid)

    def final(pid: str) -> str:
        return ids.record_id(seed, spec.name, pid)

    metric_points = [
        MetricPoint(
            record_id=final(ctx.metric_pid(component, metric, minute)),
            timestamp=ctx.at_minute(minute),
            service=component,
            metric_name=metric,
            value=round(value, 4),
        )
        for (component, metric), values in sorted(ctx.series.items())
        for minute, value in enumerate(values)
    ]
    snapshot = IncidentSnapshot(
        incident_id=ids.incident_id(spec.name, seed),
        telemetry_window=TimeRange(start=window_start, end=window_end),
        components=tuple(topology.components),
        dependencies=topology.dependency_edges(final),
        logs=tuple(
            sorted(
                (
                    LogRecord(
                        record_id=final(l.pid),
                        timestamp=l.timestamp,
                        service=l.service,
                        level=l.level,
                        message=l.message,
                        attributes=l.attributes,
                    )
                    for l in logs
                ),
                key=lambda r: (r.timestamp, r.record_id),
            )
        ),
        metrics=tuple(sorted(metric_points, key=lambda r: (r.timestamp, r.record_id))),
        changes=tuple(
            sorted(
                (
                    ChangeEvent(
                        record_id=final(c.pid),
                        timestamp=c.timestamp,
                        service=c.service,
                        change_type=c.change_type,
                        summary=c.summary,
                        attributes=c.attributes,
                    )
                    for c in changes
                ),
                key=lambda r: (r.timestamp, r.record_id),
            )
        ),
        past_incidents=tuple(
            sorted(
                (
                    PastIncident(
                        record_id=final(p.pid),
                        occurred_at=p.occurred_at,
                        title=p.title,
                        summary=p.summary,
                        affected_services=p.affected_services,
                        symptoms=p.symptoms,
                        cause_type=p.cause_type,
                        resolution=p.resolution,
                    )
                    for p in past
                ),
                key=lambda r: (r.occurred_at, r.record_id),
            )
        ),
        failure_profile=spec.failure_profile,
    )
    values = {"affected": affected}
    report = IncidentReport(
        title=_fill(spec.report.title, values),
        symptoms=tuple(_fill(s, values) for s in spec.report.symptoms),
        affected_service=affected,
        start_time=report_start,
        severity=spec.report.severity,
    )
    answer_key = AnswerKey(
        incident_id=snapshot.incident_id,
        scenario_name=spec.name,
        seed=seed,
        outcome="root_cause" if fault.root_cause else "insufficient_evidence",
        root_cause=fault.root_cause,
        contributing_factors=tuple(fault.contributing_factors),
        key_evidence=tuple(final(pid) for pid in fault.key_evidence),
        expected_data_gaps=tuple(fault.expected_gaps),
        decoy_cause_types=(spec.trap.decoy_cause_type,) if spec.trap else (),
        canary=ids.canary(spec.name, seed),
    )
    generated = GeneratedIncident(
        spec=spec,
        seed=seed,
        snapshot=snapshot,
        report=report,
        answer_key=answer_key,
        topology=topology,
        onset=onset,
        lookback_start=ctx.lookback_start,
    )
    problems = generic_checks(generated) + leak_scan(generated.snapshot_files(), generated)
    if problems:
        raise GenerationError(spec.name, seed, problems)
    return generated


def write_incident(g: GeneratedIncident, snapshot_root: Path, answer_key_root: Path) -> Path:
    """Write the snapshot directory atomically (temp dir + rename), then the Answer_Key."""
    snapshot_root, answer_key_root = Path(snapshot_root), Path(answer_key_root)
    snapshot_root.mkdir(parents=True, exist_ok=True)
    answer_key_root.mkdir(parents=True, exist_ok=True)
    target = snapshot_root / g.snapshot.incident_id
    staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=snapshot_root))
    try:
        for name, text in g.snapshot_files().items():
            (staging / name).write_text(text, encoding="utf-8")
        if target.exists():
            shutil.rmtree(target)
        staging.rename(target)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    (answer_key_root / f"{g.snapshot.incident_id}.json").write_text(g.answer_key_file(), encoding="utf-8")
    return target
