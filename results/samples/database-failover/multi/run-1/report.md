# Investigation report: Write errors on shipping-1cd

## Incident

- Incident ID: `inc-c8c6cf544662`
- Affected service: shipping-1cd
- Start time: 2024-01-05T08:10:00+00:00
- Severity: sev2
- System: multi (report built by reporting_agent)

## Verdict

**conclusive**

## Termination

- Termination reason: `critic_accepted`

## Hypotheses

### Rank 1: `H1` orders-db-9d9 / database_failover

- Confidence: 0.91 (before data-gap penalty: 0.91)
- Revision: 2
- Statement: orders-db-9d9 likely underwent a failover or role change around 08:07-08:13Z, leaving the writer temporarily read-only and intermittently resetting connections, which caused shipping-1cd write failures.
- Supporting evidence: `E0001`, `E0002`, `E0006`, `E0007`
- Contradicting evidence: none
- Evidence sufficient: no
- Contributing factors: Read-only transaction errors from shipping-1cd align with a writer role disruption., orders-db-9d9 logs show missed heartbeats, automatic failover, and later promotion., Replication lag was elevated before and during the incident window.
- Missing evidence:
  - Direct database-side evidence of the primary/replica transition details during 08:07-08:13Z, such as explicit promotion or role-change logs from orders-db-9d9.
  - Additional database metrics or events tying the read-only window and connection resets to the failover sequence with higher temporal precision.

### Rank 2: `H2` orders-db-9d9 / config_error

- Confidence: 0.56 (before data-gap penalty: 0.56)
- Revision: 2
- Statement: A database-side configuration or routing issue on orders-db-9d9 may have exposed a read-only endpoint or misrouted writes, producing the read-only transaction errors seen by shipping-1cd.
- Supporting evidence: `E0001`, `E0004`
- Contradicting evidence: `E0006`, `E0007`
- Evidence sufficient: no
- Contributing factors: The observed symptom is consistent with a read-only database endpoint., No local shipping-1cd deployment or config change was found, pushing suspicion upstream.
- Missing evidence:
  - Current evidence of a configuration change or endpoint routing change on orders-db-9d9 near the incident start.
  - Database-side logs or state showing read-only mode was introduced by configuration rather than failover.

### Rank 3: `H3` orders-db-9d9 / network_partition

- Confidence: 0.34 (before data-gap penalty: 0.34)
- Revision: 2
- Statement: A transient connectivity problem between shipping-1cd and orders-db-9d9 may have caused query resets and secondary write failures, with the read-only error reflecting an unstable database path rather than a local shipping bug.
- Supporting evidence: `E0001`, `E0004`
- Contradicting evidence: `E0006`, `E0007`
- Evidence sufficient: no
- Contributing factors: Connection resets were logged alongside write failures., The service dependency path includes orders-db-9d9 directly in the write path.
- Missing evidence:
  - Network telemetry or infrastructure logs showing packet loss, path instability, or connection disruption between shipping-1cd and orders-db-9d9 during 08:07-08:13Z.
  - Evidence that the database failover logs are insufficient to explain the connection resets, or that resets occurred independently of failover.

### Rank 4: `H4` shipping-1cd / code_defect

- Confidence: 0.18 (before data-gap penalty: 0.18)
- Revision: 2
- Statement: shipping-1cd may contain a code defect in its order-save path that mishandles database write errors or transaction state, amplifying an upstream database issue into user-visible save failures.
- Supporting evidence: `E0001`, `E0002`
- Contradicting evidence: `E0003`, `E0006`
- Evidence sufficient: no
- Contributing factors: The service error rate rose sharply during the incident window., However, the strongest evidence points to upstream database behavior rather than a local code change.
- Missing evidence:
  - Application traces or stack traces from shipping-1cd showing an internal exception path or bug in order persistence handling.
  - Release or build evidence tying the failure to a specific shipping-1cd version or recent code change.

## Superseded Hypotheses

_No entries._

## Facts

