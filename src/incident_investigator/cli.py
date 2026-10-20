"""Command-line interface: ``generate``, ``investigate`` and ``eval`` (Req 28).

Every check runs before any Tool_Call, LLM call or output write. Exit 0 means the outputs were
written, whatever the Verdict. Argument errors exit 2; every other failure exits 1.
"""

import argparse
import asyncio
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from .clock import SystemClock
from .llm.factory import API_KEY_ENV, ProviderUnavailable, build_llm_client
from .models.config import Config, ConfigError, load_config
from .models.run import RunInputs
from .redact import SecretRedactor
from .runtime import RunDependencies
from .tools.snapshot import SNAPSHOT_FILE, IncidentReportError, PathGuard, SnapshotLoader, SnapshotLoadError

DEFAULT_SCENARIOS = Path("scenarios")


class CliError(Exception):
    """A failure reported to the user with exit status 1."""


def _overlap(a: Path, b: Path) -> bool:
    ra, rb = Path(a).resolve(), Path(b).resolve()
    return ra == rb or ra.is_relative_to(rb) or rb.is_relative_to(ra)


def _inside(child: Path, parent: Path) -> bool:
    rc, rp = Path(child).resolve(), Path(parent).resolve()
    return rc == rp or rc.is_relative_to(rp)


def check_roots(snapshot_root: Path, answer_key_root: Path) -> None:
    """Req 24.10: the two roots must be neither equal nor nested (after resolving ``..`` and symlinks)."""
    if _overlap(snapshot_root, answer_key_root):
        raise CliError(
            f"snapshot root {snapshot_root} and answer key root {answer_key_root} must not be equal or nested"
        )


def _load_config(path: Path) -> Config:
    try:
        return load_config(path)
    except ConfigError as exc:
        raise CliError(str(exc)) from None


def _require_api_key(config: Config) -> None:
    variable = API_KEY_ENV.get(config.llm.provider)
    if variable is not None and not os.environ.get(variable):
        raise CliError(f"provider {config.llm.provider} needs the environment variable {variable}, which is not set")


# generate


def cmd_generate(args: argparse.Namespace) -> int:
    from .datagen.catalogue import ScenarioSpecError, load_catalogue
    from .datagen.generator import GenerationError, generate, write_incident

    if args.config is not None:
        config = _load_config(args.config)
        snapshot_root = args.snapshot_root or config.paths.snapshot_root
        answer_key_root = args.answer_key_root or config.paths.answer_key_root
    else:
        snapshot_root = args.snapshot_root or Path("data/snapshots")
        answer_key_root = args.answer_key_root or Path("data/answer_keys")
    check_roots(snapshot_root, answer_key_root)
    if _inside(args.scenarios, snapshot_root):
        raise CliError(f"the scenarios directory {args.scenarios} must not be inside the snapshot root")
    try:
        specs = load_catalogue(args.scenarios)
    except ScenarioSpecError as exc:
        raise CliError(str(exc)) from None
    if not specs:
        raise CliError(f"no scenario specs found in {args.scenarios}")
    for spec in specs:
        try:
            generated = generate(spec, args.seed)
        except GenerationError as exc:
            raise CliError(str(exc)) from None
        target = write_incident(generated, snapshot_root, answer_key_root)
        print(f"{spec.name}: {target}")
    return 0


# investigate


def cmd_investigate(args: argparse.Namespace) -> int:
    config = _load_config(args.config)
    snapshot_root, answer_key_root = config.paths.snapshot_root, config.paths.answer_key_root
    check_roots(snapshot_root, answer_key_root)
    incident_dir = Path(args.incident)
    if not _inside(incident_dir, snapshot_root) or not (incident_dir / SNAPSHOT_FILE).is_file():
        raise CliError(f"--incident {incident_dir} is not an incident snapshot directory under {snapshot_root}")
    if _inside(args.out, snapshot_root):
        raise CliError(f"--out {args.out} must not be inside the snapshot root {snapshot_root}")
    if args.system == "baseline":
        raise CliError("--system baseline is not available in this build yet")
    _require_api_key(config)
    clock = SystemClock()
    try:
        client = build_llm_client(config.llm, clock, os.environ)
    except ProviderUnavailable as exc:
        raise CliError(str(exc)) from None
    loader = SnapshotLoader(PathGuard(snapshot_root))
    try:
        report = loader.load_incident_report(incident_dir)
    except IncidentReportError as exc:
        raise CliError(str(exc)) from None

    from .orchestrator.run import run_investigator
    from .report.writer import FileRunOutputSink

    redactor = SecretRedactor.from_env()
    inputs = RunInputs(snapshot_dir=incident_dir, incident_report=report, config=config.for_system_under_test())
    deps = RunDependencies(llm_client=client, clock=clock, redactor=redactor)
    try:
        result = asyncio.run(run_investigator(inputs, deps, FileRunOutputSink(args.out, redactor)))
    except SnapshotLoadError as exc:
        raise CliError(str(exc)) from None
    print(f"{result.report.verdict} ({result.report.termination_reason}); outputs in {args.out}")
    return 0


# entry point


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="incident-investigator", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("generate", help="generate every scenario's snapshot, incident report and answer key")
    gen.add_argument("--seed", type=int, required=True)
    gen.add_argument("--scenarios", type=Path, default=DEFAULT_SCENARIOS)
    gen.add_argument("--snapshot-root", type=Path, default=None)
    gen.add_argument("--answer-key-root", type=Path, default=None)
    gen.add_argument("--config", type=Path, default=None, help="take the roots from this config file")
    gen.set_defaults(handler=cmd_generate)

    inv = sub.add_parser("investigate", help="investigate one incident snapshot")
    inv.add_argument("--incident", type=Path, required=True)
    inv.add_argument("--system", choices=("multi", "baseline"), required=True)
    inv.add_argument("--config", type=Path, required=True)
    inv.add_argument("--out", type=Path, required=True)
    inv.set_defaults(handler=cmd_investigate)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    redactor = SecretRedactor.from_env()
    try:
        return args.handler(args)
    except CliError as exc:
        print(f"error: {redactor.redact(str(exc))}", file=sys.stderr)
        return 1
    except Exception as exc:  # unhandled: report it without leaking secrets
        print(f"error: {type(exc).__name__}: {redactor.redact(str(exc))}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
