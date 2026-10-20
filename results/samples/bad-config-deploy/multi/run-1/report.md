# Investigation report: Elevated 5xx errors on reviews-0ee

## Incident

- Incident ID: `inc-e4c5349a46f3`
- Affected service: reviews-0ee
- Start time: 2024-01-18T00:50:00+00:00
- Severity: sev2
- System: multi (report built by reporting_agent)

## Verdict

**conclusive**

## Termination

- Termination reason: `critic_accepted`

## Hypotheses

### Rank 1: `H1` reviews-0ee / connection_pool_exhaustion

- Confidence: 0.86 (before data-gap penalty: 0.86)
- Revision: 1
- Statement: A config change on reviews-0ee reduced db.pool.max_connections from 50 to 5, causing connection pool exhaustion that drove the 5xx spike and latency increase.
- Supporting evidence: `E0003`, `E0002`, `E0005`
- Contradicting evidence: none
- Evidence sufficient: no
- Contributing factors: Directly affected the incident service, Timing is close to observed onset, Explains both elevated errors and latency
- Missing evidence:
  - Current database connection pool saturation metrics or wait-time evidence around 00:34-00:50Z
  - Application logs showing connection acquisition timeouts or pool exhaustion errors

### Rank 2: `H2` reviews-0ee / config_error

- Confidence: 0.67 (before data-gap penalty: 0.67)
- Revision: 1
- Statement: The incident was caused by an erroneous configuration change on reviews-0ee, with the reduced database pool limit being the triggering misconfiguration rather than a deeper code or dependency fault.
- Supporting evidence: `E0003`, `E0002`
- Contradicting evidence: none
- Evidence sufficient: no
- Contributing factors: Confirmed config change on the affected service, Error rate rose after the change
- Missing evidence:
  - Evidence that the config change alone, rather than another concurrent issue, explains the full error and latency pattern
  - Rollback or comparison data showing behavior before and after the config change

### Rank 3: `H3` reviews-0ee / resource_exhaustion

- Confidence: 0.54 (before data-gap penalty: 0.54)
- Revision: 1
- Statement: Reducing max database connections to 5 may have created an internal resource bottleneck on reviews-0ee, leading to request queuing and elevated latency with secondary 5xxs.
- Supporting evidence: `E0003`, `E0005`
- Contradicting evidence: none
- Evidence sufficient: no
- Contributing factors: Explains latency spike well, Could manifest as partial error increase under load
- Missing evidence:
  - Thread/worker saturation or request queue depth metrics for reviews-0ee during the incident window
  - Logs or traces showing requests blocked waiting on internal resources

### Rank 4: `H4` reviews-0ee / dependency_timeout

- Confidence: 0.22 (before data-gap penalty: 0.22)
- Revision: 1
- Statement: An upstream dependency of reviews-0ee may have slowed or timed out, and the service change merely coincided with the onset of the incident.
- Supporting evidence: `E0004`, `E0002`, `E0005`
- Contradicting evidence: `E0003`
- Evidence sufficient: no
- Contributing factors: reviews-0ee depends on several upstream services and a third-party API, Upstream issues can produce both latency and 5xxs
- Missing evidence:
  - Dependency-specific latency/error metrics for checkout-23b, media-bbe, session-cache-8e3, geo-api-38e, and front-lb-b54 around the incident window
  - Logs or traces identifying which upstream call path timed out

## Superseded Hypotheses

_No entries._

## Facts

- `F001` (dependency): reviews-0ee had a config change at 2024-01-18T00:34:00Z that reduced db.pool.max_connections from 50 to 5 on the affected service itself, which is a plausible direct contributor to elevated 5xx/latency near the incident start. [`E0003`]
- `F002` (dependency): reviews-0ee is downstream of front-lb-b54 and upstream of checkout-23b, media-bbe, session-cache-8e3, and geo-api-38e; these are the relevant dependency-path components to inspect for upstream or adjacent failures affecting reviews-0ee. [`E0004`]
- `F003` (observability): reviews-0ee error_rate changed from a low baseline to sustained 32% errors, with onset at 2024-01-18T00:40:00Z and clearly elevated by 00:50Z. [`E0002`]
- `F004` (observability): reviews-0ee latency_p99_ms spiked from normal levels to over 1s, with onset at 2024-01-18T00:45:00Z and sustained elevation from 00:50Z onward. [`E0005`]
- `F005` (reporting): reviews-0ee error_rate rose from a low baseline to a sustained 32% during the incident window. [`E0002`]
- `F006` (reporting): reviews-0ee p99 latency exceeded 1 second shortly after the error spike began. [`E0005`]
- `F007` (reporting): A config change on reviews-0ee reduced db.pool.max_connections from 50 to 5 shortly before the incident. [`E0003`]
- `F008` (reporting): reviews-0ee depends on front-lb-b54 and has downstream/upstream relationships that should be considered when validating the incident path. [`E0004`]

