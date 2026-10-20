"""OfflineResponder: the ``provider = "fake"`` model for offline ``investigate`` and ``eval`` runs (design D4).

It reads only the ``<context>`` JSON block of each prompt and applies generic, deterministic
heuristics. It never sees Answer_Keys, scenario names or the data generator, and imports neither
``datagen`` nor ``evaluation``. Its results show the pipeline works end to end; they do not measure
real LLM accuracy.

Rules per call kind:

- ``plan``: one Observability and one Dependency Task, then Reasoning, Critic and Reporting (the
  Default_Task_Graph shape).
- ``specialist_step`` / ``specialist_findings``: finish at once with one Claim per non-error
  Observation of the Task. Log claims take the service from the query and the onset from the first
  ERROR record (no-onset without ERROR records); metric claims take the onset from the first bucket
  past half of the rise when the series at least doubles; change, dependency and past-incident claims
  take their Components and event time from the returned records.
- ``reasoning``: score each Component named in evidence summaries (+2 per ERROR-level top value, +3
  per change before the reported start, +1 per dependency mention; +0.5 tie-breaker for a queried
  service with records), map the cause type with a generic keyword table over the summary text
  (fallback ``resource_exhaustion``), and emit the top 2-5 labels with descending confidence, cited
  from the same summaries.
- ``critic``: the Req 11.6 / 11.17 rules first; in round 1 request ``query_metrics(error_rate)`` for
  the rank-1 Component when it has fewer than two complete, current supporting items; later rounds
  request logs while there is no complete support; otherwise accept.
- ``reporting``: one ``mitigation`` for a supported rank 1 of a conclusive Run, ``further_investigation``
  for the rest.
- ``baseline_step``: a fixed probe sequence (logs, metrics, deployments, dependencies, incidents for the
  first symptom), then a final answer built with the ``reasoning`` heuristic.

Every Tool request it emits uses ``limit`` <= 25.
"""

import json
import re
from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any

from ..models.vocab import CallKind, CauseType
from .base import LLMRequest, LLMResponse
from .context import extract_context
from .fake import RecordingMixin

OFFLINE_LIMIT = 25
MAX_CLAIMS = 20
MAX_HYPOTHESES = 5
PROBE_BEFORE = timedelta(hours=1)
PROBE_AFTER = timedelta(hours=2)
PROBE_LOOKBACK = timedelta(days=7)
PROBE_DEPENDENCY_DEPTH = 3

# Generic symptom keywords -> cause type, matched at word starts in lower-cased summary text.
# Checked in order; the first match wins. Change-type words sit after the specific symptoms so a
# coincidental change does not hide a clear symptom.
CAUSE_KEYWORDS: tuple[tuple[tuple[str, ...], CauseType], ...] = (
    (("certificate", "tls handshake", "x.509", "cert expired"), CauseType.CERTIFICATE_EXPIRY),
    (("no space left", "disk full", "disk quota"), CauseType.DISK_FULL),
    (("out of memory", "oom", "heap"), CauseType.MEMORY_LEAK),
    (("connection pool", "waiting for a database connection", "too many connections"), CauseType.CONNECTION_POOL_EXHAUSTION),
    (("failover", "replica promoted", "read-only"), CauseType.DATABASE_FAILOVER),
    (("cache miss", "hit ratio", "stampede"), CauseType.CACHE_STAMPEDE),
    (("rate limit", "too many requests", "throttl"), CauseType.RATE_LIMITING),
    (("dns", "name resolution", "nxdomain"), CauseType.DNS_FAILURE),
    (("unhealthy", "health check failed", "no healthy upstream"), CauseType.LOAD_BALANCER_MISCONFIGURATION),
    (("feature_flag_change",), CauseType.FEATURE_FLAG_REGRESSION),
    (("config_change",), CauseType.CONFIG_ERROR),
    (("deployment",), CauseType.DEPLOY_REGRESSION),
    (("upstream", "gateway", "timed out", "timeout"), CauseType.DEPENDENCY_TIMEOUT),
)
CHANGE_TYPES = ("deployment", "config_change", "feature_flag_change")
CONFIDENCES_SUPPORTED = (0.9, 0.6, 0.4, 0.3, 0.2)
CONFIDENCES_UNSUPPORTED = (0.5, 0.4, 0.3, 0.2, 0.1)


# helpers


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _dump(value: Any) -> str:
    return json.dumps(value, sort_keys=True)


def _mentions(text: str, name: str) -> bool:
    """``name`` occurs in ``text`` as a whole Component name (not inside a longer name)."""
    return re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", text) is not None


