"""Where a Run writes its outputs (Req 21.1)."""

from typing import Protocol

from ..models.blackboard import BlackboardState
from ..models.report import InvestigationReport
from ..models.trace import RunTrace


class RunOutputSink(Protocol):
    """Writes ``report.json``, ``report.md``, ``trace.json`` and ``ledger.json`` for one Run."""

    def write(self, report: InvestigationReport, trace: RunTrace, ledger: BlackboardState) -> None: ...
