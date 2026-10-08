# Dodam Deep Dive — Controlled Execution Results

> Run date: 2026-10-08 (Asia/Seoul)  
> Revision: working tree after `4fc933b` (before the final commit for this run)<br>
> Environment: isolated PostgreSQL `127.0.0.1:55432`; in-process controlled fake provider

## Run result

The harness executed six single-request scenarios. It made six fake-provider
calls: two augmented completions, three deterministic fallbacks, and one
durably cancelled request. The in-flight cancellation had no durable result
write and emitted one `LATE_WRITE_BLOCKED` event.

| Scenario | Terminal state | Rerank classification | Durable result write | Late write blocked |
| --- | --- | --- | --- | --- |
| fixed delay | `COMPLETED` | augmented | yes | no |
| long-tail delay | `COMPLETED` | augmented | yes | no |
| timeout | `COMPLETED` | fallback | yes | no |
| 429 | `COMPLETED` | fallback | yes | no |
| 5xx | `COMPLETED` | fallback | yes | no |
| cancel before completion | `CANCELLED` | no user-visible augmentation | no | yes |

Observed wall-clock timing in this small local run was p50 **3.498 ms** and p95
**31.604 ms**. These figures reflect deliberately tiny fake delays, one request
at a time, and local scheduling. They are not provider latency, an SLA, a p95
target, a capacity result, or a recommended concurrency setting.

## What this establishes

- The fallback path remains distinguishable from LLM augmentation.
- The token/status fence rejects this exercised late-completion path after a
  durable cancellation.
- The event sequence is queryable during execution before test-only rows are
  cleaned up.

## Durable queued-cancellation probe

With an explicit capacity-one process-local lane, the probe admitted one
durable request, then claimed a second request and cancelled it while it waited
behind the first. The first completed after one fake-provider call. The second
ended `CANCELLED`, made **zero** provider calls, wrote no result, and emitted
`RERANK_DROPPED` after admission. Its observed local queue wait was **38.408
ms**.

This differs from the in-flight cancellation row: that row had already made
one provider call before cancellation, then the durable fence blocked its late
result write. The exercise therefore establishes a narrow control-flow
distinction only: queued cancellation can avoid a not-yet-started call; an
already-started call remains potentially wasted unless a provider-specific
cancellation contract is introduced and verified.

## Process-local lane probe

The same isolated run also released two simultaneous in-process fake-provider
tasks through a shared lane. Capacity was an experiment variable only.

| Per-process capacity | Observed peak in-flight | Queue wait p50 | Queue wait p95 |
| --- | ---: | ---: | ---: |
| 1 | 1 | 10.537 ms | 21.071 ms |
| 2 | 2 | 0.002 ms | 0.003 ms |

This demonstrates the process-local semaphore's expected control flow under a
two-task synthetic overlap. It does not compare production throughput, provider
429 behavior, cost, or multi-worker/global capacity, and does not select an
operator value for `RECOMMENDATION_RERANK_MAX_IN_FLIGHT`.

## What remains unmeasured

- bounded admission, queue wait, in-flight overlap, and multi-worker/global
  coordination;
- token usage and costs from a provider response;
- real provider retries, quota semantics, and cancellation effectiveness;
- RAG corpus quality or user-facing recommendation quality.
