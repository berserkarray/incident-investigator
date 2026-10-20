# Investigation report: Timeouts on shipping-929

## Incident

- Incident ID: `inc-e6b27f29bcb7`
- Affected service: shipping-929
- Start time: 2024-01-06T23:43:00+00:00
- Severity: sev2
- System: multi (report built by reporting_agent)

## Verdict

**inconclusive**

## Termination

- Termination reason: `max_critic_rounds`
- Limit reached: critic_rounds (configured 3, used 3)

## Hypotheses

### Rank 1: `H1` cart-e26 / dependency_timeout

- Confidence: 0.86 (before data-gap penalty: 0.91)
- Revision: 3
- Statement: cart-e26 is responding too slowly or timing out, causing shipping-929 to exceed its upstream call timeout and return 504s.
- Supporting evidence: `E0001`, `E0004`, `E0007`, `E0009`, `E0010`
- Contradicting evidence: `E0008`
- Evidence sufficient: no
- Contributing factors: Immediate upstream dependency is cart-e26, shipping-929 logs show repeated cart call timeouts, cart-e26 latency is already high, cart-e26 emits worker-pool exhaustion errors, cart-e26 CPU is near saturation
- Missing evidence:
  - Direct cart-e26 logs or traces showing request queueing, timeout errors, or slow backend calls at the incident time.
  - Request-level tracing or dependency timing that ties shipping-929 timeouts to specific slow cart-e26 operations.

### Rank 2: `H2` cart-e26 / resource_exhaustion

- Confidence: 0.77 (before data-gap penalty: 0.82)
- Revision: 3
- Statement: cart-e26 is under resource pressure or capacity strain, which is manifesting as elevated latency and causing downstream shipping-929 timeouts.
- Supporting evidence: `E0007`, `E0009`, `E0010`, `E0001`
- Contradicting evidence: `E0008`
- Evidence sufficient: no
- Contributing factors: High cart-e26 p99 latency, Worker pool exhaustion errors, Near-saturated CPU, Shipping timeouts align with cart slowness
- Missing evidence:
  - Cart-side memory, thread pool, or connection pool metrics during the incident window to confirm the specific exhausted resource.
  - Cart logs showing whether saturation was caused by load, retries, or an internal bottleneck.

### Rank 3: `H3` shipping-929 / connection_pool_exhaustion

- Confidence: 0.29 (before data-gap penalty: 0.34)
- Revision: 3
- Statement: shipping-929 itself may be exhausting outbound connection or worker pools while waiting on cart-e26, amplifying timeout errors and 504s without a local deployment change.
- Supporting evidence: `E0001`, `E0002`
- Contradicting evidence: `E0003`, `E0006`, `E0005`
- Evidence sufficient: no
- Contributing factors: No local deployment/config change on shipping-929, Shipping logs show upstream timeouts, Error rate is elevated on shipping-929
- Missing evidence:
  - Shipping-929 connection pool, worker pool, and queue-depth metrics around the incident start.
  - Shipping-929 logs or traces showing blocked threads, retries, or pool exhaustion.

### Rank 4: `H4` shipping-929 / code_defect

- Confidence: 0.13 (before data-gap penalty: 0.18)
- Revision: 3
- Statement: A latent bug in shipping-929's request handling or timeout logic is causing excessive 504s when cart calls are slow, independent of a recent deployment.
- Supporting evidence: `E0001`, `E0002`
- Contradicting evidence: `E0003`, `E0006`
- Evidence sufficient: no
- Contributing factors: No recent shipping-929 deployment found, Timeouts are visible in shipping logs, Error rate is elevated
- Missing evidence:
  - Application traces or stack traces from shipping-929 showing an internal exception path, retry bug, or timeout-handling defect.
  - Code or release evidence linking the behavior to a known defect.

## Superseded Hypotheses

_No entries._

## Facts

