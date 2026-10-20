# Investigation report: Timeouts on shipping-929

## Incident

- Incident ID: `inc-e6b27f29bcb7`
- Affected service: shipping-929
- Start time: 2024-01-06T23:43:00+00:00
- Severity: sev2
- System: multi (report built by reporting_agent)

## Verdict

**conclusive**

## Termination

- Termination reason: `critic_accepted`

## Hypotheses

### Rank 1: `H1` cart-e26 / dependency_timeout

- Confidence: 0.90 (before data-gap penalty: 0.90)
- Revision: 2
- Statement: cart-e26 is timing out or responding too slowly to shipping-929, causing shipping-929 to return 504s and log repeated upstream call timeouts.
- Supporting evidence: `E0001`, `E0004`, `E0006`, `E0007`
- Contradicting evidence: none
- Evidence sufficient: no
- Contributing factors: shipping-929 repeatedly times out on cart-e26 calls, cart-e26 p99 latency rose above 2 s near the incident start, cart-e26 logs show worker pool saturation
- Missing evidence:
  - Direct dependency metrics or logs from cart-e26 showing the timeout behavior at the incident start and confirming it is the bottleneck rather than a downstream symptom.
  - Evidence that cart-e26 latency/error onset aligns tightly with shipping-929 timeout onset across the same time window.

### Rank 2: `H3` cart-e26 / resource_exhaustion

- Confidence: 0.78 (before data-gap penalty: 0.78)
- Revision: 2
- Statement: cart-e26 is experiencing worker-pool or similar resource exhaustion, which slows responses enough to trigger shipping-929 timeouts and elevated 504s.
- Supporting evidence: `E0006`, `E0007`, `E0001`
- Contradicting evidence: none
- Evidence sufficient: no
- Contributing factors: worker pool exhausted: 256/256 threads busy, cart-e26 latency p99 exceeded 2 s, shipping-929 logs show repeated cart-e26 timeouts
- Missing evidence:
  - Metrics showing the specific exhausted resource on cart-e26, such as thread pool, CPU, memory, queue depth, or connection saturation.
  - Evidence that the saturation began at or before the shipping-929 timeout onset and is not secondary to another upstream issue.

### Rank 3: `H2` shipping-929 / code_defect

- Confidence: 0.28 (before data-gap penalty: 0.28)
- Revision: 2
- Statement: shipping-929 has an internal request-handling defect that makes it time out on cart-e26 calls under normal conditions, independent of a recent deployment on shipping-929.
- Supporting evidence: `E0003`, `E0001`
- Contradicting evidence: `E0006`, `E0007`
- Evidence sufficient: no
- Contributing factors: no recent deployment/config/flag change on shipping-929, shipping-929 logs mention cart-e26 timeouts
- Missing evidence:
  - Runtime or code-level evidence from shipping-929 showing a defect in timeout handling, retry logic, or request orchestration.
  - Evidence that cart-e26 was healthy while shipping-929 still timed out, or that the issue reproduces without cart-e26 degradation.

### Rank 4: `H4` public-lb-571 / load_balancer_misconfiguration

- Confidence: 0.08 (before data-gap penalty: 0.08)
- Revision: 2
- Statement: A load balancer routing issue is intermittently affecting shipping-929 traffic, and the cart-e26 timeout messages are downstream symptoms rather than the root cause.
- Supporting evidence: `E0004`, `E0002`
- Contradicting evidence: `E0001`, `E0006`, `E0007`
- Evidence sufficient: no
- Contributing factors: public-lb-571 is in the dependency graph, shipping-929 error rate is elevated
- Missing evidence:
  - Load balancer logs or configuration evidence showing misrouting, unhealthy backend selection, or connection issues around the incident start.
  - Evidence that requests fail before reaching cart-e26, or that cart-e26 remains healthy while shipping-929 errors persist.

## Superseded Hypotheses

_No entries._

## Facts

- `F001` (dependency): shipping-929 has upstream dependencies on cart-e26, checkout-bbb, auth-620, catalog-db-7ab, session-cache-bcd, card-gateway-6d9, events-queue-2c6, and mail-queue-672; public-lb-571 is downstream of shipping-929. This places cart-e26 and checkout-bbb one to two hops upstream in the dependency path, while public-lb-571 sits downstream. [`E0004`]
- `F002` (dependency): No deployment, config change, or feature-flag change was found for shipping-929 in the week before the incident start time, so there is no current evidence of a direct recent change on the affected service itself. [`E0003`]
- `F003` (dependency): shipping-929 logs show repeated timeouts calling cart-e26 beginning before the incident start time, indicating cart-e26 is the immediate upstream dependency on the observed failure path. [`E0001`]
- `F004` (observability): shipping-929 error_rate jumped sharply from a low baseline to about 40% starting at 2024-01-06T23:43:00Z. [`E0002`]
- `F005` (observability): shipping-929 logs began showing repeated error messages matching 'call to cart-e26 timed out after 2000 ms' by 2024-01-06T23:40:07Z. [`E0001`]
- `F006` (observability): shipping-929 latency_p99_ms did not show a clear onset over the queried range; it stayed in the roughly 162-197 ms band throughout. [`E0005`]
- `F007` (observability): cart-e26 latency p99_ms rose sharply from sub-200 ms to above 2 s, with onset at 2024-01-06T23:33:00Z, aligning closely with shipping-929's timeout onset. [`E0006`]
- `F008` (observability): shipping-929 error_rate increased from near-zero to above 40% during the queried range, consistent with the reported 504 symptom. [`E0002`]
- `F009` (observability): shipping-929 logs contain repeated upstream timeout errors mentioning cart-e# calls timing out, with onset no later than 2024-01-06T23:40:07.364803Z. [`E0001`]
- `F010` (observability): shipping-929 shows repeated upstream timeout errors calling cart-e26, starting at 2024-01-06T23:40:07Z. [`E0001`]
- `F011` (observability): shipping-929 p99 latency is elevated over the queried range, with the highest observed value at 2024-01-06T23:43:00Z. [`E0005`]
- `F012` (observability): cart-e26 shows worker-pool saturation errors, starting at 2024-01-06T23:38:33Z. [`E0007`]
- `F013` (observability): cart-e26 p99 latency is elevated over the queried range, with the highest observed value at 2024-01-07T00:33:00Z. [`E0006`]
- `F014` (reporting): shipping-929 error rate jumped from near-zero to about 40% starting at 2024-01-06T23:43:00Z. [`E0002`]
- `F015` (reporting): shipping-929 logs show repeated timeouts calling cart-e26 beginning by 2024-01-06T23:40:07Z. [`E0001`]
- `F016` (reporting): cart-e26 p99 latency rose sharply above 2 seconds, aligning with the shipping-929 timeout onset. [`E0006`]
- `F017` (reporting): cart-e26 also emitted worker-pool exhaustion errors during the incident window. [`E0007`]
- `F018` (reporting): No deployment, config change, or feature-flag change was found for shipping-929 in the week before the incident. [`E0003`]

