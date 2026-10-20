# Investigation report: Write errors on shipping-1cd

## Incident

- Incident ID: `inc-c8c6cf544662`
- Affected service: shipping-1cd
- Start time: 2024-01-05T08:10:00+00:00
- Severity: sev2
- System: baseline (report built by baseline)

## Verdict

**conclusive**

## Termination

- Termination reason: `final_answer`

## Hypotheses

### Rank 1: `H1` orders-db-9d9 / database_failover

- Confidence: 0.93 (before data-gap penalty: 0.93)
- Revision: 1
- Statement: orders-db-9d9 likely entered a failover/read-only state or otherwise became unavailable for writes, causing shipping-1cd to fail order saves.
- Supporting evidence: `E0001`, `E0005`
- Contradicting evidence: none
- Evidence sufficient: yes
- Contributing factors: Repeated write errors explicitly mention read-only transactions against orders-db-9d9., Connection resets during queries indicate database instability., Replication lag spiked sharply around the incident start.

### Rank 2: `H2` orders-db-9d9 / network_partition

- Confidence: 0.55 (before data-gap penalty: 0.55)
- Revision: 1
- Statement: A transient network or connectivity issue between shipping-1cd and orders-db-9d9 caused query resets and write failures.
- Supporting evidence: `E0001`
- Contradicting evidence: `E0005`
- Evidence sufficient: no
- Contributing factors: Logs show SQLSTATE 08006 connection resets., The failure is upstream of the affected service.
- Missing evidence:
  - Database-side telemetry or logs showing whether connectivity errors coincided with failover, maintenance, or node loss.

### Rank 3: `H3` shipping-1cd / code_defect

- Confidence: 0.18 (before data-gap penalty: 0.18)
- Revision: 1
- Statement: A defect in shipping-1cd's write path caused it to issue invalid updates or mishandle database errors.
- Supporting evidence: `E0001`
- Contradicting evidence: `E0003`, `E0005`
- Evidence sufficient: no
- Contributing factors: The service emits the observed failures., Application bugs can surface as write errors.
- Missing evidence:
  - Recent shipping-1cd deployment or config changes near the incident start that could explain a regression.

### Rank 4: `H4` shipping-1cd / deploy_regression

- Confidence: 0.08 (before data-gap penalty: 0.08)
- Revision: 1
- Statement: A recent shipping-1cd deployment introduced the write failures.
- Supporting evidence: none
- Contradicting evidence: `E0003`
- Evidence sufficient: no
- Contributing factors: Deploy regressions are a common cause of sudden write-path failures.
- Missing evidence:
  - Any deployment/config/feature-flag change for shipping-1cd in the lookback window.

### Rank 5: `H5` orders-db-9d9 / resource_exhaustion

- Confidence: 0.04 (before data-gap penalty: 0.04)
- Revision: 1
- Statement: orders-db-9d9 was overloaded or resource constrained, leading to lag and intermittent write failures.
- Supporting evidence: `E0005`
- Contradicting evidence: `E0001`
- Evidence sufficient: no
- Contributing factors: Replication lag was elevated for a sustained period., Resource pressure can degrade database write availability.
- Missing evidence:
  - Database CPU, memory, disk, and connection metrics around the incident window.

## Superseded Hypotheses

_No entries._

## Facts

_No entries._

## Assumptions

_No entries._

## Data Gaps

_No entries._

## Recommended Actions

- `A1` [mitigation] for `H1`: Treat orders-db-9d9 as the primary fault domain: verify replica/primary role, restore write availability, and confirm shipping-1cd can save orders again. (evidence: `E0001`, `E0005`)
- `A2` [further_investigation] for `H1`: Review database failover and replication events around 2024-01-05T08:10Z to determine why writes were rejected as read-only. (evidence: `E0001`, `E0005`)
- `A3` [further_investigation] for `H2`: Check for network errors, node restarts, or routing issues between shipping-1cd and orders-db-9d9 if failover evidence is absent. (evidence: `E0001`)
- `A4` [further_investigation] for `H3`: If database-side causes are ruled out, inspect shipping-1cd error handling and recent code paths that issue UPDATE statements. (evidence: `E0001`, `E0003`)

## Incident Report Claims

| Claim | Text | Status | Corroborating | Contradicting |
|---|---|---|---|---|
| `IC1` | HTTP 5xx rate on shipping-1cd above 15% | verified | `E0002` | none |
| `IC2` | Orders submitted through shipping-1cd fail to save | verified | `E0001` | none |
| `IC3` | The affected service is shipping-1cd. | verified | `E0001`, `E0004` | none |
| `IC4` | The incident started at 2024-01-05T08:10:00+00:00. | verified | `E0001`, `E0002` | none |

## Missing Evidence

_No entries._

## Budget Usage

| Budget | Used | Limit |
|---|---|---|
| Tool calls | 5 | 60 |
| Tokens (input 20242, output 1354) | 21596 | 400000 |
| Critic rounds | 0 | 3 |
| Wall-clock seconds | 7.188 | 900 |

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

- Query: `{"limit":25,"metric_name":"replication_lag_s","service":"orders-db-9d9","step_seconds":600,"time_range":{"end":"2024-01-05T10:10:00Z","start":"2024-01-05T07:10:00Z"},"tool":"query_metrics"}`
- Records: 13
- Time span: 2024-01-05T07:10:00+00:00 – 2024-01-05T09:10:00+00:00
  - replication_lag_s orders-db-9d9 (x13)
- metric_name: replication_lag_s
- service: orders-db-9d9
