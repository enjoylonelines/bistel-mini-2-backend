# Dodam Deep Dive — Candidate Evaluation Boundary and Section-aware Challenger

> Date: 2026-09-30
> Backend deep-dive branch: `deep-dive/dodam-chatbot-3axis`

## 1. Candidate-selection evaluator added

A separate evaluator now exists for the policy-discovery stage:

- `experiments/chatbot/candidate_selection_eval.py`
- `tests/test_candidate_selection_eval.py`

It measures only ranked policy IDs before evidence retrieval.

Metrics:

- Candidate Recall@K
- Candidate MRR
- miss count / miss case IDs

This intentionally does **not** reuse Section Hit@K as a candidate-selection metric.

## 2. Why no fresh candidate Recall number is reported yet

The current repository has recommendation flow examples in
`docs/RECOMMENDATION_SWAGGER_TEST_CASES.md`, but those cases describe:

- request inputs,
- lifecycle expectations,
- follow-up behavior,
- result-shape expectations,

and do not provide a reviewed expected-policy gold label for each recommendation case.

The retrieval gold set does contain `expected_policy_ids`, but those questions
were created to evaluate chunk retrieval and are not automatically valid labels
for `RecommendationCandidateService`, which consumes structured conditions and
rule filtering.

Therefore the deep dive does **not** invent a candidate Recall@K number by
relabeling the retrieval gold set.

A valid candidate benchmark requires a small reviewed fixture with:

- selected/merged condition input,
- allowed expected policy IDs,
- explicit reason why each policy is expected,
- optional acceptable alternatives.

Until that exists, the candidate evaluator is ready but the metric remains
unreported.

## 3. Production retrieval-scope contract

Added regression test:

`tests/test_recommendation_service_retrieval_scope.py`

It confirms that once recommendation candidates are selected, evidence search is
executed per candidate with:

`policy_ids=[candidate_policy_id]`

Therefore the system has two materially different retrieval layers:

```text
candidate discovery
    ↓
selected policy ids
    ↓
policy-scoped evidence retrieval
```

These layers must be evaluated separately.

## 4. R019 section-aware challenger

Historical policy-scoped vector retrieval repeatedly misses the expected
`신청 방법` section for R019 while retrieving `신청 기간`.

The current metadata abstraction maps both sections to the same broad
APPLICATION role.

The smallest challenger tested is a finer application subtype:

- method
- period

Experiment files:

- `experiments/chatbot/application_section_cases.jsonl`
- `experiments/chatbot/application_section_subtype.py`
- `experiments/chatbot/application_policy_section_inventory.json`
- `experiments/chatbot/section_aware_selector.py`

## 5. Offline results

### Application subtype classifier

First rule:

- generic word `기간` counted as a period signal

Result:

- 9/10

Failure:

R025 contains `쉰 기간` but asks **where to apply**, so the generic period token
was a false signal.

Refined period signals were limited to application-specific phrases such as:

- 신청 기간
- 접수 기간
- 모집 기간
- 기간 없이
- 언제 신청
- 신청/접수 기한
- 상시 모집/신청

Result on the 10-case exploratory fixture:

- 10/10

### Metadata section selector

Using the historical section inventory for the expected policy, the subtype-aware
selector chooses:

- `신청 방법` for method questions
- `신청 기간` for period questions

Result:

- 10/10 section-selection hits
- R019 selects chunk `24290000004` / `신청 방법`

## 6. Interpretation boundary

The 10/10 result is **not** a retrieval-accuracy improvement claim.

It is an offline upper-bound/diagnostic experiment because:

1. policy identity is already known;
2. the historical section inventory is available;
3. the subtype rule was refined after observing a failure;
4. there is no held-out application set;
5. vector latency/ranking is not measured in this selector.

What it does support:

> The current APPLICATION metadata role is too coarse to distinguish method
> from period, and section subtype is a lower-cost challenger worth live-testing
> before introducing a general reranker.

## 7. Larger-K question

The historical artifacts show policy 242 contains at least these seven distinct
standard chunks:

- 정리된 지원 조건
- 조건 구조
- 공식 지원대상 원문
- 지원 내용
- 신청 방법
- 신청 기간
- 유의 사항

The R019 Top-5 vector result excludes `신청 방법`.

However, the historical artifacts do not record R019 Top-7/Top-10 vector ranks,
so the exact larger-K rank cannot be claimed.

A fresh live query is still required to answer:

> Is the application-method chunk already in the larger vector candidate pool,
> or does first-stage vector search exclude it?

That distinction determines whether a reranker is justified.

## 8. Live rerun blocker

Fresh live retrieval remains blocked at the time of this cycle.

Deployment state:

- `GET /health/live` → HTTP 200
- `GET /health/ready` → `not_ready`
- database check → `InternalServerError`

No historical metric is relabeled as a fresh run.

## 9. Verification

Focused deep-dive + chat regression suite:

- 194 passed
- 0 failed

Existing warnings:

- passlib `crypt` deprecation
- one existing AsyncMock coroutine warning in `test_chat_slot.py`

## 10. Next gate

Proceed only with one of these:

1. DB readiness restored → run R019 at K=5/7/10 and the bounded application set;
2. create a reviewed recommendation candidate gold fixture, then run actual
   Candidate Recall@K/MRR through `RecommendationCandidateService`.

Do not add a reranker, vector DB, or embedding-model sweep before one of those
experiments discriminates the failure mode.