- `F001` (observability): shipping-1cd error_rate was stable around 0.2%–0.6% before 08:00Z, then jumped to a mean of 10.086% with a max of 22.42% in the 08:10Z bucket, matching the incident start and confirming a 5xx/error surge. [`E0002`]
- `F002` (observability): shipping-1cd began logging write failures against orders-db-9d9 at 08:07:13Z, with repeated occurrences through 08:12Z, indicating write requests were failing consistently during the incident window. [`E0001`]
- `F003` (observability): shipping-1cd also logged repeated database connection resets starting at 08:07:53Z and alternating with the read-only transaction errors, showing the failure mode was not a single isolated write error. [`E0001`]
- `F004` (dependency): shipping-1cd depends upstream on orders-db-9d9, which sits directly in the write path for the service. [`E0004`]
- `F005` (dependency): shipping-1cd also has upstream dependencies on auth-d4f, profile-609, session-cache-0da, jobs-queue-21c, and tax-api-5fe, so these are part of the broader dependency path that could affect request handling around the incident. [`E0004`]
- `F006` (dependency): No deployments, config changes, or feature-flag changes were found for shipping-1cd in the week before the incident start, so there is no current evidence of a local change on shipping-1cd to explain the write errors. [`E0003`]
- `F007` (dependency): shipping-1cd began showing elevated error_rate at the incident start time, with a maximum at 2024-01-05T08:10:00Z, matching the reported onset of the write-error incident. [`E0002`]
- `F008` (dependency): shipping-1cd logs show write failures against orders-db-9d9, including 'cannot execute UPDATE in a read-only transaction' and connection resets during query, which directly support a database write-path problem. [`E0001`]
- `F009` (observability): shipping-1cd began logging write failures to orders-db with read-only transaction errors at 08:07:13Z, just before the reported incident start. [`E0001`]
- `F010` (observability): shipping-1cd also started logging database connection resets during queries at 08:07:53Z, indicating the write path was failing around the incident onset. [`E0001`]
- `F011` (observability): shipping-1cd error_rate spiked to 0.2242 at 08:10:00Z, matching the reported 5xx/write-error window. [`E0002`]
- `F012` (observability): shipping-1cd latency_p99_ms stayed elevated but broadly stable across 08:00-08:20Z, with no distinct onset spike; the metric ranged roughly 165-198 ms and peaked at 08:10:00Z without a sharp step change. [`E0005`]
- `F013` (dependency): shipping-1cd experienced write failures because its upstream database dependency orders-db-9d9 returned read-only transaction errors and connection resets during queries; this sits on the upstream dependency path from shipping-1cd to orders-db-9d9. [`E0001`, `E0004`]
- `F014` (dependency): The incident onset aligns with a sharp increase in shipping-1cd error rate, which peaked at 2024-01-05T08:10:00Z, matching the reported start time of the write-error incident. [`E0002`]
- `F015` (dependency): No deployments, config changes, or feature-flag changes were found for shipping-1cd in the incident window, so there is no rollout event to attribute the write errors to. [`E0003`]
- `F016` (observability): shipping-1cd showed an error spike in write-path failures, with the signal starting at 2024-01-05T08:07:13Z; logs show read-only transaction write failures and connection resets to orders-db-9d9. [`E0001`]
- `F017` (observability): shipping-1cd also showed elevated error_rate, peaking at 2024-01-05T08:10:00Z, consistent with the reported 5xx increase. [`E0002`]
- `F018` (observability): shipping-1cd latency_p99_ms increased around the incident window, peaking at 2024-01-05T08:10:00Z, indicating degraded request handling during the write failures. [`E0005`]
- `F019` (observability): orders-db-9d9 showed a failover sequence beginning at 2024-01-05T08:07:06Z, with the primary missing heartbeats, automatic failover starting at 08:07:30Z, and writes accepted again after promotion at 08:13:00Z. [`E0006`]
- `F020` (observability): shipping-1cd began logging write failures against orders-db-9d9 at 08:07:13Z, with errors indicating the database was read-only. [`E0001`]
- `F021` (observability): shipping-1cd started logging database connection resets at 08:07:53Z, consistent with an upstream database disruption. [`E0001`]
- `F022` (observability): shipping-1cd error_rate peaked at 08:10:00Z, matching the reported incident onset window. [`E0002`]
- `F023` (observability): shipping-1cd p99 latency peaked at 08:10:00Z, indicating request slowdown during the incident window. [`E0005`]
- `F024` (observability): orders-db-9d9 logged a primary-unreachable error at 08:07:06Z, which is a failover indicator. [`E0006`]
- `F025` (observability): orders-db-9d9 began automatic failover at 08:07:30Z. [`E0006`]
- `F026` (observability): orders-db-9d9 reported the new primary was promoted and writes were accepted again at 08:13:00Z. [`E0006`]
- `F027` (observability): orders-db-9d9 replication lag rose from the 07:50 bucket into a sustained high range by 07:55-08:10Z, peaking at 08:05:00Z, then dropped sharply by 08:15:00Z. [`E0007`]
- `F028` (reporting): shipping-1cd error_rate spiked at the incident start, peaking at 08:10Z, consistent with the reported 5xx surge. [`E0002`]
- `F029` (reporting): shipping-1cd logged repeated write failures to orders-db-9d9, including read-only transaction errors and connection resets, starting just before the incident window. [`E0001`]
- `F030` (reporting): orders-db-9d9 logged a primary heartbeat miss, automatic failover, and later promotion of the new primary with writes accepted again. [`E0006`]
- `F031` (reporting): orders-db-9d9 replication lag was elevated before and during the incident window, then dropped sharply after failover completed. [`E0007`]
- `F032` (reporting): No deployments, config changes, or feature-flag changes were found for shipping-1cd in the week before the incident. [`E0003`]

