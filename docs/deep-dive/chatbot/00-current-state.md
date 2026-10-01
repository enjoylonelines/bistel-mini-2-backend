# Dodam Deep Dive — Current State Baseline

> Baseline date: 2026-09-30
> Backend revision: `08ebc167384cca490303059dcb927887c300bd12`
> Frontend revision: `50690f27ff6689c2f810509fab96afb5bc913ad1`
> Backend branch for deep dive: `deep-dive/dodam-chatbot-3axis`

## 1. Local source of truth

Fresh clones:

- Backend: `~/Projects/bistel-mini-2-backend`
- Frontend: `~/Projects/bistel-mini-2-frontend`

Legacy copy under `~/Documents/kosa-course/projects/mini-2` is not used as source of truth.

## 2. Baseline test evidence

### Backend

Environment:

- Python 3.12
- dependencies resolved from `requirements.txt` via `uv`

Command:

```bash
uv run --python /opt/homebrew/bin/python3.12 --with-requirements requirements.txt \
  python -m pytest -q \
  tests/test_chat_request_repository.py \
  tests/test_chat_service.py \
  tests/test_chat_e2e.py \
  tests/eval/test_live_eval_metrics.py
```

Result:

- 49 passed
- 0 failed
- elapsed: 13.94 s

Python 3.14 was not used because the pinned `pydantic-core==2.33.2` / PyO3 dependency does not support that interpreter combination in the current environment.

### Frontend

Command:

```bash
node --test app/chat/chatProgress.test.mjs
```

Result:

- 8 passed
- 0 failed

## 3. Existing lifecycle capabilities

Current backend already provides:

- durable `chat_request` row
- `processing/completed/failed/cancelled` states
- idempotency key lookup
- duplicate request prevention at persistence boundary
- persisted response payload for completed requests
- per-request status API
- latest incomplete request API
- stale processing cleanup
- SSE events with request ID
- explicit separation between transport disconnect and user cancellation
- final persistence before `done`

Current frontend already provides:

- request ID capture from `accepted`
- pending request persistence in browser-side state/storage
- request status polling
- latest incomplete request recovery
- refresh/reconnect recovery
- retry/cancel UI state
- idempotency key transmission

Therefore the deep dive must not claim these as newly implemented features unless a measured defect requires a change.

## 4. Structural defect candidates

### 4.1 Duplicate chat state representation

Persisted slot state contains `profile` and `pending`, while graph runtime also exposes top-level `profile`, `pending`, `pending_intent`, and `awaiting_slots`.

This is a defect candidate, not yet a confirmed bug.

### 4.2 Oversized orchestration modules

Observed sizes:

- `app/services/ai_request_lifecycle_service.py`: ~83 KB
- `app/services/chat/chat_service.py`: ~45 KB
- `app/ai/nodes/chat/chat_nodes.py`: ~27 KB
- frontend `app/chat/page.js`: ~90 KB

Size alone is not considered a defect. Refactoring requires evidence of mixed ownership, change coupling, or failure isolation problems.

### 4.3 Process-local cancellation fast path

`app/services/chat/chat_cancel_registry.py` stores cancellation events in process memory.

Persistent database/session existence checks protect final persistence, so the primary risk is not necessarily duplicate writes; the remaining risk is delayed cancellation and wasted work across process boundaries.

### 4.4 Compatibility normalization breadth

Frontend `apis/chatApi.js` accepts many field aliases and historic shapes. The current backend contract should be measured before removing any compatibility branches.

## 5. Existing branch evidence not merged into develop

### `origin/feat/chat-retriever-layer`

12 commits ahead of develop.

Contains:

- pluggable retrieval abstraction
- multiple retriever benchmark
- goldset leakage correction
- label audit
- hybrid fusion tuning
- adaptive fallback

### `origin/refactor/chat-handler-result-lifecycle`

33 commits ahead of develop.

Contains the retriever work plus:

- explicit handler outputs
- AI request recovery hardening
- generated grounding evaluation
- deployment/CI work

These branches are evidence sources and candidate implementation sources. They are not automatically canonical.

## 6. First lifecycle discriminating experiment

Question:

> If the SSE transport is cancelled after the backend has accepted the request, does the same durable run still complete and persist exactly one assistant result?

Added regression test:

`test_send_message_stream_disconnect_after_accept_persists_same_run`

Observed result on current develop implementation:

- test passed
- exactly one durable request fixture used
- one user message + one assistant message persisted
- request marked completed
- request not marked cancelled
- request not marked failed

Conclusion:

The main direct-routing path already satisfies the first lifecycle invariant under the unit-level cancellation scenario. No production refactor is justified from this scenario alone.

## 7. Next discriminating questions

1. Does the same invariant hold for special follow-up paths where execution occurs before the normal streaming task is created?
2. Can session deletion in another process stop or safely suppress late persistence?
3. Does graph state duplication create observable state divergence after multi-turn slot/follow-up flows?
4. Can the unmerged retrieval benchmark be reproduced against the current develop data and schema?