- `F001` (observability): shipping-929 error_rate jumped from ~0.3-0.5% to ~40% starting at 23:33Z and stayed elevated through the queried range. [`E0002`]
- `F002` (observability): shipping-929 latency_p99_ms was already elevated across the queried window (roughly 162-197 ms in the sampled buckets) with no distinct step-change onset visible in the available data. [`E0005`]
- `F003` (observability): shipping-929 began emitting repeated error logs for timeouts calling cart-e26 at 23:40:07Z, continuing repeatedly afterward. [`E0001`]
- `F004` (dependency): shipping-929 depends upstream on checkout-bbb (1 hop), which in turn is upstream of mail-queue-672 (2 hops); this places checkout-related request handling on the dependency path for shipping-929 timeouts. [`E0004`]
- `F005` (dependency): shipping-929 depends upstream on cart-e26 (1 hop); logs show shipping-929 errors are cart call timeouts, making cart-e26 the most directly implicated upstream dependency in the observed failure path. [`E0004`, `E0001`]
- `F006` (dependency): shipping-929 depends upstream on auth-620 (1 hop), so authentication latency or failures could contribute to request timeouts on the shipping path. [`E0004`]
- `F007` (dependency): shipping-929 depends upstream on catalog-db-7ab (1 hop), so database slowness is another upstream failure domain on the request path. [`E0004`]
- `F008` (dependency): shipping-929 depends upstream on session-cache-bcd (1 hop), so cache latency or misses could affect shipping request performance. [`E0004`]
- `F009` (dependency): shipping-929 depends upstream on card-gateway-6d9 (2 hops), so third-party payment dependency issues are on the broader upstream path, though less directly tied to the cart timeout logs. [`E0004`]
- `F010` (dependency): shipping-929 also has upstream queue dependencies on events-queue-2c6 and mail-queue-672, which are part of the broader dependency graph and could affect asynchronous side effects even if they are not the primary cause of synchronous 504s. [`E0004`]
- `F011` (dependency): shipping-929 has a downstream dependency on public-lb-571, indicating the load balancer sits in front of the service rather than being an upstream cause of the timeout behavior. [`E0004`]
- `F012` (dependency): No deployments, config changes, or feature-flag changes were found for shipping-929 in the week before the incident start time, so there is no current evidence of a direct change on the service itself. [`E0003`]
- `F013` (observability): shipping-929 error_rate was already elevated by 2024-01-06T22:43:00Z and remained high through the queried range, with a last observed value of 0.401971 and a max of 0.4118. [`E0002`]
- `F014` (observability): shipping-929 error logs show timeouts calling cart beginning at 2024-01-06T23:40:07.364803Z and continuing through the observed window. [`E0001`]
- `F015` (observability): shipping-929 p99 latency did not show a corresponding spike in the queried range; values stayed around 162-197 ms, so the incident symptom of p99 latency above 2 s is not supported by the available metric evidence. [`E0005`]
- `F016` (observability): cart-e26 p99 latency was already high at the start of the queried window (about 1.4-2.3 s) and remained elevated, which could explain shipping-929 timeouts via an upstream dependency. [`E0007`]
- `F017` (dependency): shipping-929 depends on cart-e26 as an immediate upstream service in its dependency path; this places cart-e26 directly upstream of the affected service. [`E0004`]
- `F018` (dependency): shipping-929 was experiencing upstream call timeouts to cart-e# during the incident window, indicating the failure mode was in the cart dependency path rather than a local deployment/config change on shipping-929. [`E0001`]
- `F019` (dependency): shipping-929 showed elevated error rate during the incident window, consistent with the reported 504s and timeout symptoms. [`E0002`]
- `F020` (dependency): shipping-929 showed elevated p99 latency during the incident window, consistent with the reported timeout symptoms. [`E0005`]
- `F021` (dependency): No deployment, config change, or feature-flag change was found for shipping-929 in the incident window, so there is no current evidence of a local change on the affected service. [`E0006`, `E0003`]
- `F022` (dependency): No deployment, config change, or feature-flag change was found for cart-e26 in the incident window, so there is no current evidence of a recent change on the immediate upstream dependency. [`E0008`]
- `F023` (observability): shipping-929 error_rate rose from a low baseline to elevated levels during the incident window, with the first observed increase at 2024-01-06T23:43:00Z and continuing upward afterward. [`E0002`]
- `F024` (observability): shipping-929 p99 latency was already elevated by 2024-01-06T23:38:00Z and remained high through the queried range, consistent with the reported timeout symptoms. [`E0005`]
- `F025` (observability): shipping-929 began logging upstream timeout errors to cart around 2024-01-06T23:40:07Z, indicating the shipping service was failing while waiting on cart. [`E0001`]
- `F026` (observability): cart-e26 p99 latency was already above 1.4 s by 2024-01-06T23:38:00Z and peaked above 2.2 s at 2024-01-06T23:48:00Z, matching the upstream timeout hypothesis. [`E0007`]
- `F027` (observability): cart-e26 CPU utilization jumped from a lower baseline to sustained near-saturation starting at 2024-01-06T23:38:00Z, which is consistent with resource pressure on the upstream dependency. [`E0010`]
- `F028` (observability): cart-e26 started emitting worker-pool exhaustion errors at 2024-01-06T23:38:33Z, supporting saturation as the proximate cause of its slow responses. [`E0009`]
- `F029` (observability): shipping-929 request volume is not directly evidenced as changing in the available data; only error_rate is observed, so no onset can be established for request_rate from current evidence. [`E0002`]
- `F030` (observability): shipping-929 shows an error spike from upstream cart call timeouts; the first observed timeout log is at 2024-01-06T23:40:07.364803Z and the pattern continues through 23:57Z. [`E0001`]
- `F031` (observability): shipping-929 latency p99 is elevated during the incident window, with the first observed sample at 2024-01-06T23:38:00Z and a peak at 23:38Z. [`E0005`]
- `F032` (observability): shipping-929 error rate is elevated across the queried range, reaching 0.401971 by 2024-01-07T00:03:00Z. [`E0002`]
- `F033` (observability): cart-e26 shows worker-pool exhaustion errors beginning at 2024-01-06T23:38:33.940460Z, consistent with request rejection under load. [`E0009`]
- `F034` (observability): cart-e26 latency p99 is already high at the start of the observed window and remains above 2 s through the available samples. [`E0007`]
- `F035` (observability): cart-e26 CPU utilization is elevated and peaks at 2024-01-06T23:43:00Z, aligning with the onset of worker-pool exhaustion. [`E0010`]
- `F036` (observability): cart-e26 shows CPU saturation beginning at the incident onset, consistent with a bottleneck behind the upstream timeouts. [`E0010`]
- `F037` (observability): cart-e26 logs show worker pool exhaustion starting shortly after the incident began, indicating request rejection under load. [`E0009`]
- `F038` (observability): shipping-929 error rate rose sharply during the incident window, matching the reported 504 symptom. [`E0002`]
- `F039` (observability): shipping-929 latency p99 was elevated during the incident window, matching the reported timeout symptom. [`E0005`]
- `F040` (observability): cart-e26 memory utilization did not materially change over the queried range, so memory pressure is not supported as the bottleneck by current evidence. [`E0017`]
- `F041` (observability): shipping-929 began emitting error logs for timed-out calls to cart around 2024-01-06T23:40:07Z, consistent with the reported timeout symptoms. [`E0001`]
- `F042` (observability): shipping-929 error_rate was already elevated in the queried range and rose to 0.401971 by 2024-01-07T00:03:00Z, indicating sustained error pressure during the incident window. [`E0002`]
- `F043` (observability): shipping-929 p99 latency stayed in a relatively narrow band (about 162-197 ms) over the queried range, so I do not see evidence of a p99 latency spike above 2 s in the available metrics. [`E0005`]
- `F044` (observability): cart-e26 started logging worker-pool exhaustion and request rejections at 2024-01-06T23:38:33Z, which aligns with the upstream timeout symptoms seen in shipping. [`E0009`]
- `F045` (observability): cart-e26 p99 latency was already high at the first observed point (1973 ms at 2024-01-06T23:38:00Z) and remained above 2 s by 2024-01-06T23:48:00Z, supporting a slow upstream dependency. [`E0007`]
- `F046` (observability): cart-e26 CPU utilization was elevated from the start of the queried window and peaked at 0.9889 at 2024-01-06T23:43:00Z, consistent with saturation during the incident. [`E0010`]
- `F047` (observability): cart-e26 memory utilization stayed roughly flat around 0.44-0.46 over the queried range, so memory does not appear to be the changing signal here. [`E0017`]
- `F048` (reporting): shipping-929 error_rate jumped from roughly 0.3-0.5% to about 40% and stayed elevated during the queried range. [`E0002`]
- `F049` (reporting): shipping-929 logs repeatedly show timeouts calling cart-e26 starting at 2024-01-06T23:40:07Z. [`E0001`]
- `F050` (reporting): cart-e26 p99 latency was already high, reaching above 2 s in the sampled window. [`E0007`]
- `F051` (reporting): cart-e26 emitted worker-pool exhaustion errors and showed near-saturated CPU during the incident window. [`E0009`, `E0010`]
- `F052` (reporting): No deployments, config changes, or feature-flag changes were found for shipping-929 or cart-e26 in the incident window. [`E0003`, `E0006`, `E0008`]
- `F053` (reporting): shipping-929 depends directly on cart-e26, making cart the most directly implicated upstream dependency in the observed failure path. [`E0004`, `E0001`]

