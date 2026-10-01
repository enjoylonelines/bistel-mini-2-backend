# Dodam Deep Dive — Lifecycle & Full-stack Findings

> Backend baseline: `origin/develop@08ebc16`
> Frontend baseline: `origin/develop@50690f2`

## 1. Lifecycle invariant 1 — transport disconnect after accept

Question:

> If SSE disconnects after a durable request is accepted, does the same request still converge to one persisted result?

Added test:

`test_send_message_stream_disconnect_after_accept_persists_same_run`

Observed:

- one durable request fixture
- one user message
- one assistant message
- completed called once
- failed not called
- cancelled not called

Decision:

No backend production refactor. Current direct-routing implementation already satisfies this invariant in the focused cancellation scenario.

## 2. Lifecycle invariant 2 — cross-process deletion safety

The local `chat_cancel_registry` is process-memory only.

That initially suggested a multi-worker correctness risk.

A second discriminating test was added:

`test_send_message_stream_deleted_session_blocks_late_persist_without_local_cancel`

Test setup:

- local cancel event remains unset
- graph completes
- durable DB check reports session no longer exists

Observed:

- stream reaches `cancelled`
- assistant output is not persisted
- evidence/policy links are not persisted
- request is not completed
- request is marked cancelled

Conclusion:

The process-local cancel registry is primarily a **fast cancellation signal**, not the only correctness boundary.

The durable `session_exists` check suppresses late persistence even when local process memory did not receive a cancel event.

Remaining limitation:

- another worker may continue doing unnecessary generation work until the final durable check;
- therefore this is an efficiency/cancellation-latency issue, not currently a late-write correctness failure.

Do not introduce Redis/Kafka/distributed cancellation solely from the current evidence.

## 3. ChatGraphState duplicate-field reclassification

Initial audit flagged these as duplicate ownership:

Persisted `ChatSlot`:

- `profile`
- `pending`

Runtime `ChatGraphState`:

- `profile`
- `pending`
- `pending_intent`
- `awaiting_slots`

Code tracing shows:

1. `classify_intent()` reads persisted `slot.profile/slot.pending`;
2. it expands them into top-level working fields for routing/handlers;
3. handlers update working state;
4. persistence calls `build_next_slot()`;
5. only the rebuilt `slot_json` is persisted for the next turn.

Current interpretation:

> The top-level fields are primarily a transient working representation derived from persisted slot state.

Therefore this is **not yet a confirmed dual-source-of-truth bug**.

The remaining concern is naming/contract clarity and accidental handler divergence. It should only be refactored if a multi-turn regression test exposes inconsistent persisted output.

## 4. Frontend transport/recovery contract

Added transport-level test:

`apis/chatStreamClient.test.mjs`

Cases:

1. accepted → token → connection closes without terminal event
2. accepted → done

Observed:

- accepted request ID is delivered before `STREAM_INCOMPLETE`;
- caller can retain durable request ID for status recovery;
- completed stream does not emit an incomplete error.

Together with current `app/chat/page.js`, the existing design already implements:

```text
accepted request_id
  ↓
transport incomplete
  ↓
GET /chat/requests/{request_id}
  ↓
processing → poll
completed → apply persisted payload
failed/cancelled → terminal UI
```

## 5. Confirmed frontend semantic defect

Current recovery loop has a 45-second client-side polling budget.

Before this deep dive, when that budget expired while the backend still reported `processing`, the UI changed the request to:

```text
FAILED
"응답을 완료하지 못했어요. 다시 시도해 주세요."
```

This conflated:

- client recovery polling timeout

with:

- durable backend request failure.

That violates the central invariant:

> transport/recovery failure must not invent a durable run failure.

Minimal correction:

- recovery timeout now leaves the message in `PROCESSING`;
- pending request remains recoverable;
- only backend `failed` or `cancelled` produces the corresponding terminal UI state.

This is intentionally not a redesign of the chat page.

## 6. Verification

### Backend

```text
pytest tests/test_chat_*.py
186 passed
0 failed
```

Two existing warnings remain:

- passlib `crypt` deprecation
- one AsyncMock coroutine warning in `test_chat_slot.py`

Neither was introduced by this cycle.

### Frontend

Node focused tests:

- 10 passed
- 0 failed

ESLint:

- 0 errors
- 1 existing custom-font warning

Next production build:

- successful

## 7. Decision after Cycle 1 / early Cycle 3

Validated, no refactor needed:

- disconnect-after-accept durable completion
- late-write suppression after cross-process-style session deletion
- request ID preservation across incomplete SSE
- refresh/status-recovery architecture

Minimal production fix justified:

- do not render recovery polling timeout as durable request failure

Still open:

- browser-level recovery E2E
- request progress recovery after tab/session switch
- RAG broad candidate-selection failure
- answer grounding failure attribution
