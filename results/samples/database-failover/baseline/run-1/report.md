# Investigation report: Write errors on shipping-1cd

## Incident

- Incident ID: `inc-c8c6cf544662`
- Affected service: shipping-1cd
- Start time: 2024-01-05T08:10:00+00:00
- Severity: sev2
- System: baseline (report built by baseline)

## Verdict

**inconclusive**

## Termination

- Termination reason: `final_answer`

## Hypotheses

### Rank 1: `H1` orders-db-9d9 / database_failover

- Confidence: 0.86 (before data-gap penalty: 0.86)
- Revision: 1
- Statement: orders-db-9d9 likely failed over to a read-only replica or otherwise became read-only around 08:07-08:10, causing shipping-1cd write attempts to fail and intermittent connection resets.
- Supporting evidence: `E0001`, `E0002`, `E0004`
- Contradicting evidence: none
- Evidence sufficient: no
- Contributing factors: Write attempts fail with 'cannot execute UPDATE in a read-only transaction', Intermittent SQLSTATE 08006 connection resets to orders-db-9d9, Error spike begins just before the reported start time
- Missing evidence:
  - Database-side logs or metrics confirming a failover, promotion, or replica read-only state change at the onset.
  - Replication lag / connection / failover metrics for orders-db-9d9 around 08:10 UTC.

### Rank 2: `H2` orders-db-9d9 / connection_pool_exhaustion

- Confidence: 0.42 (before data-gap penalty: 0.42)
- Revision: 1
- Statement: A database connectivity issue on orders-db-9d9 may have caused resets and secondary read-only transaction errors seen by shipping-1cd.
- Supporting evidence: `E0001`
- Contradicting evidence: none
- Evidence sufficient: no
- Contributing factors: Repeated connection reset errors, Could explain write failures as a downstream symptom
- Missing evidence:
  - DB connection metrics or pool saturation evidence around the incident window.
  - Logs from orders-db-9d9 showing pool exhaustion or connection churn.

### Rank 3: `H3` shipping-1cd / code_defect

- Confidence: 0.18 (before data-gap penalty: 0.18)
- Revision: 1
- Statement: shipping-1cd may contain a write-path defect that mishandles database transaction state, surfacing as read-only transaction errors.
- Supporting evidence: `E0001`
- Contradicting evidence: `E0003`
- Evidence sufficient: no
- Contributing factors: Errors are emitted by shipping-1cd itself, No recent shipping-1cd deployments were found in the pre-incident window
- Missing evidence:
  - Application release or config evidence for shipping-1cd outside the queried window, or code-level traces showing a local bug.
  - More detailed application logs showing the exact code path leading to the failed UPDATE.

### Rank 4: `H4` shipping-1cd / deploy_regression

- Confidence: 0.08 (before data-gap penalty: 0.08)
- Revision: 1
- Statement: A recent deployment or config change in shipping-1cd caused the write failures.
- Supporting evidence: none
- Contradicting evidence: `E0003`
- Evidence sufficient: no
- Contributing factors: Deployment/config changes are a common cause to check
- Missing evidence:
  - Any deployment/config/feature-flag change near 08:10 UTC for shipping-1cd.

## Superseded Hypotheses

_No entries._

## Facts

_No entries._

## Assumptions

_No entries._

## Data Gaps

_No entries._

## Recommended Actions

- `A1` [further_investigation] for `H1`: Check orders-db-9d9 for failover/promotion events, read-only mode transitions, and replication health around 08:07-08:10 UTC. (evidence: `E0001`, `E0002`, `E0004`)
- `A2` [mitigation] for `H1`: If the database is still read-only or unstable, restore primary writable access and verify shipping-1cd write success before reopening traffic. (evidence: `E0001`, `E0002`)
- `A3` [further_investigation] for `H2`: Inspect database connection metrics and server logs for pool exhaustion or connection churn to determine whether connectivity issues preceded the write failures. (evidence: `E0001`)
- `A4` [further_investigation] for `H3`: Review shipping-1cd application traces and any out-of-window releases/configuration to rule out a local write-path defect. (evidence: `E0001`, `E0003`)

## Incident Report Claims

| Claim | Text | Status | Corroborating | Contradicting |
|---|---|---|---|---|
| `IC1` | HTTP 5xx rate on shipping-1cd above 15% | verified | `E0002` | none |
| `IC2` | Orders submitted through shipping-1cd fail to save | verified | `E0001` | none |
| `IC3` | The affected service is shipping-1cd. | verified | `E0001`, `E0002`, `E0004` | none |
| `IC4` | The incident started at 2024-01-05T08:10:00+00:00. | verified | `E0001`, `E0002` | none |

## Missing Evidence

- [reasoning_agent] for `H1`: Database-side logs or metrics confirming a failover, promotion, or replica read-only state change at the onset.
- [reasoning_agent] for `H1`: Replication lag / connection / failover metrics for orders-db-9d9 around 08:10 UTC.
- [reasoning_agent] for `H2`: DB connection metrics or pool saturation evidence around the incident window.
- [reasoning_agent] for `H2`: Logs from orders-db-9d9 showing pool exhaustion or connection churn.
- [reasoning_agent] for `H3`: Application release or config evidence for shipping-1cd outside the queried window, or code-level traces showing a local bug.
- [reasoning_agent] for `H3`: More detailed application logs showing the exact code path leading to the failed UPDATE.
- [reasoning_agent] for `H4`: Any deployment/config/feature-flag change near 08:10 UTC for shipping-1cd.

## Budget Usage

| Budget | Used | Limit |
|---|---|---|
| Tool calls | 4 | 60 |
| Tokens (input 8484, output 1133) | 9617 | 400000 |
| Critic rounds | 0 | 3 |
| Wall-clock seconds | 5.088 | 900 |

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