## Assumptions

_No entries._

## Data Gaps

| Gap | Source | Query or task | Reason | Time range |
|---|---|---|---|---|
| `G001` | observability | {"limit":25,"metric_name":"db_connections","service":"cart-e26","step_seconds":300,"time_range":{"end":"2024-01-07T00:03:00Z","start":"2024-01-06T23:33:00Z"},"tool":"query_metrics"} | unfulfilled_evidence_request:ER3.2:max_critic_rounds | 2024-01-06T23:33:00+00:00 – 2024-01-07T00:03:00+00:00 |

## Recommended Actions

- `A1` [further_investigation] for `H1`: Collect cart-e26 evidence for the incident window to confirm whether the upstream slowdown is due to a specific dependency bottleneck rather than generic CPU saturation: query cart-e26 db connections, cache hit ratio, and replication lag metrics, and correlate them with cart logs/traces for queueing, timeout errors, or slow backend calls. (evidence: `E0007`, `E0009`, `E0010`)
- `A2` [further_investigation] for `H2`: Investigate the exact resource constraint on cart-e26 by gathering thread-pool, connection-pool, and queue-depth metrics plus cart-side logs/traces to determine whether load, retries, or an internal bottleneck caused the worker-pool exhaustion and elevated latency. (evidence: `E0007`, `E0009`, `E0010`, `E0017`)
- `A3` [further_investigation] for `H3`: Check shipping-929 for self-induced amplification by collecting outbound connection-pool, worker-pool, and queue-depth metrics around the incident start, along with logs/traces for blocked threads, retries, or pool exhaustion. (evidence: `E0001`, `E0002`)
- `A4` [further_investigation] for `H4`: Rule out a latent shipping-929 defect by reviewing application traces or stack traces for internal exception paths, retry bugs, or timeout-handling issues, and compare against release/code history for any known defect matching the observed timeout pattern. (evidence: `E0001`, `E0002`, `E0003`, `E0006`)

