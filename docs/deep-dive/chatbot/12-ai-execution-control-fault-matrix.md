# Dodam Deep Dive — Controlled Fake-Provider Fault Matrix

> Scope: isolated local database plus in-process fake provider. This verifies control flow, not provider latency, pricing, or production capacity.

Harness: `experiments/chatbot/controlled_execution_fault_harness.py`

| Scenario | Fake-provider behavior | Expected durable outcome |
| --- | --- | --- |
| `fixed_delay` | short successful delay | `COMPLETED`, augmented result |
| `long_tail_delay` | longer successful delay within local deadline | `COMPLETED`, augmented result |
| `timeout` | delay exceeds rerank timeout | `COMPLETED`, labelled deterministic fallback |
| `rate_limit` | throws a 429-shaped error | `COMPLETED`, labelled deterministic fallback |
| `provider_5xx` | throws a 503-shaped error | `COMPLETED`, labelled deterministic fallback |
| `cancel_before_completion` | successful completion after caller cancellation | `CANCELLED`, no result write, `LATE_WRITE_BLOCKED` |

Each row creates a test-only local user and recommendation request, commits the
claim, runs exactly one fake-provider call, reads the durable event sequence,
and deletes its test-only user/request afterward. The policy corpus and any real
provider credential are not used.

The harness also has one two-request, capacity-one durable probe: it admits the
first request, cancels the second while that request waits in the process-local
lane, and verifies `RERANK_DROPPED` with zero provider calls. It distinguishes
that case from the single-request in-flight cancellation row, which has one
already-started fake-provider call and a blocked late write.

Neither slice measures or claims global concurrency, multi-worker coordination,
provider capacity, a cost saving, or a recommended concurrency value.
