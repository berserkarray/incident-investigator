"""OfflineResponder: the ``provider = "fake"`` model for offline ``investigate`` and ``eval`` runs (design D4).

It reads only the ``<context>`` JSON block of each prompt and applies generic, deterministic
heuristics. It never sees Answer_Keys, scenario names or the data generator, and imports neither
``datagen`` nor ``evaluation``. Its results show the pipeline works end to end; they do not measure
real LLM accuracy.
"""

import json
from datetime import datetime, timedelta
from typing import Any

from ..models.vocab import CallKind, CauseType
from .base import LLMRequest, LLMResponse
from .context import extract_context
from .fake import RecordingMixin

# Generic symptom keywords -> cause type. Checked in order; first match wins.
CAUSE_KEYWORDS: tuple[tuple[tuple[str, ...], CauseType], ...] = (
    (("certificate", "x509", "expired cert"), CauseType.CERTIFICATE_EXPIRY),
    (("no space left", "disk full", "disk quota"), CauseType.DISK_FULL),
    (("out of memory", "oom", "heap"), CauseType.MEMORY_LEAK),
    (("connection pool", "waiting for a database connection", "too many connections"), CauseType.CONNECTION_POOL_EXHAUSTION),
    (("failover", "replica promoted", "read-only"), CauseType.DATABASE_FAILOVER),
    (("cache miss", "hit ratio"), CauseType.CACHE_STAMPEDE),
    (("rate limit", "429", "throttl"), CauseType.RATE_LIMITING),
    (("dns", "name resolution", "nxdomain"), CauseType.DNS_FAILURE),
    (("unhealthy", "health check failed", "no healthy upstream"), CauseType.LOAD_BALANCER_MISCONFIGURATION),
    (("upstream", "gateway", "timed out"), CauseType.DEPENDENCY_TIMEOUT),
)
CHANGE_CAUSE = {
    "config_change": CauseType.CONFIG_ERROR,
    "deployment": CauseType.DEPLOY_REGRESSION,
    "feature_flag_change": CauseType.FEATURE_FLAG_REGRESSION,
}
CONFIDENCES_SUPPORTED = (0.9, 0.6, 0.4, 0.3, 0.2)
CONFIDENCES_UNSUPPORTED = (0.5, 0.4, 0.3, 0.2, 0.1)
OFFLINE_LIMIT = 25


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _before(summary_value: str, start: datetime) -> bool:
    """Change summaries read ``"<change_type> <service> @<iso time>: <summary>"``."""
    try:
        stamp = summary_value.split("@", 1)[1].split(": ", 1)[0].strip()
        return datetime.fromisoformat(stamp.upper()) < start
    except (IndexError, ValueError):
        return False


def _top_values(entry: dict[str, Any]) -> list[dict[str, Any]]:
    return entry.get("observation", entry.get("summary", {})).get("top_values", [])


def _dump(value: Any) -> str:
    return json.dumps(value, sort_keys=True)


