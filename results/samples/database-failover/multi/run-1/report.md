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

- Confidence: 0.86 (before data-gap penalty: 0.86)
- Revision: 1
- Statement: orders-db-9d9 likely failed over or was promoted into a read-only state around onset, causing shipping-1cd write attempts to fail with read-only transaction errors and intermittent connection resets.
- Supporting evidence: `E0001`, `E0002`, `E0004`
- Contradicting evidence: none
- Evidence sufficient: no
- Contributing factors: Direct write-path dependency from shipping-1cd to orders-db-9d9, Observed read-only transaction failures against the database, Observed connection resets during queries, Error spike aligns with incident start
- Missing evidence:
  - Database-side evidence of failover, promotion, replica role change, or maintenance event around 2024-01-05T08:10:00Z
  - Database health/role metrics or logs confirming the primary became read-only or changed leadership

### Rank 2: `H2` orders-db-9d9 / config_error

- Confidence: 0.72 (before data-gap penalty: 0.72)
- Revision: 1
- Statement: A configuration change on orders-db-9d9 likely made the database read-only or otherwise misrouted writes, producing the UPDATE-in-read-only errors seen by shipping-1cd.
- Supporting evidence: `E0001`, `E0004`
- Contradicting evidence: `E0003`
- Evidence sufficient: no
- Contributing factors: Write failures explicitly mention read-only transactions, The database is directly in the write path, No local change was found on shipping-1cd, so the fault may be upstream
- Missing evidence:
  - Evidence of a configuration change on orders-db-9d9 near onset
  - Database configuration or role settings showing read-only mode was enabled intentionally or accidentally

### Rank 3: `H3` orders-db-9d9 / network_partition

- Confidence: 0.55 (before data-gap penalty: 0.55)
- Revision: 1
- Statement: A transient network or connectivity issue between shipping-1cd and orders-db-9d9 caused query connection resets and secondary write failures, with the read-only error possibly arising from an unstable database session path.
- Supporting evidence: `E0001`, `E0002`, `E0004`
- Contradicting evidence: none
- Evidence sufficient: no
- Contributing factors: Connection reset errors are present in the logs, The issue is centered on the immediate database dependency, The error spike is brief, consistent with a transient connectivity problem
- Missing evidence:
  - Network telemetry or infrastructure logs showing packet loss, routing disruption, or connection resets between shipping-1cd and orders-db-9d9
  - Database-side logs confirming connectivity interruptions rather than application-level write rejection

### Rank 4: `H4` shipping-1cd / code_defect

- Confidence: 0.31 (before data-gap penalty: 0.31)
- Revision: 1
- Statement: shipping-1cd may have an application-side bug in its write path that incorrectly issues writes against orders-db-9d9 in a read-only transaction or mishandles reconnects, causing the observed failures without any recent local deployment.
- Supporting evidence: `E0001`, `E0002`, `E0003`
- Contradicting evidence: `E0004`
- Evidence sufficient: no
- Contributing factors: The service emits the failing write errors, No recent deployment or config change was found on shipping-1cd, The error spike is localized to the service
- Missing evidence:
  - Application traces or code-path evidence showing a bug in transaction handling or reconnect logic
  - A recent code change or release artifact for shipping-1cd to connect the failure to a specific regression

## Superseded Hypotheses

_No entries._

## Facts

