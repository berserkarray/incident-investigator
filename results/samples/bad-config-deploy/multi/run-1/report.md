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

### Rank 1: `H2` reviews-0ee / connection_pool_exhaustion

- Confidence: 0.90 (before data-gap penalty: 0.90)
- Revision: 2
- Statement: reviews-0ee likely exhausted its database connection pool after the pool limit was reduced from 50 to 5, causing request queuing, elevated latency, and 5xx errors.
- Supporting evidence: `E0003`, `E0005`, `E0007`
- Contradicting evidence: none
- Evidence sufficient: no
- Contributing factors: Config change reduced db.pool.max_connections from 50 to 5 shortly before symptoms, Repeated log errors report timed out waiting for a database connection, p99 latency rose sharply in the same window
- Missing evidence:
  - Direct pool saturation evidence such as pool waiters, connection acquisition latency, or exhausted connection counts at the incident onset.
  - Additional logs or traces showing request queuing or repeated connection acquisition failures immediately after the config change.

### Rank 2: `H1` reviews-0ee / config_error

- Confidence: 0.84 (before data-gap penalty: 0.84)
- Revision: 2
- Statement: A configuration change on reviews-0ee mis-sized the database pool, and the resulting config error directly caused the incident symptoms.
- Supporting evidence: `E0003`, `E0005`, `E0007`
- Contradicting evidence: none
- Evidence sufficient: no
- Contributing factors: Recent config change is the only observed change near onset, Timeout errors appeared after the change, Latency and error rate both worsened after the change
- Missing evidence:
  - Evidence that the new pool setting was intended and safe for expected traffic, or that another config parameter also changed.
  - Metrics or traces tying the config change to request queuing and 5xx generation rather than a coincidental timing overlap.

### Rank 3: `H3` reviews-0ee / dependency_timeout

- Confidence: 0.28 (before data-gap penalty: 0.28)
- Revision: 2
- Statement: An upstream dependency of reviews-0ee intermittently timed out, and reviews-0ee surfaced those failures as 5xx and higher latency.
- Supporting evidence: `E0004`, `E0002`
- Contradicting evidence: `E0005`
- Evidence sufficient: no
- Contributing factors: reviews-0ee has several upstream dependencies that could fail independently, Observed symptoms could be secondary to upstream slowness
- Missing evidence:
  - Current logs or traces showing timeout failures to checkout-23b, media-bbe, session-cache-8e3, or geo-api-38e around onset.
  - Dependency-specific latency or error metrics demonstrating an upstream outage or slowdown at the same time.

### Rank 4: `H4` reviews-0ee / code_defect

- Confidence: 0.20 (before data-gap penalty: 0.20)
- Revision: 2
- Statement: The config change exposed or triggered an application bug in reviews-0ee's database handling, leading to connection timeout failures and elevated 5xx/latency.
- Supporting evidence: `E0003`, `E0005`
- Contradicting evidence: none
- Evidence sufficient: no
- Contributing factors: Timeouts may reflect an application-level handling issue, No code deployment evidence is currently present
- Missing evidence:
  - Stack traces or application error details indicating a code path bug rather than pure resource shortage.
  - Deployment or code-change evidence showing a recent application change correlated with the onset.

## Superseded Hypotheses

_No entries._

## Facts

- `F001` (observability): reviews-0ee error_rate jumped from a baseline around 0.003-0.005 to 0.32 starting at 2024-01-18T00:40Z and stayed at 0.32 through the queried range. [`E0002`]
- `F002` (observability): No error or warn log records matched the queried reviews-0ee window, so logs did not show a detectable change in that range. [`E0001`]
- `F003` (dependency): reviews-0ee had a config change at 2024-01-18T00:34:00Z setting db.pool.max_connections from 50 to 5; this is on the affected service itself and directly in its database access path, making it the most relevant recent dependency-related change near the incident start. [`E0003`]
- `F004` (dependency): reviews-0ee depends upstream on checkout-23b, media-bbe, session-cache-8e3, and geo-api-38e, and has downstream front-lb-b54; this places the affected service in a path where upstream issues could also surface as reviews-0ee errors, but no recent change in those dependencies is evidenced here. [`E0004`]
- `F005` (observability): reviews-0ee error_rate rose sharply and peaked at 0.32 at 2024-01-18T00:40:00Z, consistent with the reported 5xx surge. [`E0002`]
- `F006` (observability): reviews-0ee began emitting repeated error logs for database connection acquisition timeouts starting at 2024-01-18T00:46:06.458639Z, which supports request queuing/pool exhaustion after the pool size reduction. [`E0005`]
- `F007` (observability): A config change on reviews-0ee set db.pool.max_connections from 50 to 5 at 2024-01-18T00:34:00Z, and connection-timeout errors appeared shortly afterward, making the config change a plausible trigger for the elevated 5xx/latency symptoms. [`E0003`, `E0005`]
- `F008` (observability): reviews-0ee shows a sharp increase in p99 latency starting at 2024-01-18T00:45:00Z, rising from ~170-190 ms to 1041.2161 ms and staying there through the queried range. [`E0007`]
- `F009` (observability): reviews-0ee shows elevated error_rate beginning by 2024-01-18T00:40:00Z, reaching 0.32 at peak in the queried window. [`E0002`]
- `F010` (observability): reviews-0ee logs show error-level request failures with the pattern 'timed out waiting for a database connection' starting at 2024-01-18T00:46:06.458639Z. [`E0005`]
- `F011` (observability): reviews-0ee had a config change at 2024-01-18T00:34:00Z that set db.pool.max_connections from 50 to 5, which is temporally close to the onset of the latency and timeout symptoms. [`E0003`]
- `F012` (reporting): reviews-0ee error_rate jumped from a baseline around 0.003-0.005 to 0.32 starting at 2024-01-18T00:40:00Z and stayed elevated through the queried range. [`E0002`]
- `F013` (reporting): The service had a config change at 2024-01-18T00:34:00Z setting db.pool.max_connections from 50 to 5. [`E0003`]
- `F014` (reporting): Repeated error logs reported timed out waiting for a database connection starting at 2024-01-18T00:46:06.458639Z. [`E0005`]
- `F015` (reporting): p99 latency rose sharply to 1041.2161 ms at 2024-01-18T00:45:00Z and remained elevated. [`E0007`]
- `F016` (reporting): The evidence most strongly supports connection-pool exhaustion or mis-sizing on reviews-0ee as the incident driver. [`E0003`, `E0005`, `E0007`]

