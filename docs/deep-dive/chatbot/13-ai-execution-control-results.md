# Dodam Deep Dive — Controlled Execution Results

> Run date: 2026-10-08 (Asia/Seoul)  
> Revision: `afb7b71` plus uncommitted harness/doc work at execution time  
> Environment: isolated PostgreSQL `127.0.0.1:55432`; in-process controlled fake provider

## Run result

The harness executed six single-request scenarios. It made six fake-provider
calls: two augmented completions, three deterministic fallbacks, and one
durably cancelled request. The cancelled request had no durable result write and
emitted one `LATE_WRITE_BLOCKED` event.

| Scenario | Terminal state | Rerank classification | Durable result write | Late write blocked |
| --- | --- | --- | --- | --- |
| fixed delay | `COMPLETED` | augmented | yes | no |
| long-tail delay | `COMPLETED` | augmented | yes | no |
| timeout | `COMPLETED` | fallback | yes | no |
| 429 | `COMPLETED` | fallback | yes | no |
| 5xx | `COMPLETED` | fallback | yes | no |
| cancel before completion | `CANCELLED` | no user-visible augmentation | no | yes |

Observed wall-clock timing in this small local run was p50 **3.984 ms** and p95
**31.456 ms**. These figures reflect deliberately tiny fake delays, one request
at a time, and local scheduling. They are not provider latency, an SLA, a p95
target, a capacity result, or a recommended concurrency setting.

## What this establishes

- The fallback path remains distinguishable from LLM augmentation.
- The token/status fence rejects this exercised late-completion path after a
  durable cancellation.
- The event sequence is queryable during execution before test-only rows are
  cleaned up.

## What remains unmeasured

- bounded admission, queue wait, in-flight overlap, and multi-worker/global
  coordination;
- token usage and costs from a provider response;
- real provider retries, quota semantics, and cancellation effectiveness;
- RAG corpus quality or user-facing recommendation quality.
