"""The five Tools and their Observation_Summaries against hand-built snapshots (Req 3.11, 13.3-13.7, 13.9, 13.12, 13.13)."""

from datetime import timedelta

from factories import T0, at, make_snapshot, window

from incident_investigator.models.snapshot import ChangeEvent, DependencyEdge, MetricPoint, PastIncident
from incident_investigator.models.tools import (
    GetDependenciesInput,
    GetDeploymentsInput,
    MetricBucket,
    QueryMetricsInput,
    SearchIncidentsInput,
    ToolResult,
)
from incident_investigator.models.vocab import ChangeType, MetricName, ToolName
from incident_investigator.tools.impl import (
    TOOL_REGISTRY,
    cut_to_limit,
    get_dependencies,
    get_deployments,
    query_metrics,
    search_incidents,
)
from incident_investigator.tools.summary import ObservationSummarizer

SERVICE = "checkout-a1b"


def test_registry_has_exactly_the_five_tools():
    """Req 13.1"""
    assert set(TOOL_REGISTRY) == set(ToolName)


# query_metrics (Req 13.4, 13.13)


def metric_snapshot():
    points = [
        MetricPoint(record_id=f"m{i:02d}", timestamp=at(i), service=SERVICE, metric_name=MetricName.ERROR_RATE, value=float(i))
        for i in range(12)
    ]
    points += [
        MetricPoint(record_id="other-svc", timestamp=at(1), service="payments-c2d", metric_name=MetricName.ERROR_RATE, value=9.0),
        MetricPoint(record_id="other-metric", timestamp=at(1), service=SERVICE, metric_name=MetricName.REQUEST_RATE, value=9.0),
    ]
    return make_snapshot(metrics=tuple(points))


def test_query_metrics_buckets_align_to_range_start_and_exclude_the_end():
    snap = metric_snapshot()
    inp = QueryMetricsInput(service=SERVICE, metric_name="error_rate", time_range=window(0, 10), step_seconds=300)
    first, second = query_metrics(inp, snap)
    assert isinstance(first, MetricBucket)
    assert (first.timestamp, first.mean, first.min, first.max, first.count) == (at(0), 2.0, 0.0, 4.0, 5)
    assert first.constituent_ids == ("m00", "m01", "m02", "m03", "m04")
    assert (second.timestamp, second.mean, second.count) == (at(5), 7.0, 5)  # m10 at the end is excluded
    assert first.record_id == f"{SERVICE}/error_rate:{int(at(0).timestamp())}:300"

    shifted = query_metrics(inp.model_copy(update={"time_range": window(2, 12)}), snap)
    assert [(b.timestamp, b.constituent_ids[0], b.count) for b in shifted] == [(at(2), "m02", 5), (at(7), "m07", 5)]


def test_query_metrics_skips_empty_buckets_and_other_series():
    snap = metric_snapshot()
    inp = QueryMetricsInput(service=SERVICE, metric_name="error_rate", time_range=window(-60, 120), step_seconds=3600)
    (bucket,) = query_metrics(inp, snap)  # the first hour bucket is empty and omitted
    assert bucket.timestamp == at(0) and bucket.count == 12 and "other-svc" not in bucket.constituent_ids
    none = QueryMetricsInput(service=SERVICE, metric_name="cpu_utilization", time_range=window(0, 60))
    assert query_metrics(none, snap) == []


# get_deployments (Req 13.5, 13.13)


def change(record_id, minute, service=SERVICE, change_type=ChangeType.DEPLOYMENT):
    return ChangeEvent(record_id=record_id, timestamp=at(minute), service=service, change_type=change_type, summary=f"s-{record_id}")


def deployments_snapshot():
    return make_snapshot(
        changes=(
            change("c3", 30, change_type=ChangeType.CONFIG_CHANGE),
            change("c1", 0),
            change("c2", 10, service="payments-c2d", change_type=ChangeType.FEATURE_FLAG_CHANGE),
            change("c0", 0, service="payments-c2d"),
            change("c4", 60),
        )
    )


