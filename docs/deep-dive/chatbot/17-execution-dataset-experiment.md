# Versioned execution-control dataset experiment

Date: 2026-10-09  
Dataset: `experiments/chatbot/datasets/dodam_execution_v1.jsonl`

## Why a dataset experiment

The earlier 100-repeat run checked deterministic replay of one cancellation
timing. It is retained as a narrow regression, but it is not a portfolio
experiment. This document uses the same structure recommended by LLM
observability tools: freeze a dataset, run baseline and candidate on exactly
the same items, attach item-level scores, then compare aggregate results and
inspect individual failures.

The dataset remains local and synthetic. When explicitly invoked with
`--langfuse`, the same 24 items are exported as two Langfuse SDK experiment
runs: one for baseline and one for treatment. This is observability evidence
for the experiment itself, not production application instrumentation.

## Dataset v1

The 24 explicit items are a full control-flow matrix:

| Dimension | Values |
| --- | --- |
| candidate evidence fan-out | 2, 4, 8 candidates |
| process-local lane capacity | 1, 2 |
| durable cancellation timing | before any provider start; after the initially admitted calls start |
| fake provider outcome | success; error |

`2 × 3 × 2 × 2 = 24`. Candidate counts and capacities are experiment variables,
not production defaults. The fake provider is held behind an admission gate so
the cancellation transition is deterministic and observable.

## Compared variants

- **Baseline:** candidate evidence lane without a post-admission execution
  recheck, representing the preceding behavior.
- **Treatment:** the current evidence lane with the callback that production
  graph wiring resolves through durable recommendation ownership.

For each item, the experiment records provider calls started, calls started
after cancellation, prevented calls, estimated embedding input tokens avoided,
and provider-error classification. Scores validate the expected control-flow
contract; they are not answer-quality scores.

## Result

Command:

```bash
PYTHONPATH=. .venv/bin/python experiments/chatbot/run_execution_dataset_experiment.py \
  --output /tmp/dodam-execution-v1.json
```

| Metric | Baseline | Treatment |
| --- | ---: | ---: |
| dataset items | 24 | 24 |
| provider calls started | 112 | 18 |
| calls started after cancellation | 94 | 0 |
| prevented calls | 0 | 94 |
| estimated embedding input tokens avoided | 0 | 3,102 |
| control-flow score contracts passed | 24/24 for four core contracts | 24/24 for five core contracts |

On this fixed dataset, treatment reduced started fake-search calls by **83.929%**
and eliminated all 94 post-cancellation starts. The treatment's four basic
contracts plus the no-post-cancel-call score all passed 24/24. Baseline's
no-post-cancel-call score is 2/24 only because the two-candidate/capacity-two
after-admission cases have no waiting work left to start.

## Permitted portfolio claim

> 후보 수·lane 용량·취소 시점·provider 오류를 조합한 24개 versioned execution
> dataset에서, 취소 후 시작되는 RAG 검색을 94건에서 0건으로 제거하고 전체 fake
> search 시작 호출을 83.9% 줄였다. 각 scenario의 호출·오류·취소 contract를
> score로 검증했다.

The claim must retain `versioned local fake-search dataset` or equivalent
wording. It does not establish production cancellation frequency, provider
billing, real retrieval quality, terminal-write correctness, multi-worker
behavior, or global capacity.

## Langfuse export and receipt check

The export is opt-in, so normal local regression tests do not send any data.
It loads `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, and
`LANGFUSE_BASE_URL` from the local `.env` only when `--langfuse` is supplied.

```bash
PYTHONPATH=. .venv/bin/python experiments/chatbot/run_execution_dataset_experiment.py \
  --langfuse \
  --output /tmp/dodam-execution-v1-langfuse.json
```

The experiment runner is intentionally serial (`--langfuse-max-concurrency=1`)
to preserve the existing deterministic scenario execution. This is a runner
setting, not an application or provider capacity limit.

On 2026-10-09, the configured Langfuse project received two runs with 24 items
each: `dodam-execution-v1-baseline` (experiment ID `9f446eab1379eebb`) and
`dodam-execution-v1-treatment` (experiment ID `36ef1af81052eee9`). The
Experiments page showed 24 items and zero runner errors for each run. Its
item-level boolean scores reflected baseline `no_post_cancel_provider_call`
as false 22 / true 2, and treatment as true 24, matching the local result.

Only synthetic scenario ID, candidate count, lane capacity, controlled cancel
phase, controlled fake-provider outcome, and generated control-flow output are
exported. No user request, profile, policy text, real provider response,
provider token usage, billing value, or production traffic is exported.