def _keyword(text: str, keyword: str) -> bool:
    return re.search(rf"(?<![\w]){re.escape(keyword)}", text) is not None


def _change_before(value: str, start: datetime | None) -> bool:
    """Change summaries read ``"<change_type> <service> @<iso time>: <summary>"``."""
    if start is None:
        return False
    try:
        stamp = value.split("@", 1)[1].split(": ", 1)[0].strip()
        return datetime.fromisoformat(stamp) < start
    except (IndexError, ValueError):
        return False


def _top_values(entry: dict[str, Any]) -> list[dict[str, Any]]:
    return entry.get("observation", entry.get("summary", {})).get("top_values", [])


def _record_count(entry: dict[str, Any]) -> int:
    return entry.get("observation", entry.get("summary", {})).get("record_count", 0)


def _records(obs: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    return [r for r in obs.get("records", []) if r.get("kind") == kind]


def _window(start: datetime, before: timedelta, after: timedelta) -> dict[str, str]:
    return {"start": (start - before).isoformat(), "end": (start + after).isoformat()}


def _complete_current(entry: dict[str, Any] | None) -> bool:
    return entry is not None and entry.get("reliability") == "complete" and not entry.get("historical")


class OfflineResponder(RecordingMixin):
    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.prompts.append(request)
        kind = request.repair_of if request.call_kind is CallKind.REPAIR else request.call_kind
        context = extract_context(request.user)
        handler = {
            CallKind.PLAN: self._plan,
            CallKind.SPECIALIST_FINDINGS: self._findings,
            CallKind.SPECIALIST_STEP: self._step,
            CallKind.REASONING: self._reasoning,
            CallKind.CRITIC: self._critic,
            CallKind.REPORTING: self._reporting,
            CallKind.BASELINE_STEP: self._baseline_step,
        }.get(kind)
        if handler is None:
            raise ValueError(f"OfflineResponder has no rule for call kind {kind}")
        text = _dump(handler(context))
        return LLMResponse(text=text, usage=self.synthetic_usage(request, text), provider="fake")

    # plan

    def _plan(self, ctx: dict[str, Any]) -> dict[str, Any]:
        return {
            "tasks": [
                {"task_id": "observability-1", "agent": "observability", "focus": "symptoms and onset in logs and metrics"},
                {"task_id": "dependency-1", "agent": "dependency", "focus": "recent changes, dependencies and history"},
                {"task_id": "reasoning-1", "agent": "reasoning", "depends_on": ["observability-1", "dependency-1"]},
                {"task_id": "critic-1", "agent": "critic", "depends_on": ["reasoning-1"]},
                {"task_id": "reporting-1", "agent": "reporting"},
            ]
        }

    # specialists

    def _step(self, ctx: dict[str, Any]) -> dict[str, Any]:
        return {"reasoning": "The code-issued observations are enough for this task.", "findings": self._findings(ctx)}

    def _findings(self, ctx: dict[str, Any]) -> dict[str, Any]:
        role = ctx.get("role")
        components = set(ctx.get("components", []))
        claims = []
        for obs in ctx.get("observations", []):
            if not obs.get("evidence_id") or obs.get("error"):
                continue
            claim = self._claim(obs, components)
            if claim is not None and (claim["kind"] == "observability") == (role == "observability"):
                claims.append(claim)
        return {"claims": claims[:MAX_CLAIMS]}

    def _claim(self, obs: dict[str, Any], components: set[str]) -> dict[str, Any] | None:
        return {
            "query_logs": self._log_claim,
            "query_metrics": self._metric_claim,
            "get_deployments": self._change_claim,
            "get_dependencies": self._dependency_claim,
            "search_incidents": self._incident_claim,
        }.get(obs.get("tool"), lambda o, c: None)(obs, components)

    @staticmethod
    def _log_claim(obs: dict[str, Any], components: set[str]) -> dict[str, Any] | None:
        query = obs.get("query", {})
        service = query.get("service")
        if service not in components:
            return None
        errors = [r for r in _records(obs, "log") if r.get("level") == "error"]
        claim = {
            "kind": "observability",
            "service": service,
            "signal": {"kind": "log", "level": "error", "pattern": query.get("text") or "error"},
            "evidence_ids": [obs["evidence_id"]],
        }
        if errors:
            onset = min((_dt(r["timestamp"]) for r in errors), default=None)
            claim["text"] = f"{service}: {len(errors)} error log records in the queried range, first at {onset.isoformat()}"
            claim["onset"] = onset.isoformat()
        else:
            claim["text"] = f"{service}: no error log records in the queried range"
            claim["no_onset"] = True
        return claim

    @staticmethod
    def _metric_claim(obs: dict[str, Any], components: set[str]) -> dict[str, Any] | None:
        query = obs.get("query", {})
        service, metric = query.get("service"), query.get("metric_name")
        if service not in components or not metric:
            return None
        buckets = sorted(_records(obs, "metric_bucket"), key=lambda b: _dt(b["timestamp"]))
        claim = {
            "kind": "observability",
            "service": service,
            "signal": {"kind": "metric", "metric_name": metric},
            "evidence_ids": [obs["evidence_id"]],
        }
        onset = None
        if len(buckets) >= 2:
            first = buckets[0]["mean"]
            peak = max(b["mean"] for b in buckets)
            if peak > 2 * max(first, 1e-9):
                halfway = first + (peak - first) / 2
                onset = next(b["timestamp"] for b in buckets if b["mean"] >= halfway)
                claim["text"] = f"{service}: {metric} rose from {first:.4g} to {peak:.4g}, past halfway at {onset}"
        if onset is None:
            claim["text"] = f"{service}: {metric} did not change over the queried range"
            claim["no_onset"] = True
        else:
            claim["onset"] = onset
        return claim

    @staticmethod
    def _change_claim(obs: dict[str, Any], components: set[str]) -> dict[str, Any] | None:
        changes = [r for r in _records(obs, "change") if r.get("service") in components]
        if not changes:
            return None
        latest = max(changes, key=lambda r: (_dt(r["timestamp"]), r["record_id"]))
        return {
            "kind": "dependency",
            "text": f"{latest['change_type']} on {latest['service']} at {latest['timestamp']}: {latest['summary']}"[:500],
            "components": [latest["service"]],
            "finding_type": latest["change_type"],
            "event_time": latest["timestamp"],
            "historical": False,
            "evidence_ids": [obs["evidence_id"]],
        }

    @staticmethod
    def _dependency_claim(obs: dict[str, Any], components: set[str]) -> dict[str, Any] | None:
        service = obs.get("query", {}).get("service")
        if service not in components:
            return None
        records = [r for r in _records(obs, "dependency") if r.get("component") in components]
        upstream = sorted({r["component"] for r in records if r.get("direction") == "upstream"})
        downstream = sorted({r["component"] for r in records if r.get("direction") == "downstream"})
        named = [service] + [n for n in sorted({*upstream, *downstream}) if n != service][:9]
        text = (
            f"{service} depends on {', '.join(upstream) or 'no upstream component'}; "
            f"{', '.join(downstream) or 'no component'} depends on it"
        )
        return {
            "kind": "dependency",
            "text": text[:500],
            "components": named,
            "finding_type": "dependency_relation",
            "historical": False,
            "evidence_ids": [obs["evidence_id"]],
        }

    @staticmethod
    def _incident_claim(obs: dict[str, Any], components: set[str]) -> dict[str, Any] | None:
        incidents = _records(obs, "past_incident")
        if not incidents:
            return None
        first = incidents[0]
        named = [s for s in first.get("affected_services", []) if s in components]
        if not named and obs.get("query", {}).get("service") in components:
            named = [obs["query"]["service"]]
        if not named:
            return None
        return {
            "kind": "dependency",
            "text": f"Past incident {first['record_id']} at {first['occurred_at']}: {first['title']}"[:500],
            "components": named,
            "finding_type": "past_incident",
            "event_time": first["occurred_at"],
            "historical": True,
            "evidence_ids": [obs["evidence_id"]],
        }

    # reasoning

    def _scores(self, ctx: dict[str, Any]) -> dict[str, dict[str, Any]]:
        start = _dt(ctx["incident"]["start_time"])
        names = list(ctx.get("components", []))
        scores: dict[str, dict[str, Any]] = {n: {"score": 0.0, "text": [], "support": []} for n in names}
        for entry in ctx.get("evidence", []):
            if entry.get("historical"):
                continue
            tool = entry.get("tool")
            for top in _top_values(entry):
                value = top["value"]
                lowered = value.lower()
                if tool == "query_logs" and lowered.startswith("error "):
                    points = 2.0
                elif tool == "get_deployments" and lowered.split(" ", 1)[0] in CHANGE_TYPES and _change_before(value, start):
                    points = 3.0
                elif tool == "get_dependencies":
                    points = 1.0
                else:
                    points = 0.0
                for name in names:
                    if not _mentions(value, name):
                        continue
                    info = scores[name]
                    info["score"] += points
                    info["text"].append(lowered)
                    if entry["evidence_id"] not in info["support"]:
                        info["support"].append(entry["evidence_id"])
            service = entry.get("query", {}).get("service")
            if service in scores and _record_count(entry) > 0:
                scores[service]["score"] += 0.5
                if entry["evidence_id"] not in scores[service]["support"]:
                    scores[service]["support"].append(entry["evidence_id"])
        return scores

    @staticmethod
    def _cause(info: dict[str, Any]) -> CauseType:
        text = " ".join(info["text"])
        for keywords, cause in CAUSE_KEYWORDS:
            if any(_keyword(text, k) for k in keywords):
                return cause
        return CauseType.RESOURCE_EXHAUSTION

    def _reasoning(self, ctx: dict[str, Any]) -> dict[str, Any]:
        evidence = {e["evidence_id"]: e for e in ctx.get("evidence", [])}
        affected = ctx["incident"]["affected_service"]
        scores = self._scores(ctx)
        ordered = sorted(scores.items(), key=lambda kv: (-kv[1]["score"], kv[0] != affected, kv[0]))
        labels: list[tuple[str, CauseType, list[str]]] = []
        for name, info in ordered:
            if len(labels) == MAX_HYPOTHESES or (info["score"] <= 0 and len(labels) >= 2):
                break
            labels.append((name, self._cause(info), info["support"]))
        if len(labels) == 1:
            first = labels[0]
            alternative = CauseType.CAPACITY_LIMIT if first[1] is CauseType.DEPENDENCY_TIMEOUT else CauseType.DEPENDENCY_TIMEOUT
            labels.append((first[0], alternative, []))
        rank1_supported = bool(labels) and any(_complete_current(evidence.get(e)) for e in labels[0][2])
        confidences = CONFIDENCES_SUPPORTED if rank1_supported else CONFIDENCES_UNSUPPORTED
        hypotheses = []
        for rank, (name, cause, support) in enumerate(labels, start=1):
            sufficient = any(_complete_current(evidence.get(e)) for e in support)
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
                    else [
                        {
                            "description": f"Current, complete telemetry for {name} around the reported start time",
                            "suggested_tool": "query_metrics",
                        }
                    ],
                }
            )
        return {"hypotheses": hypotheses, "claim_statuses": self._claim_statuses(ctx)}

    @staticmethod
    def _claim_statuses(ctx: dict[str, Any]) -> list[dict[str, Any]]:
        incident = ctx["incident"]
        affected, start = incident["affected_service"], _dt(incident["start_time"])
        on_affected = [
            e for e in ctx.get("evidence", []) if not e.get("historical") and e.get("query", {}).get("service") == affected
        ]
        with_records = [e["evidence_id"] for e in on_affected if _record_count(e) > 0]
        with_errors = [e["evidence_id"] for e in on_affected if any(t["value"].startswith("error ") for t in _top_values(e))]
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
        window = _window(_dt(ctx["incident"]["start_time"]), PROBE_BEFORE, PROBE_AFTER)
        component = rank1["component"]

        def request(concern: str, tool: str, params: dict[str, Any], question: str) -> dict[str, Any]:
            return {
                "evidence_requests": [
                    {
                        "target": "observability",
                        "hypothesis_id": rank1["hypothesis_id"],
                        "concern": concern,
                        "question": question,
                        "suggested_tool": tool,
                        "suggested_params": params,
                        "rationale": f"The top hypothesis on {component} needs current, complete evidence.",
                    }
                ]
            }

        if not current and any(e.get("historical") for e in cited):  # Req 11.6
            logs = {"service": component, "time_range": window, "levels": ["error"], "limit": OFFLINE_LIMIT}
            return request("historical_only_support", "query_logs", logs, f"Do current logs of {component} show the failure?")
        if current and not complete:  # Req 11.17
            logs = {"service": component, "time_range": window, "levels": ["error"], "limit": OFFLINE_LIMIT - 5}
            return request("low_reliability_support", "query_logs", logs, f"Do complete logs of {component} confirm the failure?")
        if ctx.get("critic_round", 1) == 1 and len(complete) < 2:
            metrics = {
                "service": component,
                "metric_name": "error_rate",
                "time_range": window,
                "step_seconds": 600,
                "limit": OFFLINE_LIMIT,
            }
            return request("missing_evidence", "query_metrics", metrics, f"Did the error rate of {component} rise at the onset?")
        if not complete:  # an acceptance would fail Req 11.5
            logs = {"service": component, "time_range": window, "limit": OFFLINE_LIMIT}
            return request("missing_evidence", "query_logs", logs, f"What do the logs of {component} show?")
        return {"accept_hypothesis_id": rank1["hypothesis_id"]}

    # reporting

    @staticmethod
    def _actions(
        hypotheses: Iterable[dict[str, Any]], evidence: dict[str, dict[str, Any]], conclusive: bool
    ) -> list[dict[str, Any]]:
        actions = []
        for h in hypotheses:
            support = [e for e in h["supporting_evidence_ids"] if _complete_current(evidence.get(e))]
            cause = h["cause_type"].replace("_", " ")
            if h["rank"] == 1 and support and conclusive:
                actions.append(
                    {
                        "text": f"Mitigate {cause} on {h['component']} and confirm recovery.",
                        "category": "mitigation",
                        "hypothesis_id": h["hypothesis_id"],
                        "evidence_ids": support,
                    }
                )
            else:
                actions.append(
                    {
                        "text": f"Collect more evidence on {cause} for {h['component']}.",
                        "category": "further_investigation",
                        "hypothesis_id": h["hypothesis_id"],
                        "evidence_ids": [],
                    }
                )
        return actions

    def _reporting(self, ctx: dict[str, Any]) -> dict[str, Any]:
        hypotheses = sorted(ctx.get("hypotheses", []), key=lambda h: h["rank"])
        evidence = {e["evidence_id"]: e for e in ctx.get("evidence", [])}
        actions = self._actions(hypotheses, evidence, ctx.get("verdict") == "conclusive")
        return {"summary_claims": [], "recommended_actions": actions[: ctx.get("max_actions", 6)]}

    # baseline

    @staticmethod
    def _probes(incident: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        start = _dt(incident["start_time"])
        service = incident["affected_service"]
        window = _window(start, PROBE_BEFORE, PROBE_AFTER)
        return [
            ("query_logs", {"service": service, "time_range": window, "levels": ["warn", "error"], "limit": OFFLINE_LIMIT}),
            (
                "query_metrics",
                {"service": service, "metric_name": "error_rate", "time_range": window, "step_seconds": 600, "limit": OFFLINE_LIMIT},
            ),
            (
                "get_deployments",
                {"service": service, "time_range": _window(start, PROBE_LOOKBACK, timedelta(0)), "limit": OFFLINE_LIMIT},
            ),
            ("get_dependencies", {"service": service, "depth": PROBE_DEPENDENCY_DEPTH, "limit": OFFLINE_LIMIT}),
            ("search_incidents", {"query": incident["symptoms"][0][:200], "limit": OFFLINE_LIMIT}),
        ]

    @staticmethod
    def _history_evidence(turn: dict[str, Any]) -> dict[str, Any]:
        obs = turn["observation"]
        reliability = "partial" if obs.get("partial") else "stale" if obs.get("stale") else "complete"
        return {
            "evidence_id": obs["evidence_id"],
            "tool": obs["tool"],
            "query": obs.get("query") or {},
            "reliability": reliability,
            "historical": obs["tool"] == "search_incidents",
            "observation": obs.get("summary", {}),
        }

    def _baseline_step(self, ctx: dict[str, Any]) -> dict[str, Any]:
        history = ctx.get("history", [])
        probes = self._probes(ctx["incident"])
        if len(history) < len(probes):
            tool, params = probes[len(history)]
            return {
                "reasoning_note": f"Probe {len(history) + 1} of {len(probes)}: {tool}.",
                "tool_request": {"tool": tool, "params": params},
            }
        evidence = [self._history_evidence(t) for t in history if t.get("observation", {}).get("evidence_id")]
        reasoning = self._reasoning({**ctx, "evidence": evidence})
        hypotheses = [{**h, "hypothesis_id": f"H{h['rank']}"} for h in reasoning["hypotheses"]]
        by_id = {e["evidence_id"]: e for e in evidence}
        conclusive = bool(hypotheses) and any(_complete_current(by_id.get(e)) for e in hypotheses[0]["supporting_evidence_ids"])
        return {
            "reasoning_note": "All probes are done; answering from the observations.",
            "final_answer": {
                "outcome": "conclusive" if conclusive else "inconclusive",
                "hypotheses": hypotheses,
                "claim_statuses": reasoning["claim_statuses"],
                "claims": [],
                "recommended_actions": self._actions(hypotheses, by_id, conclusive),
            },
        }
