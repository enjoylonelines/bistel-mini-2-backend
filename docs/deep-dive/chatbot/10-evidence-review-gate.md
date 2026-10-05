# Dodam Deep Dive — Evidence Review Gate

> Status: implemented with isolated fixtures; original-corpus quality pending

## Problem

`chat_message_evidence` already persisted retrieved chunks, but a policy-specific
response could still be emitted with no evidence attached. A stored evidence
link alone does not establish that an answer was eligible to be shown.

## Bounded contract

Before an assistant payload is built, a deterministic review checks policy
output completeness. It does not attempt sentence-level factual verification.

| Output | Required provenance | Verdict when missing |
| --- | --- | --- |
| policy list | chunk id + snippet + source URL | `BLOCKED` |
| eligibility result | chunk id + snippet + source URL | `BLOCKED` |
| application card | chunk id + snippet + source URL | `BLOCKED` |
| policy summary/key points | chunk id + snippet + source URL | `BLOCKED` |
| ordinary clarification or slot request | none | `PASS` |

`REVIEW_REQUIRED` applies when evidence exists but its provenance is incomplete,
for example a chunk/snippet without a source URL. The payload stays visible with
a disclaimer so a reviewer or user can verify it. `BLOCKED` strips policy
results and returns a safe message rather than presenting an unsupported policy
claim.

## Persisted trace

The payload carries `evidence_review` with:

- verdict: `PASS`, `REVIEW_REQUIRED`, or `BLOCKED`
- result types that required evidence
- evidence and complete-evidence counts
- deterministic reason codes

It is retained in the existing assistant-message `structured_json` and restored
when chat history is read. No database migration is required.

## Evidence collected

- `tests/test_evidence_review.py`: verdict rules and blocking behavior
- `tests/test_chat_e2e.py`: a compare response without evidence is blocked
- `tests/test_chat_controller.py`: review metadata survives message rehydration

The focused chat/evaluation set passed with `146 passed`, and the serializer
coverage passed with `76 passed` in the same checkout.

## Boundary and next gate

These tests use controlled fixtures. They prove the gate's control flow and
persistence, not policy-answer correctness, source freshness, or retrieval
quality on the original corpus. Those require the original policy/chunk snapshot
and a claim-level evaluation set.

The next bounded step is to define claim units and map each unit to evidence
chunk IDs before allowing a claim-level `PASS`. Until then, this response-level
gate is intentionally conservative.