def test_get_deployments_filters_and_orders():
    snap = deployments_snapshot()
    all_in_range = get_deployments(GetDeploymentsInput(time_range=window(0, 60)), snap)
    assert [c.record_id for c in all_in_range] == ["c0", "c1", "c2", "c3"]  # c4 sits on the end bound
    by_service = get_deployments(GetDeploymentsInput(time_range=window(0, 61), service=SERVICE), snap)
    assert [c.record_id for c in by_service] == ["c1", "c3", "c4"]
    by_type = get_deployments(
        GetDeploymentsInput(time_range=window(0, 61), change_types=["config_change", "feature_flag_change"]), snap
    )
    assert [c.record_id for c in by_type] == ["c2", "c3"]
    assert get_deployments(GetDeploymentsInput(time_range=window(100, 120)), snap) == []


# get_dependencies (Req 13.6)


def edges(*pairs):
    return tuple(DependencyEdge(record_id=rid, dependent=a, dependency=b) for rid, a, b in pairs)


def dependency_snapshot(extra=()):
    return make_snapshot(
        dependencies=edges(
            ("dep-1", "lb-0aa", SERVICE),
            ("dep-2", SERVICE, "payments-c2d"),
            ("dep-3", SERVICE, "orders-db-456"),
            ("dep-4", "payments-c2d", "card-api-abc"),
            ("dep-5", "orders-db-456", "card-api-abc"),
            *extra,
        )
    )


def test_get_dependencies_depth_and_direction():
    snap = dependency_snapshot()
    one_hop = get_dependencies(GetDependenciesInput(service=SERVICE), snap)
    assert {(d.component, d.direction, d.hops) for d in one_hop} == {
        ("payments-c2d", "upstream", 1),
        ("orders-db-456", "upstream", 1),
        ("lb-0aa", "downstream", 1),
    }
    assert [d.record_id for d in one_hop] == ["dep-1", "dep-2", "dep-3"]  # no timestamps: ordered by record ID
    downstream = get_dependencies(GetDependenciesInput(service="payments-c2d", depth=2), snap)
    lb = next(d for d in downstream if d.component == "lb-0aa")
    assert (lb.direction, lb.hops, lb.record_id, lb.constituent_ids) == ("downstream", 2, "dep-1", ("dep-2", "dep-1"))
    assert lb.component_kind == "load_balancer"


def test_get_dependencies_shortest_path_then_lowest_edge_ids():
    two_paths = get_dependencies(GetDependenciesInput(service=SERVICE, depth=5), dependency_snapshot())
    card = next(d for d in two_paths if d.component == "card-api-abc")
    assert (card.hops, card.record_id, card.constituent_ids) == (2, "dep-4", ("dep-2", "dep-4"))

    direct = dependency_snapshot(extra=[("dep-9", SERVICE, "card-api-abc")])
    card = next(d for d in get_dependencies(GetDependenciesInput(service=SERVICE, depth=5), direct) if d.component == "card-api-abc")
    assert (card.hops, card.record_id) == (1, "dep-9")


def test_get_dependencies_of_an_isolated_component_is_empty():
    assert get_dependencies(GetDependenciesInput(service="search-123", depth=5), dependency_snapshot()) == []


# search_incidents (Req 13.7)


def incident(record_id, days_ago, title, services=(SERVICE,), symptoms=("errors",), cause="dns_failure"):
    return PastIncident(
        record_id=record_id,
        occurred_at=T0 - timedelta(days=days_ago),
        title=title,
        summary="summary",
        affected_services=services,
        symptoms=symptoms,
        cause_type=cause,
        resolution="fixed",
    )


def incidents_snapshot():
    return make_snapshot(
        past_incidents=(
            incident("pin-a", 10, "Connection POOL saturated"),
            incident("pin-b", 30, "Disk alarm", services=("payments-c2d",), symptoms=("pool timeouts",)),
            incident("pin-c", 5, "Unrelated outage"),
        )
    )


