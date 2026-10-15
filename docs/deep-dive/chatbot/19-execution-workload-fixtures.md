# Execution workload fixtures

Date: 2026-10-13
Dataset: `experiments/chatbot/datasets/dodam_execution_workload_v1.jsonl`

## Purpose

The existing versioned dataset establishes cancellation control-flow contracts.
This companion workload runner measures the same production RAG evidence-lane
code under bounded request concurrency. It is intentionally local and uses a
fake chunk searcher: it does not send HTTP traffic, call an LLM provider, use a
database, or represent production capacity.

The runnable fixture axes are:

| Profile | Fixtures | Primary measurement |
| --- | --- | --- |
| `normal` | successful RAG fan-out | elapsed time and lane wait baseline |
| `cancellation` | cancel before admission; cancel while queued | started/avoided/post-cancel calls |
| `fault` | controlled provider error | error classification under lane admission |
| `mixed` | normal + queued cancellation | interference under bounded concurrency |
| `all` | all six versioned fixtures | broad local regression only |

Run each single axis first, then the mixed profile. A mixed outcome must be
compared with its single-axis baselines before attributing a p95 regression to
lane saturation, provider errors, or cancellation.

## Commands

```bash
PYTHONPATH=. .venv/bin/python experiments/chatbot/run_execution_workload.py \
  --profile normal --repeat 20 --concurrency 1 --lane-capacity 2

PYTHONPATH=. .venv/bin/python experiments/chatbot/run_execution_workload.py \
  --profile cancellation --repeat 20 --concurrency 2 --lane-capacity 1

PYTHONPATH=. .venv/bin/python experiments/chatbot/run_execution_workload.py \
  --profile mixed --repeat 20 --concurrency 8 --lane-capacity 2 \
  --output /tmp/dodam-execution-workload-v1.json
```

## Reportable fields

- request elapsed p50/p95/max;
- request-local RAG lane queue-wait p95;
- provider calls started;
- calls that started after cancellation;
- calls avoided before provider start; and
- requests carrying a controlled provider error.

`post_cancel_provider_calls` is the primary cancellation safety assertion. A
tokenizer estimate and real-provider token usage remain separate measurements;
this fake-provider workload does not establish token cost, retrieval quality,
browser/SSE behavior, multi-worker behavior, or a Kubernetes capacity target.

## Fixed local baseline (2026-10-13)

The commands above were run with a 2 ms fake provider delay. The normal and
fault profiles used request concurrency 1; cancellation used 2; mixed used 4.
Every run shared a process-local evidence lane of capacity 2. These values are
a regression baseline for this machine and harness only, not an application
SLO, provider latency measurement, or capacity recommendation.

| Profile | Requests | Elapsed p50 / p95 / max | Lane wait p95 | Started / avoided calls | Post-cancel calls | Error requests |
| --- | ---: | --- | ---: | ---: | ---: | ---: |
| `normal` | 20 | 4.770 / 4.913 / 4.989 ms | 2.386 ms | 80 / 0 | 0 | 0 |
| `cancellation` | 40 | 4.808 / 5.237 / 99.048 ms | 4.715 ms | 80 / 160 | 0 | 0 |
| `fault` | 20 | 4.772 / 4.939 / 5.095 ms | 2.414 ms | 80 / 0 | 0 | 20 |
| `mixed` | 40 | 9.527 / 92.841 / 97.482 ms | 92.499 ms | 80 / 160 | 0 | 0 |

The mixed p95 is queueing created by the intentionally shared lane and local
event-loop scheduling; it is useful as an interference signal. It must not be
reported as live RAG latency. The important mixed-workload control result is
that all 160 queued, cancelled evidence searches were avoided and no call began
after its request had been marked cancelled.
