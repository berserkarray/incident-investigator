"""Markdown_Renderer: a pure function of the Investigation_Report (Req 20.1, 21.2, 21.3, 21.7)."""

from collections.abc import Iterable

from ..models.base import canonical_json
from ..models.report import InvestigationReport, ReportHypothesis

NO_ENTRIES = "_No entries._"


def _ids(ids: Iterable[str]) -> str:
    ids = list(ids)
    return ", ".join(f"`{i}`" for i in ids) if ids else "none"


def _cell(text: object) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _hypotheses(lines: list[str], hypotheses: tuple[ReportHypothesis, ...]) -> None:
    if not hypotheses:
        lines.append(NO_ENTRIES)
        lines.append("")
        return
    for h in hypotheses:
        lines.append(f"### Rank {h.rank}: `{h.hypothesis_id}` {h.component} / {h.cause_type}")
        lines.append("")
        lines.append(f"- Confidence: {h.confidence:.2f} (before data-gap penalty: {h.raw_confidence:.2f})")
        lines.append(f"- Revision: {h.revision}")
        lines.append(f"- Statement: {h.statement}")
        lines.append(f"- Supporting evidence: {_ids(h.supporting_evidence_ids)}")
        lines.append(f"- Contradicting evidence: {_ids(h.contradicting_evidence_ids)}")
        lines.append(f"- Evidence sufficient: {'yes' if h.evidence_sufficient else 'no'}")
        factors = ", ".join(h.contributing_factors) if h.contributing_factors else "none"
        lines.append(f"- Contributing factors: {factors}")
        if h.missing_evidence:
            lines.append("- Missing evidence:")
            lines.extend(f"  - {m.description}" for m in h.missing_evidence)
        lines.append("")


class MarkdownRenderer:
    def render(self, report: InvestigationReport) -> str:
        lines: list[str] = []
        add = lines.append
        inc = report.incident
        add(f"# Investigation report: {inc.title}")
        add("")
        add("## Incident")
        add("")
        add(f"- Incident ID: `{inc.incident_id}`")
        add(f"- Affected service: {inc.affected_service}")
        add(f"- Start time: {inc.start_time.isoformat()}")
        add(f"- Severity: {inc.severity}")
        add(f"- System: {report.system} (report built by {report.built_by})")
        add("")
        add("## Verdict")
        add("")
        add(f"**{report.verdict}**")
        add("")
        add("## Termination")
        add("")
        add(f"- Termination reason: `{report.termination_reason}`")
        if report.budget_limit is not None:
            note = report.budget_limit
            add(f"- Limit reached: {note.limit} (configured {note.configured:g}, used {note.used_at_loop_end:g})")
        add("")
        add("## Hypotheses")
        add("")
        _hypotheses(lines, report.hypotheses)
        add("## Superseded Hypotheses")
        add("")
        _hypotheses(lines, report.superseded_hypotheses)
        add("## Facts")
        add("")
        if report.facts:
            lines.extend(f"- `{f.claim_id}` ({f.producer}): {f.text} [{_ids(f.evidence_ids)}]" for f in report.facts)
        else:
            add(NO_ENTRIES)
        add("")
        add("## Assumptions")
        add("")
        if report.assumptions:
            lines.extend(f"- `{a.claim_id}` ({a.producer}): {a.text}" for a in report.assumptions)
        else:
            add(NO_ENTRIES)
        add("")
        add("## Data Gaps")
        add("")
        if report.data_gaps:
            add("| Gap | Source | Query or task | Reason | Time range |")
            add("|---|---|---|---|---|")
            for g in report.data_gaps:
                query = canonical_json(g.query) if g.query is not None else (g.task_id or (f"step {g.step}" if g.step else ""))
                window = f"{g.affected_time_range.start.isoformat()} – {g.affected_time_range.end.isoformat()}" if g.affected_time_range else ""
                add(f"| `{g.gap_id}` | {g.source} | {_cell(query)} | {_cell(g.reason)} | {window} |")
        else:
            add(NO_ENTRIES)
        add("")
        add("## Recommended Actions")
        add("")
        if report.recommended_actions:
            for a in report.recommended_actions:
                add(f"- `{a.action_id}` [{a.category}] for `{a.hypothesis_id}`: {a.text} (evidence: {_ids(a.evidence_ids)})")
        else:
            add(NO_ENTRIES)
        add("")
        add("## Incident Report Claims")
        add("")
        if report.claim_statuses:
            add("| Claim | Text | Status | Corroborating | Contradicting |")
            add("|---|---|---|---|---|")
            for c in report.claim_statuses:
                add(
                    f"| `{c.claim_id}` | {_cell(c.text)} | {c.status} | {_ids(c.corroborating_ids)} | {_ids(c.contradicting_ids)} |"
                )
        else:
            add(NO_ENTRIES)
        add("")
        add("## Missing Evidence")
        add("")
        if report.missing_evidence:
            for m in report.missing_evidence:
                hyp = f" for `{m.hypothesis_id}`" if m.hypothesis_id else ""
                add(f"- [{m.source}]{hyp}: {m.description}")
        else:
            add(NO_ENTRIES)
        add("")
        add("## Budget Usage")
        add("")
        u = report.budget_usage
        add("| Budget | Used | Limit |")
        add("|---|---|---|")
        add(f"| Tool calls | {u.tool_calls.used:g} | {u.tool_calls.limit:g} |")
        add(f"| Tokens (input {u.input_tokens}, output {u.output_tokens}) | {u.tokens.used:g} | {u.tokens.limit:g} |")
        add(f"| Critic rounds | {u.critic_rounds.used:g} | {u.critic_rounds.limit:g} |")
        add(f"| Wall-clock seconds | {u.wall_clock_s.used:g} | {u.wall_clock_s.limit:g} |")
        add("")
        add("## Evidence Appendix")
        add("")
        if report.evidence_appendix:
            for e in report.evidence_appendix:
                obs = e.observation
                add(f"### `{e.evidence_id}` {e.tool} ({e.reliability})")
                add("")
                add(f"- Query: `{canonical_json(e.query)}`")
                add(f"- Records: {obs.record_count}{' (truncated)' if obs.truncated else ''}")
                if obs.time_span is not None:
                    add(f"- Time span: {obs.time_span.start.isoformat()} – {obs.time_span.end.isoformat()}")
                if obs.note:
                    add(f"- Note: {obs.note}")
                for top in obs.top_values:
                    add(f"  - {top.value} (x{top.count})")
                for key in sorted(obs.key_attributes):
                    add(f"- {key}: {', '.join(obs.key_attributes[key])}")
                add("")
        else:
            add(NO_ENTRIES)
            add("")
        return "\n".join(lines).rstrip() + "\n"
