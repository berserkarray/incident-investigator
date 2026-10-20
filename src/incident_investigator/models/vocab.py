"""Closed vocabularies shared by every component."""

from enum import StrEnum, auto
from typing import Literal


class Severity(StrEnum):
    SEV1 = "sev1"
    SEV2 = "sev2"
    SEV3 = "sev3"
    SEV4 = "sev4"


class CauseType(StrEnum):
    """General incident cause taxonomy. It is broader than the synthetic dataset on purpose."""

    # changes
    CONFIG_ERROR = "config_error"
    DEPLOY_REGRESSION = "deploy_regression"
    FEATURE_FLAG_REGRESSION = "feature_flag_regression"
    CODE_DEFECT = "code_defect"
    # data stores and caches
    DATABASE_FAILOVER = "database_failover"
    CONNECTION_POOL_EXHAUSTION = "connection_pool_exhaustion"
    CACHE_STAMPEDE = "cache_stampede"
    DATA_CORRUPTION = "data_corruption"
    # resources and capacity
    MEMORY_LEAK = "memory_leak"
    DISK_FULL = "disk_full"
    RESOURCE_EXHAUSTION = "resource_exhaustion"
    CAPACITY_LIMIT = "capacity_limit"
    # network, edge and dependencies
    LOAD_BALANCER_MISCONFIGURATION = "load_balancer_misconfiguration"
    NETWORK_PARTITION = "network_partition"
    DNS_FAILURE = "dns_failure"
    CERTIFICATE_EXPIRY = "certificate_expiry"
    THIRD_PARTY_OUTAGE = "third_party_outage"
    DEPENDENCY_TIMEOUT = "dependency_timeout"
    RATE_LIMITING = "rate_limiting"
    # traffic and time
    TRAFFIC_ATTACK = "traffic_attack"
    CLOCK_SKEW = "clock_skew"


class ToolName(StrEnum):
    QUERY_LOGS = "query_logs"
    QUERY_METRICS = "query_metrics"
    GET_DEPLOYMENTS = "get_deployments"
    GET_DEPENDENCIES = "get_dependencies"
    SEARCH_INCIDENTS = "search_incidents"


class ErrorCode(StrEnum):
    TIMEOUT = "timeout"
    INVALID_INPUT = "invalid_input"
    CIRCUIT_OPEN = "circuit_open"
    BUDGET_EXHAUSTED = "budget_exhausted"


class FailureType(StrEnum):
    TIMEOUT = "timeout"
    PARTIAL = "partial_result"
    STALE = "stale_data"