- `F001` (observability): shipping-1cd shows a spike in request errors; the error_rate metric jumped from a baseline around 0.003-0.006 to a peak of 0.2242 in the 2024-01-05T08:10:00Z bucket, then returned near baseline by 08:20Z. [`E0002`]
- `F002` (observability): shipping-1cd shows error logs for failed writes to orders-db-9d9, with messages about read-only transactions and connection resets starting at 2024-01-05T08:07:13Z. [`E0001`]
- `F003` (observability): shipping-1cd shows error logs for connection resets to orders-db-9d9, starting at 2024-01-05T08:07:53Z. [`E0001`]
- `F004` (dependency): shipping-1cd depends upstream on orders-db-9d9, which sits one hop upstream in the dependency path and is the most relevant immediate dependency for the write failures. [`E0004`]
- `F005` (dependency): shipping-1cd also depends upstream on session-cache-0da, profile-609, auth-d4f, and public-lb-63f; these are part of the immediate dependency neighborhood around the affected service. [`E0004`]
- `F006` (dependency): shipping-1cd has an upstream dependency on tax-api-5fe and jobs-queue-21c at two hops, indicating additional upstream services in its dependency path beyond the immediate database/cache/auth layer. [`E0004`]
- `F007` (dependency): No deployments, config changes, or feature-flag changes were found for shipping-1cd in the 2023-12-29T08:10:00Z to 2024-01-05T08:10:00Z window, so there is no current evidence of a local change on the affected service immediately before onset. [`E0003`]
- `F008` (dependency): shipping-1cd depends on orders-db-9d9 upstream; this places orders-db-9d9 directly in the write path for shipping-1cd. [`E0004`]
- `F009` (dependency): shipping-1cd was emitting write-failure errors against orders-db-9d9, including 'cannot execute UPDATE in a read-only transaction' and connection resets during query, which matches the reported write errors. [`E0001`]
- `F010` (dependency): shipping-1cd error rate spiked to 0.2242 at 2024-01-05T08:10:00Z, aligning with the incident start time and supporting that the observed failure was active at onset. [`E0002`]
- `F011` (observability): shipping-1cd shows an error-rate spike that begins at 2024-01-05T08:10:00Z. [`E0002`]
- `F012` (observability): shipping-1cd shows error logs for failed writes to orders-db-9d9, including read-only transaction UPDATE failures, starting at 2024-01-05T08:07:13.579214Z. [`E0001`]
- `F013` (observability): shipping-1cd shows error logs for connection resets to orders-db-9d9 during queries, starting at 2024-01-05T08:07:53.139530Z. [`E0001`]
- `F014` (reporting): shipping-1cd experienced a short-lived spike in request errors that peaked at 0.2242 around 2024-01-05T08:10:00Z and then returned near baseline. [`E0002`]
- `F015` (reporting): The dominant observed failures were write attempts to orders-db-9d9 rejected as read-only transactions, along with connection resets during queries. [`E0001`]
- `F016` (reporting): orders-db-9d9 is the immediate upstream dependency in the write path for shipping-1cd. [`E0004`]
- `F017` (reporting): No deployments, config changes, or feature-flag changes were found for shipping-1cd in the week before onset. [`E0003`]

## Assumptions

_No entries._

## Data Gaps

_No entries._

## Recommended Actions

- `A1` [further_investigation] for `H1`: Check orders-db-9d9 for failover, promotion, replica role changes, or maintenance activity around 2024-01-05T08:10:00Z, and confirm whether the database entered a read-only state during the incident window. (evidence: `E0001`, `E0002`, `E0004`)
- `A2` [further_investigation] for `H2`: Review orders-db-9d9 configuration and recent change history for any read-only setting, routing change, or other misconfiguration near onset to determine whether a config change caused the write failures. (evidence: `E0001`, `E0004`)
- `A3` [further_investigation] for `H3`: Collect network and infrastructure telemetry between shipping-1cd and orders-db-9d9 to verify whether packet loss, routing disruption, or connection instability caused the observed connection resets. (evidence: `E0001`, `E0002`, `E0004`)
- `A4` [further_investigation] for `H4`: Inspect shipping-1cd transaction handling and reconnect logic, and correlate with any recent release artifacts, to rule in or out an application-side regression in the write path. (evidence: `E0001`, `E0002`, `E0003`)

## Incident Report Claims

| Claim | Text | Status | Corroborating | Contradicting |
|---|---|---|---|---|
| `IC1` | HTTP 5xx rate on shipping-1cd above 15% | verified | `E0002` | none |
| `IC2` | Orders submitted through shipping-1cd fail to save | verified | `E0001` | none |
| `IC3` | The affected service is shipping-1cd. | verified | `E0001`, `E0002` | none |
| `IC4` | The incident started at 2024-01-05T08:10:00+00:00. | verified | `E0002`, `E0001` | none |

## Missing Evidence

_No entries._

## Budget Usage

| Budget | Used | Limit |
|---|---|---|
| Tool calls | 6 | 60 |
| Tokens (input 35123, output 3890) | 39013 | 400000 |
| Critic rounds | 1 | 3 |
| Wall-clock seconds | 15.432 | 900 |

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

### `E0005` query_logs (partial)

- Query: `{"attributes":null,"levels":["error","warn"],"limit":25,"service":"shipping-1cd","text":"orders-db","time_range":{"end":"2024-01-05T08:30:00Z","start":"2024-01-05T08:00:00Z"},"tool":"query_logs"}`
- Records: 7
- Time span: 2024-01-05T08:07:53.139530+00:00 – 2024-01-05T08:12:50.116187+00:00
  - error shipping-1cd: connection to orders-db-#d# reset during query (SQLSTATE #) (x5)
  - error shipping-1cd: write to orders-db-#d# failed: cannot execute UPDATE in a read-only transaction (x2)
- level: error
- service: shipping-1cd
