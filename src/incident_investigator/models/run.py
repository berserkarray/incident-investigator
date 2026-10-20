"""Run boundary types shared by the Investigator, the Baseline_Agent, the CLI and the Evaluator."""

from pathlib import Path

from pydantic import Field

from .base import StrictModel
from .blackboard import BlackboardState
from .config import InvestigatorConfig
from .incident import IncidentReport
from .report import InvestigationReport
from .trace import RunTrace


class RunInputs(StrictModel):
    """Everything a System_Under_Test receives. No Answer_Key content, path or scenario name (Req 24.7)."""

    snapshot_dir: Path
    incident_report: IncidentReport
    config: InvestigatorConfig


class RunResult(StrictModel):
    report: InvestigationReport
    trace: RunTrace
    ledger: BlackboardState
    elapsed_s: float = Field(ge=0)