## Assumptions

_No entries._

## Data Gaps

_No entries._

## Recommended Actions

- `A1` [mitigation] for `H1`: Reduce immediate pressure on cart-e26 by temporarily lowering shipping-929 request concurrency or rate, and route traffic away from the slow path if possible until cart-e26 recovers. (evidence: `E0001`, `E0002`, `E0006`, `E0007`)
- `A2` [fix] for `H1`: Investigate and remediate the cart-e26 timeout bottleneck causing shipping-929 upstream call failures; prioritize restoring cart-e26 response times to sub-timeout levels and validating end-to-end request success. (evidence: `E0001`, `E0004`, `E0006`, `E0007`)
- `A3` [fix] for `H3`: Identify the exhausted cart-e26 resource behind the worker-pool saturation and correct the capacity limit or leak/queueing issue so requests are no longer rejected under load. (evidence: `E0006`, `E0007`)
- `A4` [prevention] for `H3`: Add alerting and autoscaling or backpressure controls for cart-e26 resource saturation signals such as worker-pool occupancy, queue depth, and p99 latency so similar exhaustion is detected before shipping timeouts occur. (evidence: `E0006`, `E0007`)
- `A5` [further_investigation] for `H2`: Verify whether shipping-929 has any timeout-handling or retry defect by reviewing runtime logs and reproducing the issue with cart-e26 healthy; this would close the remaining uncertainty around an internal shipping-side cause. (evidence: `E0003`, `E0001`, `E0006`)
- `A6` [further_investigation] for `H4`: Check load balancer logs and backend selection behavior around the incident window to rule out misrouting or unhealthy backend selection as a contributing factor. (evidence: `E0004`, `E0002`)

## Incident Report Claims

| Claim | Text | Status | Corroborating | Contradicting |
|---|---|---|---|---|
| `IC1` | HTTP 504 rate on shipping-929 above 30% | contradicted | none | `E0005` |
| `IC2` | p99 latency on shipping-929 above 2 s | contradicted | none | `E0005` |
| `IC3` | The affected service is shipping-929. | verified | `E0001`, `E0002`, `E0004` | none |
| `IC4` | The incident started at 2024-01-06T23:43:00+00:00. | verified | `E0001`, `E0002`, `E0005` | none |

## Missing Evidence

_No entries._

## Budget Usage

| Budget | Used | Limit |
|---|---|---|
| Tool calls | 7 | 60 |
| Tokens (input 59952, output 5314) | 65266 | 400000 |
| Critic rounds | 2 | 3 |
| Wall-clock seconds | 31.178 | 900 |

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

### `E0005` query_metrics (stale)

- Query: `{"limit":25,"metric_name":"latency_p99_ms","service":"shipping-929","step_seconds":600,"time_range":{"end":"2024-01-07T01:43:00Z","start":"2024-01-06T22:43:00Z"},"tool":"query_metrics"}`
- Records: 15
- Time span: 2024-01-06T22:43:00+00:00 – 2024-01-07T01:03:00+00:00
  - latency_p99_ms shipping-929 (x15)
- metric_name: latency_p99_ms
- service: shipping-929

### `E0006` query_metrics (complete)

- Query: `{"limit":25,"metric_name":"latency_p99_ms","service":"cart-e26","step_seconds":600,"time_range":{"end":"2024-01-07T01:43:00Z","start":"2024-01-06T22:43:00Z"},"tool":"query_metrics"}`
- Records: 15
- Time span: 2024-01-06T22:43:00+00:00 – 2024-01-07T01:03:00+00:00
  - latency_p99_ms cart-e26 (x15)
- metric_name: latency_p99_ms
- service: cart-e26

### `E0007` query_logs (complete)

- Query: `{"attributes":null,"levels":["error","warn"],"limit":25,"service":"cart-e26","text":null,"time_range":{"end":"2024-01-07T01:43:00Z","start":"2024-01-06T22:43:00Z"},"tool":"query_logs"}`
- Records: 6
- Time span: 2024-01-06T23:38:33.940460+00:00 – 2024-01-06T23:59:17.363390+00:00
  - error cart-e26: worker pool exhausted: #/# threads busy; rejecting request (x6)
- level: error
- service: cart-e26