class OfflineResponder(RecordingMixin):
    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.prompts.append(request)
        kind = request.repair_of if request.call_kind is CallKind.REPAIR else request.call_kind
        context = extract_context(request.user)
        handler = {
            CallKind.SPECIALIST_FINDINGS: self._findings,
            CallKind.SPECIALIST_STEP: self._step,
            CallKind.REASONING: self._reasoning,
            CallKind.CRITIC: self._critic,
            CallKind.REPORTING: self._reporting,
        }.get(kind)
        if handler is None:
            raise ValueError(f"OfflineResponder has no rule for call kind {kind}")
        text = _dump(handler(context))
        return LLMResponse(text=text, usage=self.synthetic_usage(request, text), provider="fake")

    # specialists

    def _findings(self, ctx: dict[str, Any]) -> dict[str, Any]:
        claims = []
        for obs in ctx.get("observations", []):
            if not obs.get("evidence_id"):
                continue
            query = obs.get("query", {})
            service = query.get("service")
            if obs["tool"] == "query_logs" and service:
                errors = [t for t in obs["summary"].get("top_values", []) if t["value"].startswith("error ")]
                onset = min((t["first_seen"] for t in errors if t.get("first_seen")), default=None)
                count = sum(t["count"] for t in errors)
                claim = {
                    "kind": "observability",
                    "text": f"{service}: {count} error log records in the queried range"
                    + (f", first at {onset}" if onset else ""),
                    "service": service,
                    "signal": {"kind": "log", "level": "error", "pattern": query.get("text") or "error"},
                    "evidence_ids": [obs["evidence_id"]],
                }
                claim.update({"onset": onset} if onset else {"no_onset": True})
                claims.append(claim)
            elif obs["tool"] == "query_metrics" and service:
                stats = obs["summary"].get("metric_stats")
                changed = bool(stats) and stats["max"] > 2 * max(stats["first"], 1e-9)
                claim = {
                    "kind": "observability",
                    "text": f"{service}: {query.get('metric_name')} "
                    + (f"peaked at {stats['max']:.4g} at {stats['max_at']}" if changed else "did not change"),
                    "service": service,
                    "signal": {"kind": "metric", "metric_name": query.get("metric_name")},
                    "evidence_ids": [obs["evidence_id"]],
                }
                claim.update({"onset": stats["max_at"]} if changed else {"no_onset": True})
                claims.append(claim)
        return {"claims": claims}

    def _step(self, ctx: dict[str, Any]) -> dict[str, Any]:
        return {"reasoning": "Enough observations for this task.", "findings": self._findings(ctx)}

    # reasoning

    def _scores(self, ctx: dict[str, Any]) -> dict[str, dict[str, Any]]:
        start = _dt(ctx["incident"]["start_time"])
        scores: dict[str, dict[str, Any]] = {
            name: {"score": 0.0, "text": [], "support": [], "changes": []} for name in ctx.get("components", [])
        }
        for entry in ctx.get("evidence", []):
            if entry.get("historical"):
                continue
            query_service = entry.get("query", {}).get("service")
            for top in _top_values(entry):
                value = top["value"]
                for name, s in scores.items():
                    if name not in value:
                        continue
                    lowered = value.lower()
                    change = lowered.split(" ", 1)[0]
                    if lowered.startswith("error "):
                        s["score"] += 2
                    elif lowered.startswith("warn "):
                        s["score"] += 1
                    elif change in CHANGE_CAUSE and start is not None and _before(lowered, start):
                        s["score"] += 3
                        s["changes"].append(change)
                    s["text"].append(lowered)
                    if entry["evidence_id"] not in s["support"]:
                        s["support"].append(entry["evidence_id"])
            if query_service in scores and entry.get("observation", {}).get("record_count", 0) > 0:
                scores[query_service]["score"] += 0.5
        return scores

    @staticmethod
    def _cause(info: dict[str, Any]) -> CauseType:
        for change in info["changes"]:
            if change in CHANGE_CAUSE:
                return CHANGE_CAUSE[change]
        text = " ".join(info["text"])
        for keywords, cause in CAUSE_KEYWORDS:
            if any(k in text for k in keywords):
                return cause
        return CauseType.RESOURCE_EXHAUSTION

    def _reasoning(self, ctx: dict[str, Any]) -> dict[str, Any]:
        reliability = {e["evidence_id"]: (e["reliability"], e.get("historical", False)) for e in ctx.get("evidence", [])}
        scores = self._scores(ctx)
        ordered = sorted(scores.items(), key=lambda kv: (-kv[1]["score"], kv[0]))
        labels: list[tuple[str, CauseType, list[str]]] = []
        for name, info in ordered:
            if len(labels) == 5:
                break
            if info["score"] <= 0 and len(labels) >= 2:
                break
            labels.append((name, self._cause(info), info["support"]))
        if len(labels) < 2 and labels:
            alt = CauseType.DEPENDENCY_TIMEOUT if labels[0][1] is not CauseType.DEPENDENCY_TIMEOUT else CauseType.CAPACITY_LIMIT
            labels.append((labels[0][0], alt, []))
        rank1_supported = bool(labels) and any(
            reliability.get(e) == ("complete", False) for e in labels[0][2]
        )
        confidences = CONFIDENCES_SUPPORTED if rank1_supported else CONFIDENCES_UNSUPPORTED
        hypotheses = []
        for rank, (name, cause, support) in enumerate(labels, start=1):
            sufficient = any(reliability.get(e) == ("complete", False) for e in support)
            hypotheses.append(
                {
                    "component": name,
                    "cause_type": cause.value,
                    "statement": f"{cause.value.replace('_', ' ')} on {name}",
                    "supporting_evidence_ids": support,
                    "contradicting_evidence_ids": [],
                    "confidence": confidences[rank - 1],
                    "rank": rank,
                    "evidence_sufficient": sufficient,
                    "missing_evidence": []
                    if sufficient
                    else [{"description": f"Metrics for {name} around the reported start time", "suggested_tool": "query_metrics"}],
                }
            )
        return {"hypotheses": hypotheses, "claim_statuses": self._claim_statuses(ctx, reliability)}

    def _claim_statuses(self, ctx: dict[str, Any], reliability: dict[str, tuple[str, bool]]) -> list[dict[str, Any]]:
        incident = ctx["incident"]
        affected, start = incident["affected_service"], _dt(incident["start_time"])
        on_affected = [
            e for e in ctx.get("evidence", []) if not e.get("historical") and e.get("query", {}).get("service") == affected
        ]
        with_records = [e["evidence_id"] for e in on_affected if e.get("observation", {}).get("record_count", 0) > 0]
        with_errors = [
            e["evidence_id"] for e in on_affected if any(t["value"].startswith("error ") for t in _top_values(e))
        ]
        near_start = [
            e["evidence_id"]
            for e in on_affected
            for t in _top_values(e)
            if t["value"].startswith("error ")
            and t.get("first_seen")
            and start is not None
            and abs(_dt(t["first_seen"]) - start) <= timedelta(minutes=30)
        ]
        statuses = []
        for claim in ctx.get("claims", []):
            cited = {"symptom": with_errors, "affected_service": with_records, "start_time": near_start}.get(claim["kind"], [])
            cited = list(dict.fromkeys(cited))
            statuses.append(
                {
                    "claim_id": claim["claim_id"],
                    "status": "verified" if cited else "unverified",
                    "corroborating_ids": cited,
                    "contradicting_ids": [],
                }
            )
        return statuses

    # critic

    def _critic(self, ctx: dict[str, Any]) -> dict[str, Any]:
        hypotheses = sorted(ctx.get("hypotheses", []), key=lambda h: h["rank"])
        if not hypotheses:
            return {"accept_hypothesis_id": None, "evidence_requests": []}
        rank1 = hypotheses[0]
        info = {e["evidence_id"]: e for e in ctx.get("evidence", [])}
        cited = [info[e] for e in rank1["supporting_evidence_ids"] if e in info]
        current = [e for e in cited if not e.get("historical")]
        complete = [e for e in current if e["reliability"] == "complete"]
        start = _dt(ctx["incident"]["start_time"])
        window = {
            "start": (start - timedelta(hours=1)).isoformat(),
            "end": (start + timedelta(hours=2)).isoformat(),
        }

        def request(concern: str, tool: str, params: dict[str, Any], question: str) -> dict[str, Any]:
            return {
                "target": "observability",
                "hypothesis_id": rank1["hypothesis_id"],
                "concern": concern,
                "question": question,
                "suggested_tool": tool,
                "suggested_params": params,
                "rationale": f"The top hypothesis on {rank1['component']} needs current, complete evidence.",
            }

        component = rank1["component"]
        if not current and any(e.get("historical") for e in cited):
            logs = {"service": component, "time_range": window, "levels": ["error"], "limit": OFFLINE_LIMIT}
            return {"evidence_requests": [request("historical_only_support", "query_logs", logs, f"Do current logs of {component} show the failure?")]}
        if current and not complete:
            logs = {"service": component, "time_range": window, "levels": ["error"], "limit": OFFLINE_LIMIT - 5}
            return {"evidence_requests": [request("low_reliability_support", "query_logs", logs, f"Do complete logs of {component} confirm the failure?")]}
        if ctx.get("critic_round", 1) == 1 and len(complete) < 2:
            metrics = {
                "service": component,
                "metric_name": "error_rate",
                "time_range": window,
                "step_seconds": 600,
                "limit": OFFLINE_LIMIT,
            }
            return {"evidence_requests": [request("missing_evidence", "query_metrics", metrics, f"Did the error rate of {component} rise at the onset?")]}
        if not complete:
            logs = {"service": component, "time_range": window, "limit": OFFLINE_LIMIT}
            return {"evidence_requests": [request("missing_evidence", "query_logs", logs, f"What do the logs of {component} show?")]}
        return {"accept_hypothesis_id": rank1["hypothesis_id"]}

    # reporting

    def _reporting(self, ctx: dict[str, Any]) -> dict[str, Any]:
        hypotheses = sorted(ctx.get("hypotheses", []), key=lambda h: h["rank"])
        info = {e["evidence_id"]: e for e in ctx.get("evidence", [])}
        actions = []
        for h in hypotheses:
            support = [
                e for e in h["supporting_evidence_ids"] if e in info and info[e]["reliability"] == "complete" and not info[e]["historical"]
            ]
            if h["rank"] == 1 and support and ctx.get("verdict") == "conclusive":
                actions.append(
                    {
                        "text": f"Mitigate {h['cause_type'].replace('_', ' ')} on {h['component']} and confirm recovery.",
                        "category": "mitigation",
                        "hypothesis_id": h["hypothesis_id"],
                        "evidence_ids": support,
                    }
                )
            else:
                actions.append(
                    {
                        "text": f"Collect more evidence on {h['cause_type'].replace('_', ' ')} for {h['component']}.",
                        "category": "further_investigation",
                        "hypothesis_id": h["hypothesis_id"],
                        "evidence_ids": [],
                    }
                )
        return {"summary_claims": [], "recommended_actions": actions[: ctx.get("max_actions", 6)]}
