"""Results_Table rendering: ``results.json`` and ``results.md`` with identical values (Req 26.5, 27.8, 31.6).

Formatting: each cell is ``mean ± spread`` with both values printed to ``DECIMALS`` decimals (the JSON
keeps the exact values, so a Markdown cell is the JSON value rounded); ``unavailable`` when every Run of the cell failed;
``n/a`` when the metric does not apply (no Key_Evidence, or an insufficient-evidence key for hit@3).
"""

from pathlib import Path

from ..models.results import DIAGNOSTIC_METRICS, MetricKey, MetricStat, ResultsTable
from ..redact import SecretRedactor
from .metrics import DECIMALS

RESULTS_JSON = "results.json"
RESULTS_MD = "results.md"

METRIC_LABELS: dict[MetricKey, str] = {
    MetricKey.TOP1_ACCURACY: "Top-1 accuracy",
    MetricKey.TOP3_ACCURACY: "Top-3 accuracy",
    MetricKey.CITATION_PRECISION: "Citation precision",
    MetricKey.KEY_EVIDENCE_RECALL: "Key evidence recall",
    MetricKey.TOOL_CALLS: "Tool calls",
    MetricKey.LATENCY_S: "Latency (s)",
    MetricKey.INPUT_TOKENS: "Input tokens",
    MetricKey.OUTPUT_TOKENS: "Output tokens",
    MetricKey.TOTAL_TOKENS: "Total tokens",
    MetricKey.RANKING_HIT_AT_3: "Ranking hit@3",
    MetricKey.OVER_CAUTIOUS_RATE: "Over-cautious rate",
    MetricKey.OVER_CONFIDENT_RATE: "Over-confident rate",
}
MAIN_METRICS: tuple[MetricKey, ...] = tuple(m for m in MetricKey if m not in DIAGNOSTIC_METRICS)
DIAGNOSTICS: tuple[MetricKey, ...] = tuple(m for m in MetricKey if m in DIAGNOSTIC_METRICS)


def format_stat(stat: MetricStat) -> str:
    if stat.status == "unavailable":
        return "unavailable"
    if stat.status == "not_applicable":
        return "n/a"
    return f"{stat.mean:.{DECIMALS}f} ± {stat.spread:.{DECIMALS}f}"


def _cell(text: object) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _table(table: ResultsTable, metrics: tuple[MetricKey, ...], lines: list[str]) -> None:
    header = ["Scenario", "System", "Runs", "Failed runs", *(METRIC_LABELS[m] for m in metrics)]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "---|" * len(header))
    for row in table.rows:
        cells = [row.scenario, str(row.system), str(row.runs), str(row.failed_runs)]
        cells += [format_stat(row.metrics[m]) for m in metrics]
        lines.append("| " + " | ".join(_cell(c) for c in cells) + " |")
    lines.append("")


def render_results_markdown(table: ResultsTable) -> str:
    lines = [
        "# Evaluation results",
        "",
        f"- Provider: {table.provider}",
        f"- Model: {table.model}",
        f"- Seed: {table.seed}",
        f"- Runs per scenario: {table.runs_per_scenario}",
        f"- Evaluation date: {table.evaluation_date.isoformat()}",
        f"- Scenarios: {table.scenario_count}",
        f"- Systems: {', '.join(str(s) for s in table.systems)}",
        f"- Command: `{table.eval_command}`",
        "",
        f"Cells show mean ± spread (sample standard deviation; 0.0 for a single Run), rounded to {DECIMALS} "
        "decimals. The ALL rows pool every Run of a system across scenarios. Failed Runs count as incorrect in "
        "top-1, top-3 and ranking hit@3 and are excluded from every other metric; `unavailable` means every Run of "
        "the cell failed and `n/a` means the metric does not apply.",
        "",
    ]
    if table.provider == "fake":
        lines += [
            "These are offline smoke results from the fake provider. They check the pipeline end to end and do "
            "not measure real LLM accuracy.",
            "",
        ]
    lines += ["## Accuracy, grounding and cost", ""]
    _table(table, MAIN_METRICS, lines)
    lines += ["## Diagnostics", "", "Diagnostic metrics, reported separately from accuracy.", ""]
    _table(table, DIAGNOSTICS, lines)

    lines += ["## Failed runs", ""]
    failed = [r for r in table.runs if r.failed]
    if failed:
        lines += ["| Scenario | System | Run | Error |", "|---|---|---|---|"]
        lines += [f"| {r.scenario} | {r.system} | {r.run_index} | {_cell(r.error or '')} |" for r in failed]
    else:
        lines.append("_No entries._")
    lines.append("")

    lines += ["## Isolation violations", ""]
    leaks = [(r, v) for r in table.runs for v in r.isolation_violations]
    if leaks:
        lines += ["| Scenario | System | Run | Violation |", "|---|---|---|---|"]
        lines += [f"| {r.scenario} | {r.system} | {r.run_index} | {_cell(v)} |" for r, v in leaks]
    else:
        lines.append("_No entries._")
    return "\n".join(lines).rstrip() + "\n"


def write_results(table: ResultsTable, out_dir: Path, redactor: SecretRedactor | None = None) -> tuple[Path, Path]:
    redactor = redactor or SecretRedactor()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path, md_path = out_dir / RESULTS_JSON, out_dir / RESULTS_MD
    json_path.write_text(redactor.redact(table.model_dump_json(indent=2) + "\n"), encoding="utf-8")
    md_path.write_text(redactor.redact(render_results_markdown(table)), encoding="utf-8")
    return json_path, md_path
