# Dodam Deep Dive — AI Execution Control Baseline

> Status: controlled fake-provider harness implemented; not a production capacity result

## Scope

The bounded slice is the recommendation request's final LLM rerank. Candidate
retrieval, rules, and assessment remain deterministic inputs to that rerank.
The harness uses the real durable `recommendation_request` repository against
the isolated local PostgreSQL container and an in-process fake provider.

## Durable control contract

- `PROCESSING` requests receive an `execution_token` through one atomic claim.
- Result and terminal writes require the same token while the row is still
  `PROCESSING`.
- `CANCELLED` wins a race with a late rerank completion; the late result write
  is rejected and emitted as `LATE_WRITE_BLOCKED`.
- LLM failure uses the deterministic result as `FALLBACK`; it is not counted as
  an LLM-augmented completion.

## Observability currently available

`recommendation_execution_event` is append-only and stores the request ID,
execution token, stage, outcome, error type, and small non-user-content details.

| Event | Meaning |
| --- | --- |
| `REQUEST_OFFERED` | durable request entered `PROCESSING` |
| `EXECUTION_CLAIMED` | one process-local runner claimed the request |
| `EXECUTION_TERMINAL` | augmented or fallback result persisted, or background failure recorded |
| `REQUEST_CANCELLED` | cancellation became durable |
| `LATE_WRITE_BLOCKED` | token/status fence rejected a late result write |

This is not a global queue ledger. No global or per-process concurrency limit,
queue wait target, retry policy, token usage accounting, or provider cost budget
has been selected.
