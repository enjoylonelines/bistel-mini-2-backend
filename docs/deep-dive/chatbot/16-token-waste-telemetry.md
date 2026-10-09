# Token and call-waste telemetry boundary

Date: 2026-10-09  
Revision: working tree after `cbf78d5`

## What is recorded

The recommendation path now keeps three non-interchangeable observations.

| Observation | Source | Meaning |
| --- | --- | --- |
| `llm_provider_token_usage` | raw LLM provider response, when supplied | actual input/output/total usage reported for a started rerank call |
| `evidence_search_avoided_call_count` | durable execution-owner recheck after RAG admission | candidate evidence searches prevented before the external search function starts |
| `evidence_estimated_embedding_input_tokens_avoided` | local `tiktoken` encoding for `text-embedding-3-large` | estimated input tokens of prevented embedding/search queries; not provider-reported usage or money |

`RAG_EVIDENCE_DROPPED` records zero started provider calls plus the avoided-call
count and estimated embedding input tokens. `RERANK_RELEASED` and terminal
events now include actual rerank usage when a provider adapter returned it.
An in-flight timeout/cancellation with no returned provider usage remains
`unknown`, never zero.

## Lifecycle behavior

`RecommendationGraphNodes.build_result()` passes the durable recommendation
execution-token check into `RecommendationService._search_evidences()`. When
the optional RAG lane admits an item, the service rechecks ownership immediately
before its external chunk search. A cancellation that won while an item was
queued therefore returns a skipped item instead of starting that provider call.

This is still a race boundary, not magic cancellation of an already-started
request: cancellation after the final recheck may leave one in-flight call. The
durable terminal fence remains responsible for preventing its result overwrite.

## Controlled records

### RAG queued cancellation

Command:

```bash
PYTHONPATH=. .venv/bin/python experiments/chatbot/controlled_rag_fanout_harness.py \
  --output /tmp/controlled-rag-fanout-token-telemetry.json
```

Fixture: four candidate evidence searches, capacity one, in-process fake
searcher. The first call begins; cancellation flips the execution callback
before the remaining three entries leave the lane queue.

| Started fake searches | Prevented calls | Estimated embedding input tokens prevented | Error |
| ---: | ---: | ---: | --- |
| 1 | 3 | 96 | none |

The 96-token value is from fixture query text via the local embedding tokenizer.
It is not a provider receipt, bill, production saving, or real-user statistic.
The harness itself does not use a database; the graph-node regression verifies
that the production path supplies the durable execution-owner check and emits
`RAG_EVIDENCE_DROPPED`.

### Rerank response usage

The existing isolated-DB controlled rerank harness now makes its fake provider
return a fixed usage fixture `{input: 120, output: 45, total: 165}` for a
successful call. Its recorded `RERANK_RELEASED` and terminal events preserve
that object; 429/5xx/timeout rows leave usage unavailable. This only verifies
telemetry plumbing. The numbers are not a model cost or real token benchmark.

## Remaining boundary

Before reporting actual token reduction, run an approved real-provider sample
with retained request-level usage metadata and a declared model/pricing date.
For prevented calls, report call avoidance and tokenizer-estimated input tokens
separately from provider-reported tokens. Completion tokens for a call that
never started are counterfactual and must not be presented as actual savings.
