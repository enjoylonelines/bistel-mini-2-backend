# Candidate-level RAG fan-out execution control

Date: 2026-10-09  
Revision: working tree after `f182526`

## Question

The recommendation result builder performs one evidence search per selected
candidate before LLM reranking. Can that fan-out be controlled without
pretending it is the same resource as LLM reranking, and what latency tradeoff
does that control introduce?

## Change and scope

`RecommendationService._search_evidences()` now optionally acquires a
`ProcessLocalEvidenceLane` immediately around each external chunk-search call.
The lane is separate from `ProcessLocalRerankLane`; the two calls have different
provider work, failure modes, and useful capacity controls.

`RECOMMENDATION_EVIDENCE_MAX_IN_FLIGHT` is intentionally unset by default.
When an environment explicitly sets it, its scope is one Python process only.
It is not a global, multi-worker, tenant, or provider-quota limit.

Per result summary, the service records the search-call count and, when the
lane is enabled, configured capacity, observed max in-flight, and queue wait
p50/p95. These are request-local observations, not a fleet metric.

## Controlled comparison

Command:

```bash
PYTHONPATH=. .venv/bin/python experiments/chatbot/controlled_rag_fanout_harness.py \
  --output /tmp/controlled-rag-fanout.json
```

Fixture: four candidate-level calls; each in-process fake chunk search sleeps
20 ms; five sequential repetitions per mode. The harness invokes the real
`RecommendationService._search_evidences()` fan-out path, but replaces only
the external chunk-search function. It uses no vector database, embedding
provider, durable queue, or production traffic.

| Mode | All calls completed | Observed max in-flight | Elapsed p50 | Elapsed p95 |
| --- | ---: | ---: | ---: | ---: |
| no evidence lane | 4/4 each run | 4 | 21.255 ms | 21.314 ms |
| evidence lane capacity 2 | 4/4 each run | 2 | 42.627 ms | 42.887 ms |
| evidence lane capacity 1 | 4/4 each run | 1 | 84.699 ms | 85.393 ms |

Capacity 2 queue-wait p50/p95 was approximately 10.2–10.6/20.5–21.3 ms per
run. Capacity 1 queue-wait p50/p95 was approximately 31.4–32.1/62.7–63.7 ms.

## What this establishes

- The candidate-level RAG peak can be bounded independently of rerank: 4 to 2
  or 1 in this controlled fixture.
- The bounded path completes the same four fake calls; it does not silently
  drop evidence work.
- Lower peak concurrency has a visible queue/elapsed-time cost. This result
  does not select a default capacity or show a universal latency improvement.

## What it does not establish

- provider latency, 429/5xx behavior, token cost, retrieval relevance, or
  production throughput;
- global capacity across workers or deploys;
- cancellation-before-admission for queued RAG work. The existing durable
  terminal fence still protects the final recommendation write, but avoiding
  queued search calls after cancellation needs request-lifecycle ownership at
  this earlier fan-out boundary.

The next human decision is whether the measured queue cost, provider quota,
and user-visible deadline justify enabling a per-process value, and whether
RAG queued-cancellation avoidance is worth a separate lifecycle integration.
