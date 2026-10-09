# Dodam Deep Dive — RAG Evidence Trace Closure

> Run date: 2026-10-09 (Asia/Seoul)<br>
> Revision: working tree after `26c8e67`<br>
> Environment: isolated PostgreSQL `127.0.0.1:55432`, local PGVector, configured embedding provider

## Scope

`search_policy_chunks()` now persists only the final chunk set it returns after
policy filtering, structured-snippet exclusion, and optional section-aware
selection. The persisted path is:

```text
search_policy_chunks
  → returned PolicyRagSearchResult set
  → decision_run (RAG_RETRIEVAL)
  → decision_claim (RETRIEVED_EVIDENCE)
  → evidence_span
  → claim_evidence
```

The trace is retrieval provenance, not an asserted answer or a quality label.

## Isolated probe result

One explicitly approved non-personal query was sent to the configured embedding
provider: `산재근로자 심리상담 신청 방법`. The search was limited to policy ID `1`
and `top_k=3`.

| Measurement | Observed value |
| --- | ---: |
| Returned chunks | 3 |
| Returned chunk IDs | `20002`, `20001`, `20005` |
| Persisted `decision_run` rows | 1 |
| Persisted retrieval-evidence claims | 3 |
| Persisted `evidence_span` rows | 3 |
| Persisted distinct chunk IDs | 3 |
| Local search-and-trace elapsed time | 3621.173 ms |

The DB relationship was read back from the latest run: one decision run had
three claims, three evidence spans, and three distinct chunk IDs. All returned
roles were `reference` for this source document.

## Interpretation boundary

This verifies that the actual configured search path records the same final
three chunks it returned. It does **not** establish retrieval relevance,
Recall@K, answer groundedness, provider latency, real-user traffic, cost, or
production capacity. The elapsed value includes one configured embedding query,
local vector search, and trace writes in this isolated environment.

## Closure

Dodam closes this deep-dive cycle with two bounded guarantees:

1. Search-derived recommendation evidence can be traced through durable
   decision, claim, and span records.
2. Rerank execution preserves deterministic fallback and durable cancellation
   boundaries under controlled fake-provider faults and process-local admission.

Durable multi-worker queues, global provider admission, lease recovery, and
provider quota/cost policy remain intentionally out of scope for this cycle.