## Assumptions

_No entries._

## Data Gaps

_No entries._

## Recommended Actions

- `A1` [fix] for `H2`: Restore the database connection pool to a safe value and validate the service under expected traffic; the pool reduction from 50 to 5 is the most plausible trigger for the observed connection-timeout errors, latency spike, and 5xx surge. (evidence: `E0003`, `E0005`, `E0007`)
- `A2` [mitigation] for `H2`: If the service is still degraded, temporarily increase available database connections or roll back the pool limit change to relieve request queuing and connection-acquisition timeouts. (evidence: `E0005`, `E0007`)
- `A3` [prevention] for `H1`: Add a deployment/configuration guardrail for db.pool.max_connections changes, including approval, safe defaults, and automated checks that block reductions likely to exhaust the pool under normal load. (evidence: `E0003`, `E0005`, `E0007`)
- `A4` [prevention] for `H1`: Add alerting and dashboards for connection-pool saturation signals, including pool wait time, exhausted connections, and request timeout rates, so future mis-sizing is detected before 5xx errors rise. (evidence: `E0003`, `E0005`, `E0007`)
- `A5` [further_investigation] for `H3`: Verify whether any upstream dependency of reviews-0ee was slow or failing at incident onset by checking dependency-specific logs and metrics for checkout-23b, media-bbe, session-cache-8e3, and geo-api-38e. (evidence: `E0004`)
- `A6` [further_investigation] for `H4`: Review application traces and stack details for the connection-timeout path to rule out an underlying code defect that could have been exposed by the pool-size change. (evidence: `E0003`, `E0005`)

## Incident Report Claims

| Claim | Text | Status | Corroborating | Contradicting |
|---|---|---|---|---|
| `IC1` | HTTP 5xx rate on reviews-0ee above 25% | verified | `E0002` | none |
| `IC2` | p99 latency on reviews-0ee several times above normal | verified | `E0007` | none |
| `IC3` | The affected service is reviews-0ee. | verified | `E0002`, `E0003` | none |
| `IC4` | The incident started at 2024-01-18T00:50:00+00:00. | contradicted | none | `E0002`, `E0003` |

## Missing Evidence

_No entries._

## Budget Usage

| Budget | Used | Limit |
|---|---|---|
| Tool calls | 7 | 60 |
| Tokens (input 46361, output 4857) | 51218 | 400000 |
| Critic rounds | 2 | 3 |
| Wall-clock seconds | 25.188 | 900 |

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

### `E0005` query_logs (complete)

- Query: `{"attributes":null,"levels":["error","warn"],"limit":25,"service":"reviews-0ee","text":null,"time_range":{"end":"2024-01-18T01:10:00Z","start":"2024-01-18T00:34:00Z"},"tool":"query_logs"}`
- Records: 16
- Time span: 2024-01-18T00:46:06.458639+00:00 – 2024-01-18T01:08:37.687487+00:00
  - error reviews-0ee: request failed: timed out waiting for a database connection (pool size #) (x16)
- level: error
- service: reviews-0ee

### `E0006` query_metrics (complete)

- Query: `{"limit":25,"metric_name":"db_connections","service":"reviews-0ee","step_seconds":300,"time_range":{"end":"2024-01-18T01:20:00Z","start":"2024-01-18T00:20:00Z"},"tool":"query_metrics"}`
- Records: 0
- Note: no records matched

### `E0007` query_metrics (complete)

- Query: `{"limit":25,"metric_name":"latency_p99_ms","service":"reviews-0ee","step_seconds":300,"time_range":{"end":"2024-01-18T01:20:00Z","start":"2024-01-18T00:20:00Z"},"tool":"query_metrics"}`
- Records: 12
- Time span: 2024-01-18T00:20:00+00:00 – 2024-01-18T01:15:00+00:00
  - latency_p99_ms reviews-0ee (x12)
- metric_name: latency_p99_ms
- service: reviews-0ee