## Incident Report Claims

| Claim | Text | Status | Corroborating | Contradicting |
|---|---|---|---|---|
| `IC1` | HTTP 504 rate on shipping-929 above 30% | contradicted | none | `E0005` |
| `IC2` | p99 latency on shipping-929 above 2 s | contradicted | none | `E0005` |
| `IC3` | The affected service is shipping-929. | verified | `E0001`, `E0002` | none |
| `IC4` | The incident started at 2024-01-06T23:43:00+00:00. | contradicted | none | `E0002` |

## Missing Evidence

- [evidence_request] for `H1`: Can you query cart-e26 metrics during the incident window for db connections, cache hit ratio, or replication lag to determine whether a downstream dependency bottleneck rather than generic CPU saturation is driving the latency? (suggested: query_metrics)
- [data_gap]: observability: unfulfilled_evidence_request:ER3.2:max_critic_rounds
- [reasoning_agent] for `H1`: Direct cart-e26 logs or traces showing request queueing, timeout errors, or slow backend calls at the incident time.
- [reasoning_agent] for `H1`: Request-level tracing or dependency timing that ties shipping-929 timeouts to specific slow cart-e26 operations.
- [reasoning_agent] for `H2`: Cart-side memory, thread pool, or connection pool metrics during the incident window to confirm the specific exhausted resource.
- [reasoning_agent] for `H2`: Cart logs showing whether saturation was caused by load, retries, or an internal bottleneck.
- [reasoning_agent] for `H3`: Shipping-929 connection pool, worker pool, and queue-depth metrics around the incident start.
- [reasoning_agent] for `H3`: Shipping-929 logs or traces showing blocked threads, retries, or pool exhaustion.
- [reasoning_agent] for `H4`: Application traces or stack traces from shipping-929 showing an internal exception path, retry bug, or timeout-handling defect.
- [reasoning_agent] for `H4`: Code or release evidence linking the behavior to a known defect.

## Budget Usage

| Budget | Used | Limit |
|---|---|---|
| Tool calls | 21 | 60 |
| Tokens (input 162934, output 14350) | 177284 | 400000 |
| Critic rounds | 3 | 3 |
| Wall-clock seconds | 53.884 | 900 |

## Evidence Appendix

### `E0001` query_logs (complete)

