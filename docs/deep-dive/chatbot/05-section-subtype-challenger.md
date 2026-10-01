# Dodam Deep Dive — Section-Subtype Challenger

> Date: 2026-09-30
> Status: offline diagnostic only
> Production code changed: no

## 1. Trigger

R019 is the only case that remains a section miss after the expected policy is
provided.

Question:

> 법률 문제를 무료로 물어보려면 어떤 기관을 찾아가야 하나요?

Expected section:

- `신청 방법`

Historical policy-scoped vector Top 5 repeatedly returned:

1. 신청 기간
2. 공식 지원대상 원문
3. 유의 사항
4. 정리된 지원 조건
5. 조건 구조

The expected `신청 방법` chunk exists: `24290000004`.
It appears for other questions about the same policy, but not for R019.

Across the repeated scoped benchmark, R019 remains a miss for both vector and
adaptive retrieval.

## 2. Why adaptive fallback does not fire

Historical `AdaptivePolicyRetriever` groups both:

- `신청 방법`
- `신청 기간`

under the coarse evidence role:

`APPLICATION`

The fallback condition only asks whether an APPLICATION-role hit is present.

R019 already retrieves `신청 기간`, so the system concludes that application
evidence exists and does not attempt a supplement even though the user asks
**where / which institution**, not **when**.

This is a metadata-granularity failure, not simply a vector-score failure.

## 3. Smallest challenger hypothesis

Hypothesis:

> Application queries contain enough lexical signal to distinguish
> application-method questions from application-period questions, so a finer
> section subtype can be used as a retrieval hint before introducing a general
> reranker.

This is intentionally narrower than:

- adding a reranker;
- changing embedding models;
- increasing vector K globally.

## 4. Offline diagnostic

Files:

- `experiments/chatbot/application_section_cases.jsonl`
- `experiments/chatbot/application_section_subtype.py`

The fixture contains the 10 historical application-category questions from the
audited retrieval gold set.

Subtypes:

- `method` → expected `신청 방법`
- `period` → expected `신청 기간`

### First rule

The initial period matcher treated the generic word `기간` as a period-query
signal.

Result:

- 9/10 correct
- 90%

Failure:

R025:

> 임신 준비 시술로 쉰 기간의 임금 보전은 어디에 신청하나요?

The word `기간` describes the leave period, not the application deadline.
The actual intent is application method.

### Refined rule

The period matcher was narrowed to application-specific expressions such as:

- 신청 기간
- 접수 기간
- 모집 기간
- 기간 없이
- 언제 신청
- 신청/접수 기한
- 상시 모집/신청

Result on the same 10-case diagnostic fixture:

- 10/10 correct
- 100%

## 5. Interpretation boundary

The 100% result is **not** a production-quality classifier claim.

Reasons:

- only 10 questions;
- the rules were refined after observing one failure;
- the same historical fixture is used for iteration and evaluation;
- no held-out set exists yet.

What the experiment does support:

1. the coarse APPLICATION role is too broad for at least one stable failure;
2. method-vs-period is a meaningful retrieval dimension;
3. a cheap section-aware hint is worth testing before a general reranker.

## 6. Next live experiment

Once DB readiness is restored, run R019 with policy 242 fixed.

Compare:

### Baseline

`vector(query, policy_ids=[242], top_k=5)`

### Challenger A

Section-aware candidate boost/filter using:

`application_subtype=method`

Target:

- `신청 방법` enters Top 5;
- no loss on the other 9 application cases;
- latency increase remains negligible relative to a second LLM/reranker pass.

Only if the expected section already appears in a larger vector candidate pool
but remains below Top 5 should a reranker be considered.

## 7. Deployment blocker discovered during rerun attempt

The deployed backend currently reports:

- `/health/live`: HTTP 200
- `/health/ready`: database = error / not_ready

Therefore a fresh live R019 retrieval run cannot be treated as valid today.

The deployed process is alive, but the application is not operationally ready.
This is preserved as a Full-stack Delivery finding rather than hidden or replaced
with historical numbers.