## Assumptions

_No entries._

## Data Gaps

_No entries._

## Recommended Actions

- `A1` [further_investigation] for `H1`: Collect direct database-side failover evidence for orders-db-9d9 during 08:07-08:13Z, including explicit promotion/role-change logs and higher-resolution metrics, to confirm the read-only window and connection resets were caused by failover. (evidence: `E0006`, `E0007`)
- `A2` [further_investigation] for `H2`: Verify whether any orders-db-9d9 configuration or routing change occurred near incident start, and check database-side state/logs for read-only mode introduced by configuration rather than failover. (evidence: `E0003`, `E0006`)
- `A3` [further_investigation] for `H3`: Review network telemetry and infrastructure logs between shipping-1cd and orders-db-9d9 for packet loss, path instability, or connection disruption during 08:07-08:13Z to rule in or out a transient network partition. (evidence: `E0001`, `E0004`)
- `A4` [further_investigation] for `H4`: Inspect shipping-1cd application traces or stack traces and correlate them with release/build history to determine whether an internal code defect amplified the upstream database failure. (evidence: `E0001`, `E0002`, `E0003`)

## Incident Report Claims

| Claim | Text | Status | Corroborating | Contradicting |
|---|---|---|---|---|
| `IC1` | HTTP 5xx rate on shipping-1cd above 15% | verified | `E0002` | none |
| `IC2` | Orders submitted through shipping-1cd fail to save | verified | `E0001` | none |
| `IC3` | The affected service is shipping-1cd. | verified | `E0001`, `E0002`, `E0004` | none |
| `IC4` | The incident started at 2024-01-05T08:10:00+00:00. | verified | `E0001`, `E0002` | none |

## Missing Evidence

_No entries._

## Budget Usage

| Budget | Used | Limit |
|---|---|---|
| Tool calls | 7 | 60 |
| Tokens (input 65325, output 7394) | 72719 | 400000 |
| Critic rounds | 2 | 3 |
| Wall-clock seconds | 27.823 | 900 |

## Evidence Appendix

### `E0001` query_logs (complete)

