"""Snapshot loading behind a basic path guard (Req 2.2-2.4, 24.3).

A snapshot directory holds ``snapshot.json``, ``failure_profile.json`` and ``incident_report.json``.
The snapshot is loaded once at Run start and kept in memory, so later file changes have no effect.
"""

import json
from pathlib import Path

from pydantic import ValidationError

from ..models.incident import IncidentReport
from ..models.snapshot import FailureProfile, IncidentSnapshot

SNAPSHOT_FILE = "snapshot.json"
FAILURE_PROFILE_FILE = "failure_profile.json"
INCIDENT_REPORT_FILE = "incident_report.json"


class PathOutsideRoot(Exception):
    def __init__(self, path: Path, root: Path):
        super().__init__(f"{path} resolves outside the snapshot root {root}")
        self.path = path


class SnapshotLoadError(Exception):
    def __init__(self, path: Path, reason: str):
        super().__init__(f"cannot load incident snapshot {path}: {reason}")
        self.path = path
        self.reason = reason


class IncidentReportError(Exception):
    """The Incident_Report is unreadable, malformed or invalid. ``fields`` names each invalid field."""

    def __init__(self, path: Path, reason: str, fields: tuple[str, ...] = ()):
        detail = f" (fields: {', '.join(fields)})" if fields else ""
        super().__init__(f"invalid incident report {path}: {reason}{detail}")
        self.path = path
        self.fields = fields


class PathGuard:
    """Reads files only when they resolve (following symlinks and ``..``) inside the snapshot root."""

    def __init__(self, snapshot_root: Path):
        self.root = Path(snapshot_root).resolve()

    def check(self, path: Path) -> Path:
        resolved = Path(path).resolve()
        if not resolved.is_relative_to(self.root):
            raise PathOutsideRoot(Path(path), self.root)
        return resolved

    def read_bytes(self, path: Path) -> bytes:
        return self.check(path).read_bytes()


def _validation_fields(exc: ValidationError) -> tuple[str, ...]:
    return tuple(".".join(str(p) for p in e["loc"]) or "<root>" for e in exc.errors(include_url=False))


class SnapshotLoader:
    def __init__(self, guard: PathGuard):
        self.guard = guard

    def _read_json(self, path: Path, error: type[Exception] = SnapshotLoadError):
        try:
            raw = self.guard.read_bytes(path)
        except PathOutsideRoot as exc:
            raise SnapshotLoadError(path, str(exc)) from None
        except OSError as exc:
            raise error(path, f"cannot be read ({exc.strerror or exc})") from None
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise error(path, f"is not valid JSON ({exc})") from None

    def load(self, snapshot_dir: Path) -> IncidentSnapshot:
        snapshot_dir = Path(snapshot_dir)
        data = self._read_json(snapshot_dir / SNAPSHOT_FILE)
        profile_data = self._read_json(snapshot_dir / FAILURE_PROFILE_FILE)
        try:
            profile = FailureProfile.model_validate(profile_data)
        except ValidationError as exc:
            raise SnapshotLoadError(snapshot_dir / FAILURE_PROFILE_FILE, f"invalid failure profile: {exc}") from None
        if isinstance(data, dict):
            data = {**data, "failure_profile": profile.model_dump(mode="json")}
        try:
            return IncidentSnapshot.model_validate(data)
        except ValidationError as exc:
            raise SnapshotLoadError(snapshot_dir / SNAPSHOT_FILE, f"invalid snapshot: {exc}") from None

    def load_incident_report(self, snapshot_dir: Path) -> IncidentReport:
        path = Path(snapshot_dir) / INCIDENT_REPORT_FILE
        try:
            raw = self.guard.read_bytes(path)
        except PathOutsideRoot as exc:
            raise IncidentReportError(path, str(exc)) from None
        except OSError as exc:
            raise IncidentReportError(path, f"could not be read ({exc.strerror or exc})") from None
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise IncidentReportError(path, f"could not be parsed ({exc})") from None
        try:
            return IncidentReport.model_validate(data)
        except ValidationError as exc:
            raise IncidentReportError(path, "missing or invalid fields", _validation_fields(exc)) from None