class Reliability(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    STALE = "stale"


class AgentName(StrEnum):
    ORCHESTRATOR = "orchestrator"
    OBSERVABILITY = "observability"
    DEPENDENCY = "dependency"
    REASONING = "reasoning"
    CRITIC = "critic"
    REPORTING = "reporting"


BASELINE: Literal["baseline"] = "baseline"
Caller = AgentName | Literal["baseline"]
"""Who made an LLM call or a Tool_Request: one of the six Agents or the Baseline_Agent."""


class SystemName(StrEnum):
    MULTI = "multi"
    BASELINE = "baseline"


class TaskState(StrEnum):
    PENDING = auto()
    RUNNING = auto()
    COMPLETED = auto()
    FAILED = auto()
    CANCELLED = auto()


TERMINAL_TASK_STATES: frozenset[TaskState] = frozenset(
    {TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED}
)


class TaskKind(StrEnum):
    INITIAL = auto()
    EVIDENCE_REQUEST = auto()
    REASONING = auto()
    CRITIC = auto()
    REPORTING = auto()


class ConcernCategory(StrEnum):
    MISSING_EVIDENCE = "missing_evidence"
    CONTRADICTING_EVIDENCE = "contradicting_evidence"
    UNEXPLORED_ALTERNATIVE = "unexplored_alternative"
    TEMPORAL_COINCIDENCE = "temporal_coincidence"
    HISTORICAL_ONLY_SUPPORT = "historical_only_support"
    LOW_RELIABILITY_SUPPORT = "low_reliability_support"
    UNVERIFIED_REPORT_CLAIM = "unverified_report_claim"


class ActionCategory(StrEnum):
    MITIGATION = auto()
    FIX = auto()
    PREVENTION = auto()
    FURTHER_INVESTIGATION = auto()


class ClaimStatus(StrEnum):
    UNVERIFIED = auto()
    VERIFIED = auto()
    CONTRADICTED = auto()


class ClaimLabel(StrEnum):
    FACT = auto()
    ASSUMPTION = auto()


class Verdict(StrEnum):
    CONCLUSIVE = auto()
    INCONCLUSIVE = auto()


class TerminationReason(StrEnum):
    # Investigator
    CRITIC_ACCEPTED = auto()
    MAX_CRITIC_ROUNDS = auto()
    DUPLICATE_REQUESTS_ONLY = auto()
    TOOL_CALL_BUDGET_EXHAUSTED = auto()
    TOKEN_BUDGET_EXHAUSTED = auto()
    WALL_CLOCK_TIMEOUT = auto()
    CRITIC_FAILED = auto()
    NO_HYPOTHESES = auto()
    # Baseline only
    FINAL_ANSWER = auto()
    BASELINE_FAILED = auto()


INVESTIGATOR_REASONS: frozenset[TerminationReason] = frozenset(
    {
        TerminationReason.CRITIC_ACCEPTED,
        TerminationReason.MAX_CRITIC_ROUNDS,
        TerminationReason.DUPLICATE_REQUESTS_ONLY,
        TerminationReason.TOOL_CALL_BUDGET_EXHAUSTED,
        TerminationReason.TOKEN_BUDGET_EXHAUSTED,
        TerminationReason.WALL_CLOCK_TIMEOUT,
        TerminationReason.CRITIC_FAILED,
        TerminationReason.NO_HYPOTHESES,
    }
)
BASELINE_REASONS: frozenset[TerminationReason] = frozenset(
    {
        TerminationReason.FINAL_ANSWER,
        TerminationReason.BASELINE_FAILED,
        TerminationReason.TOOL_CALL_BUDGET_EXHAUSTED,
        TerminationReason.TOKEN_BUDGET_EXHAUSTED,
        TerminationReason.WALL_CLOCK_TIMEOUT,
    }
)


class FailureKind(StrEnum):
    EXCEPTION = "exception"
    TASK_TIMEOUT = "per_task_timeout"
    STRUCTURED_OUTPUT = "structured_output"


class ComponentKind(StrEnum):
    SERVICE = auto()
    DATABASE = auto()
    CACHE = auto()
    LOAD_BALANCER = auto()
    THIRD_PARTY_API = auto()
    QUEUE = auto()


class LogLevel(StrEnum):
    DEBUG = auto()
    INFO = auto()
    WARN = auto()
    ERROR = auto()


class MetricName(StrEnum):
    REQUEST_RATE = "request_rate"
    ERROR_RATE = "error_rate"
    LATENCY_P99_MS = "latency_p99_ms"
    CPU_UTILIZATION = "cpu_utilization"
    MEMORY_UTILIZATION = "memory_utilization"
    DISK_UTILIZATION = "disk_utilization"
    CACHE_HIT_RATIO = "cache_hit_ratio"
    DB_CONNECTIONS = "db_connections"
    REPLICATION_LAG_S = "replication_lag_s"


class ChangeType(StrEnum):
    DEPLOYMENT = "deployment"
    CONFIG_CHANGE = "config_change"
    FEATURE_FLAG_CHANGE = "feature_flag_change"


class FindingType(StrEnum):
    DEPLOYMENT = auto()
    CONFIG_CHANGE = auto()
    FEATURE_FLAG_CHANGE = auto()
    DEPENDENCY_RELATION = auto()
    PAST_INCIDENT = auto()


class CallKind(StrEnum):
    PLAN = auto()
    SPECIALIST_STEP = auto()
    SPECIALIST_FINDINGS = auto()
    REASONING = auto()
    CRITIC = auto()
    REPORTING = auto()
    BASELINE_STEP = auto()
    REPAIR = auto()


SPECIALIST_TOOLS: dict[AgentName, frozenset[ToolName]] = {
    AgentName.OBSERVABILITY: frozenset({ToolName.QUERY_LOGS, ToolName.QUERY_METRICS}),
    AgentName.DEPENDENCY: frozenset(
        {ToolName.GET_DEPLOYMENTS, ToolName.GET_DEPENDENCIES, ToolName.SEARCH_INCIDENTS}
    ),
}
"""The Tools each Specialist_Agent may request (Req 8.1, 9.1)."""

NO_STALE_TOOLS: frozenset[ToolName] = frozenset({ToolName.GET_DEPENDENCIES, ToolName.SEARCH_INCIDENTS})
"""Tools whose input has no time range, so stale-data cannot apply (Req 14.1, 14.9)."""