- Query: `{"attributes":null,"levels":["error","warn"],"limit":25,"service":"shipping-1cd","text":null,"time_range":{"end":"2024-01-05T10:10:00Z","start":"2024-01-05T07:10:00Z"},"tool":"query_logs"}`
- Records: 12
- Time span: 2024-01-05T08:07:13.579214+00:00 – 2024-01-05T08:12:50.116187+00:00
  - error shipping-1cd: connection to orders-db-#d# reset during query (SQLSTATE #) (x6)
  - error shipping-1cd: write to orders-db-#d# failed: cannot execute UPDATE in a read-only transaction (x6)
- level: error
- service: shipping-1cd

### `E0002` query_metrics (complete)

- Query: `{"limit":25,"metric_name":"error_rate","service":"shipping-1cd","step_seconds":600,"time_range":{"end":"2024-01-05T10:10:00Z","start":"2024-01-05T07:10:00Z"},"tool":"query_metrics"}`
- Records: 13
- Time span: 2024-01-05T07:10:00+00:00 – 2024-01-05T09:10:00+00:00
  - error_rate shipping-1cd (x13)
- metric_name: error_rate
- service: shipping-1cd

### `E0003` get_deployments (complete)

- Query: `{"change_types":["config_change","deployment","feature_flag_change"],"limit":25,"service":"shipping-1cd","time_range":{"end":"2024-01-05T08:10:00Z","start":"2023-12-29T08:10:00Z"},"tool":"get_deployments"}`
- Records: 0
- Note: no records matched

### `E0004` get_dependencies (complete)

- Query: `{"depth":5,"limit":25,"service":"shipping-1cd","tool":"get_dependencies"}`
- Records: 7
  - upstream auth-d4f (service) hops=1 (x1)
  - upstream orders-db-9d9 (database) hops=1 (x1)
  - upstream profile-609 (service) hops=1 (x1)
  - downstream public-lb-63f (load_balancer) hops=1 (x1)
  - upstream session-cache-0da (cache) hops=1 (x1)
- component: auth-d4f, jobs-queue-21c, orders-db-9d9, profile-609, public-lb-63f
- direction: downstream, upstream

### `E0005` query_metrics (complete)

- Query: `{"limit":25,"metric_name":"latency_p99_ms","service":"shipping-1cd","step_seconds":300,"time_range":{"end":"2024-01-05T08:25:00Z","start":"2024-01-05T08:00:00Z"},"tool":"query_metrics"}`
- Records: 5
- Time span: 2024-01-05T08:00:00+00:00 – 2024-01-05T08:20:00+00:00
  - latency_p99_ms shipping-1cd (x5)
- metric_name: latency_p99_ms
- service: shipping-1cd

### `E0006` query_logs (complete)

- Query: `{"attributes":null,"levels":["error","warn"],"limit":25,"service":"orders-db-9d9","text":null,"time_range":{"end":"2024-01-05T08:15:00Z","start":"2024-01-05T08:05:00Z"},"tool":"query_logs"}`
- Records: 3
- Time span: 2024-01-05T08:07:06+00:00 – 2024-01-05T08:13:00+00:00
  - error orders-db-9d9: primary orders-db-#d#-node-a missed # heartbeats; marking it unreachable (x1)
  - warn orders-db-9d9: orders-db-#d#-node-b promoted to primary for orders-db-#d#; writes accepted again (x1)
  - warn orders-db-9d9: starting automatic failover of orders-db-#d#: promoting orders-db-#d#-node-b (x1)
- level: error, warn
- service: orders-db-9d9

### `E0007` query_metrics (complete)

- Query: `{"limit":25,"metric_name":"replication_lag_s","service":"orders-db-9d9","step_seconds":300,"time_range":{"end":"2024-01-05T08:20:00Z","start":"2024-01-05T07:50:00Z"},"tool":"query_metrics"}`
- Records: 6
- Time span: 2024-01-05T07:50:00+00:00 – 2024-01-05T08:15:00+00:00
  - replication_lag_s orders-db-9d9 (x6)
- metric_name: replication_lag_s
- service: orders-db-9d9