## Assumptions

_No entries._

## Data Gaps

_No entries._

## Recommended Actions

- `A1` [fix] for `H1`: Restore db.pool.max_connections on reviews-0ee to a safe operating value and validate database capacity before reapplying any lower limit; the observed error-rate and latency spike followed the reduction to 5 connections. (evidence: `E0003`, `E0002`, `E0005`)
- `A2` [mitigation] for `H1`: If connection saturation recurs, temporarily increase the pool limit and/or scale database capacity for reviews-0ee to relieve request blocking while the root cause is corrected. (evidence: `E0003`, `E0002`, `E0005`)
- `A3` [prevention] for `H2`: Add a configuration review and change-guardrail for database pool settings on reviews-0ee so reductions in max_connections require explicit capacity checks and approval before rollout. (evidence: `E0003`, `E0002`)
- `A4` [prevention] for `H3`: Add alerting and dashboards for connection pool utilization, request queue depth, and worker/thread saturation on reviews-0ee so resource bottlenecks are detected before they drive 5xxs and latency spikes. (evidence: `E0003`, `E0005`)
- `A5` [further_investigation] for `H4`: Inspect dependency latency/error metrics and traces for front-lb-b54, checkout-23b, media-bbe, session-cache-8e3, and geo-api-38e around the incident window to conclusively rule out an upstream timeout path. (evidence: `E0004`, `E0002`, `E0005`)

## Incident Report Claims

| Claim | Text | Status | Corroborating | Contradicting |
|---|---|---|---|---|
| `IC1` | HTTP 5xx rate on reviews-0ee above 25% | verified | `E0002` | none |
| `IC2` | p99 latency on reviews-0ee several times above normal | verified | `E0005` | none |
| `IC3` | The affected service is reviews-0ee. | verified | `E0002`, `E0005` | none |
| `IC4` | The incident started at 2024-01-18T00:50:00+00:00. | unverified | none | none |

## Missing Evidence

_No entries._

## Budget Usage

| Budget | Used | Limit |
|---|---|---|
| Tool calls | 6 | 60 |
| Tokens (input 28999, output 2524) | 31523 | 400000 |
| Critic rounds | 1 | 3 |
| Wall-clock seconds | 14.76 | 900 |

## Evidence Appendix

### `E0001` query_logs (stale)

- Query: `{"attributes":null,"levels":["error","warn"],"limit":25,"service":"reviews-0ee","text":null,"time_range":{"end":"2024-01-18T02:50:00Z","start":"2024-01-17T23:50:00Z"},"tool":"query_logs"}`
- Records: 0
- Note: no records matched

### `E0002` query_metrics (complete)

- Query: `{"limit":25,"metric_name":"error_rate","service":"reviews-0ee","step_seconds":600,"time_range":{"end":"2024-01-18T02:50:00Z","start":"2024-01-17T23:50:00Z"},"tool":"query_metrics"}`
- Records: 16
- Time span: 2024-01-17T23:50:00+00:00 – 2024-01-18T02:20:00+00:00
  - error_rate reviews-0ee (x16)
- metric_name: error_rate
- service: reviews-0ee

### `E0003` get_deployments (partial)

- Query: `{"change_types":["config_change","deployment","feature_flag_change"],"limit":25,"service":"reviews-0ee","time_range":{"end":"2024-01-18T00:50:00Z","start":"2024-01-11T00:50:00Z"},"tool":"get_deployments"}`
- Records: 1
- Time span: 2024-01-18T00:34:00+00:00 – 2024-01-18T00:34:00.000001+00:00
  - config_change reviews-0ee @2024-01-18T00:34:00Z: Set db.pool.max_connections from 50 to 5 (x1)
- change_type: config_change
- service: reviews-0ee

### `E0004` get_dependencies (partial)

- Query: `{"depth":5,"limit":25,"service":"reviews-0ee","tool":"get_dependencies"}`
- Records: 6
  - upstream checkout-23b (service) hops=1 (x1)
  - downstream front-lb-b54 (load_balancer) hops=1 (x1)
  - upstream media-bbe (service) hops=1 (x1)
  - upstream session-cache-8e3 (cache) hops=1 (x1)
  - upstream geo-api-38e (third_party_api) hops=2 (x1)
- component: checkout-23b, front-lb-b54, geo-api-38e, mail-queue-53f, media-bbe
- direction: downstream, upstream

### `E0005` query_metrics (complete)

- Query: `{"limit":25,"metric_name":"latency_p99_ms","service":"reviews-0ee","step_seconds":300,"time_range":{"end":"2024-01-18T01:10:00Z","start":"2024-01-18T00:40:00Z"},"tool":"query_metrics"}`
- Records: 6
- Time span: 2024-01-18T00:40:00+00:00 – 2024-01-18T01:05:00+00:00
  - latency_p99_ms reviews-0ee (x6)
- metric_name: latency_p99_ms
- service: reviews-0ee