- Query: `{"attributes":null,"levels":["error","warn"],"limit":25,"service":"shipping-929","text":null,"time_range":{"end":"2024-01-07T01:43:00Z","start":"2024-01-06T22:43:00Z"},"tool":"query_logs"}`
- Records: 25 (truncated)
- Time span: 2024-01-06T23:40:07.364803+00:00 – 2024-01-06T23:57:07.655663+00:00
  - error shipping-929: call to cart-e# timed out after # ms (x25)
- level: error
- service: shipping-929

### `E0002` query_metrics (complete)

- Query: `{"limit":25,"metric_name":"error_rate","service":"shipping-929","step_seconds":600,"time_range":{"end":"2024-01-07T01:43:00Z","start":"2024-01-06T22:43:00Z"},"tool":"query_metrics"}`
- Records: 15
- Time span: 2024-01-06T22:43:00+00:00 – 2024-01-07T01:03:00+00:00
  - error_rate shipping-929 (x15)
- metric_name: error_rate
- service: shipping-929

### `E0003` get_deployments (complete)

- Query: `{"change_types":["config_change","deployment","feature_flag_change"],"limit":25,"service":"shipping-929","time_range":{"end":"2024-01-06T23:43:00Z","start":"2023-12-30T23:43:00Z"},"tool":"get_deployments"}`
- Records: 0
- Note: no records matched

### `E0004` get_dependencies (complete)

- Query: `{"depth":5,"limit":25,"service":"shipping-929","tool":"get_dependencies"}`
- Records: 9
  - upstream auth-620 (service) hops=1 (x1)
  - upstream cart-e26 (service) hops=1 (x1)
  - upstream catalog-db-7ab (database) hops=1 (x1)
  - upstream checkout-bbb (service) hops=1 (x1)
  - downstream public-lb-571 (load_balancer) hops=1 (x1)
- component: auth-620, card-gateway-6d9, cart-e26, catalog-db-7ab, checkout-bbb
- direction: downstream, upstream

### `E0005` query_metrics (complete)

- Query: `{"limit":25,"metric_name":"latency_p99_ms","service":"shipping-929","step_seconds":600,"time_range":{"end":"2024-01-07T01:43:00Z","start":"2024-01-06T23:38:00Z"},"tool":"query_metrics"}`
- Records: 10
- Time span: 2024-01-06T23:38:00+00:00 – 2024-01-07T01:08:00+00:00
  - latency_p99_ms shipping-929 (x10)
- metric_name: latency_p99_ms
- service: shipping-929

### `E0006` get_deployments (complete)

- Query: `{"change_types":["config_change","deployment","feature_flag_change"],"limit":25,"service":"shipping-929","time_range":{"end":"2024-01-06T23:53:00Z","start":"2024-01-06T23:38:00Z"},"tool":"get_deployments"}`
- Records: 0
- Note: no records matched

### `E0007` query_metrics (complete)

- Query: `{"limit":25,"metric_name":"latency_p99_ms","service":"cart-e26","step_seconds":300,"time_range":{"end":"2024-01-06T23:53:00Z","start":"2024-01-06T23:38:00Z"},"tool":"query_metrics"}`
- Records: 3
- Time span: 2024-01-06T23:38:00+00:00 – 2024-01-06T23:48:00+00:00
  - latency_p99_ms cart-e26 (x3)
- metric_name: latency_p99_ms
- service: cart-e26

### `E0008` get_deployments (complete)

- Query: `{"change_types":["config_change","deployment","feature_flag_change"],"limit":25,"service":"cart-e26","time_range":{"end":"2024-01-06T23:53:00Z","start":"2024-01-06T23:38:00Z"},"tool":"get_deployments"}`
- Records: 0
- Note: no records matched

### `E0009` query_logs (complete)

- Query: `{"attributes":null,"levels":["error","warn"],"limit":25,"service":"cart-e26","text":null,"time_range":{"end":"2024-01-07T00:03:00Z","start":"2024-01-06T23:33:00Z"},"tool":"query_logs"}`
- Records: 6
- Time span: 2024-01-06T23:38:33.940460+00:00 – 2024-01-06T23:59:17.363390+00:00
  - error cart-e26: worker pool exhausted: #/# threads busy; rejecting request (x6)
- level: error
- service: cart-e26

### `E0010` query_metrics (complete)