def test_search_incidents_matches_any_term_case_insensitively():
    snap = incidents_snapshot()
    found = search_incidents(SearchIncidentsInput(query="pool  Timeout"), snap)
    assert [p.record_id for p in found] == ["pin-b", "pin-a"]  # ordered by occurred_at
    scoped = search_incidents(SearchIncidentsInput(query="pool", service=SERVICE), snap)
    assert [p.record_id for p in scoped] == ["pin-a"]
    assert search_incidents(SearchIncidentsInput(query="kernel"), snap) == []


def test_truncation_keeps_the_first_records_in_order():
    """Req 13.9"""
    found = search_incidents(SearchIncidentsInput(query="pool outage"), incidents_snapshot())
    records, truncated = cut_to_limit(found, 2)
    assert [p.record_id for p in records] == ["pin-b", "pin-a"] and truncated
    assert cut_to_limit(found, 3)[1] is False


def test_tools_are_idempotent():
    """Req 13.10"""
    snap = dependency_snapshot()
    inp = GetDependenciesInput(service=SERVICE, depth=5)
    assert get_dependencies(inp, snap) == get_dependencies(inp, snap)


# Observation_Summary per Tool (design D3, Req 3.11)


def summarize(tool, records, query=None, size=5):
    return ObservationSummarizer(size).summarize(ToolResult(tool=tool, records=tuple(records)), query)


def test_metric_summary_has_series_entry_and_stats():
    snap = metric_snapshot()
    buckets = query_metrics(
        QueryMetricsInput(service=SERVICE, metric_name="error_rate", time_range=window(0, 12), step_seconds=240), snap
    )
    summary = summarize(ToolName.QUERY_METRICS, buckets)
    assert [(t.value, t.count, t.first_seen) for t in summary.top_values] == [(f"error_rate {SERVICE}", 3, at(0))]
    stats = summary.metric_stats
    assert (stats.min, stats.max, stats.first, stats.last) == (0.0, 11.0, 1.5, 9.5)
    assert (stats.min_at, stats.max_at, stats.mean) == (at(0), at(8), 5.5)
    assert summary.key_attributes == {"metric_name": ("error_rate",), "service": (SERVICE,)}


def test_change_summary_is_latest_first():
    records = get_deployments(GetDeploymentsInput(time_range=window(0, 61)), deployments_snapshot())
    summary = summarize(ToolName.GET_DEPLOYMENTS, records, size=2)
    assert [t.value for t in summary.top_values] == [
        f"deployment {SERVICE} @2024-01-01T01:00:00Z: s-c4",
        f"config_change {SERVICE} @2024-01-01T00:30:00Z: s-c3",
    ]
    assert summary.key_attributes["change_type"] == ("config_change", "deployment")  # capped at K=2


def test_dependency_summary_orders_by_hops_then_component():
    records = get_dependencies(GetDependenciesInput(service=SERVICE, depth=2), dependency_snapshot())
    summary = summarize(ToolName.GET_DEPENDENCIES, records)
    assert [t.value for t in summary.top_values] == [
        "downstream lb-0aa (load_balancer) hops=1",
        "upstream orders-db-456 (database) hops=1",
        "upstream payments-c2d (service) hops=1",
        "upstream card-api-abc (third_party_api) hops=2",
    ]
    assert summary.time_span is None and summary.key_attributes["direction"] == ("downstream", "upstream")


def test_incident_summary_orders_by_matched_terms_then_recency():
    query = SearchIncidentsInput(query="pool outage saturated")
    records = search_incidents(query, incidents_snapshot())
    summary = summarize(ToolName.SEARCH_INCIDENTS, records, query)
    assert [(t.value.split(" ")[0], t.count) for t in summary.top_values] == [("pin-a", 2), ("pin-c", 1), ("pin-b", 1)]
    assert summary.top_values[0].value == "pin-a 2023-12-22 Connection POOL saturated [cause=dns_failure]"


def test_summaries_are_deterministic_and_bounded():
    records = get_dependencies(GetDependenciesInput(service=SERVICE, depth=5), dependency_snapshot())
    first = summarize(ToolName.GET_DEPENDENCIES, records, size=2)
    assert first == summarize(ToolName.GET_DEPENDENCIES, list(records), size=2)
    assert len(first.top_values) == 2 and first.record_count == len(records)
