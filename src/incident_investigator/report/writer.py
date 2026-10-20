"""Where a Run writes its outputs (Req 21.1, 21.5, 21.6)."""

import json
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from ..models.blackboard import BlackboardState
from ..models.report import InvestigationReport
from ..models.trace import RunTrace
from ..redact import SecretRedactor
from .markdown import MarkdownRenderer

REPORT_JSON = "report.json"
REPORT_MD = "report.md"
TRACE_JSON = "trace.json"
LEDGER_JSON = "ledger.json"


class RunOutputSink(Protocol):
    """Writes ``report.json``, ``report.md``, ``trace.json`` and ``ledger.json`` for one Run."""

    def write(self, report: InvestigationReport, trace: RunTrace, ledger: BlackboardState) -> None: ...


class FileRunOutputSink:
    def __init__(self, out_dir: Path, redactor: SecretRedactor | None = None):
        self.out_dir = Path(out_dir)
        self._redactor = redactor or SecretRedactor()
        self._renderer = MarkdownRenderer()
        self.written = 0

    def _write(self, name: str, text: str) -> None:
        (self.out_dir / name).write_text(self._redactor.redact(text), encoding="utf-8")

    def write(self, report: InvestigationReport, trace: RunTrace, ledger: BlackboardState) -> None:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self._write(REPORT_JSON, report.model_dump_json(indent=2) + "\n")
        self._write(REPORT_MD, self._renderer.render(report))
        self._write(TRACE_JSON, trace.model_dump_json(indent=2) + "\n")
        self._write(LEDGER_JSON, ledger.model_dump_json(indent=2) + "\n")
        self.written += 1


class MemoryRunOutputSink:
    """Keeps outputs in memory; used by tests."""

    def __init__(self) -> None:
        self.outputs: list[tuple[InvestigationReport, RunTrace, BlackboardState]] = []

    def write(self, report: InvestigationReport, trace: RunTrace, ledger: BlackboardState) -> None:
        self.outputs.append((report, trace, ledger))


class ReportFileError(Exception):
    def __init__(self, path: Path, problem: str):
        super().__init__(f"{path}: {problem}")
        self.path = path


def load_report(path: Path) -> InvestigationReport:
    """Parse a JSON_Report; errors name the line and column, or each invalid field path."""
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ReportFileError(path, f"invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}") from None
    try:
        return InvestigationReport.model_validate(data)
    except ValidationError as exc:
        fields = ", ".join(".".join(str(p) for p in e["loc"]) for e in exc.errors(include_url=False))
        raise ReportFileError(path, f"schema validation failed for: {fields}") from None