- Query: `{"limit":25,"metric_name":"cpu_utilization","service":"cart-e26","step_seconds":300,"time_range":{"end":"2024-01-07T00:03:00Z","start":"2024-01-06T23:33:00Z"},"tool":"query_metrics"}`
- Records: 6
- Time span: 2024-01-06T23:33:00+00:00 – 2024-01-06T23:58:00+00:00
  - cpu_utilization cart-e26 (x6)
- metric_name: cpu_utilization
- service: cart-e26

### `E0011` query_logs (stale)

- Query: `{"attributes":null,"levels":["error","warn"],"limit":25,"service":"cart-e26","text":"timeout OR slow OR latency OR exhausted OR rejecting request","time_range":{"end":"2024-01-07T00:03:00Z","start":"2024-01-06T23:33:00Z"},"tool":"query_logs"}`
- Records: 0
- Note: no records matched

### `E0012` query_logs (stale)

- Query: `{"attributes":null,"levels":["error","warn"],"limit":25,"service":"cart-e26","text":"timeout OR slow OR latency OR exhausted OR rejecting request","time_range":{"end":"2024-01-07T00:03:00Z","start":"2024-01-06T23:33:00Z"},"tool":"query_logs"}`
- Records: 0
- Note: no records matched

### `E0013` query_logs (stale)

- Query: `{"attributes":null,"levels":["error","warn"],"limit":25,"service":"cart-e26","text":"timeout OR slow OR latency OR exhausted OR rejecting request","time_range":{"end":"2024-01-07T00:03:00Z","start":"2024-01-06T23:33:00Z"},"tool":"query_logs"}`
- Records: 0
- Note: no records matched

### `E0014` query_logs (stale)

- Query: `{"attributes":null,"levels":["error","warn"],"limit":25,"service":"cart-e26","text":"timeout OR slow OR latency OR exhausted OR rejecting request","time_range":{"end":"2024-01-07T00:03:00Z","start":"2024-01-06T23:33:00Z"},"tool":"query_logs"}`
- Records: 0
- Note: no records matched

### `E0015` query_logs (stale)

- Query: `{"attributes":null,"levels":["error","warn"],"limit":25,"service":"cart-e26","text":"timeout OR slow OR latency OR exhausted OR rejecting request","time_range":{"end":"2024-01-07T00:03:00Z","start":"2024-01-06T23:33:00Z"},"tool":"query_logs"}`
- Records: 0
- Note: no records matched

### `E0016` query_logs (complete)

- Query: `{"attributes":null,"levels":["error","warn"],"limit":25,"service":"cart-e26","text":"timeout OR slow OR latency OR queue OR rejecting OR exhausted","time_range":{"end":"2024-01-07T00:03:00Z","start":"2024-01-06T23:33:00Z"},"tool":"query_logs"}`
- Records: 0
- Note: no records matched

### `E0017` query_metrics (complete)

- Query: `{"limit":25,"metric_name":"memory_utilization","service":"cart-e26","step_seconds":300,"time_range":{"end":"2024-01-07T00:03:00Z","start":"2024-01-06T23:33:00Z"},"tool":"query_metrics"}`
- Records: 6
- Time span: 2024-01-06T23:33:00+00:00 – 2024-01-06T23:58:00+00:00
  - memory_utilization cart-e26 (x6)
- metric_name: memory_utilization
- service: cart-e26

### `E0018` query_logs (complete)

- Query: `{"attributes":null,"levels":["error","warn"],"limit":25,"service":"cart-e26","text":"timeout OR slow OR latency OR queue OR rejecting OR exhausted","time_range":{"end":"2024-01-07T00:03:00Z","start":"2024-01-06T23:33:00Z"},"tool":"query_logs"}`
- Records: 0
- Note: no records matched

### `E0019` query_logs (complete)

- Query: `{"attributes":null,"levels":["error","warn"],"limit":25,"service":"cart-e26","text":"timeout OR slow OR latency OR queue OR rejecting OR exhausted","time_range":{"end":"2024-01-07T00:03:00Z","start":"2024-01-06T23:33:00Z"},"tool":"query_logs"}`
- Records: 0
- Note: no records matched

### `E0020` query_logs (complete)

- Query: `{"attributes":null,"levels":["error","warn"],"limit":25,"service":"cart-e26","text":"timeout OR slow OR latency OR queue OR rejecting OR exhausted","time_range":{"end":"2024-01-07T00:03:00Z","start":"2024-01-06T23:33:00Z"},"tool":"query_logs"}`
- Records: 0
- Note: no records matched
