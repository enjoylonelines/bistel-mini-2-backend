# Dodam Deep Dive — Retrieval Failure Decomposition

> Evidence source: historical retrieval artifacts from
> `origin/refactor/chat-handler-result-lifecycle@ab56916`.
>
> Current implementation baseline remains `origin/develop@08ebc16`.

## 1. Why the evaluation boundary changed

The earlier 50-case benchmark compared SQL, vector, hybrid, and adaptive retrieval
over the whole policy corpus.

That benchmark is useful, but code tracing shows it mixes two different product
problems:

1. **policy discovery / candidate selection**
2. **evidence retrieval inside an already selected policy**

The production path often already has a policy candidate before evidence lookup.

Examples:

- `PolicySummaryGraphRunner` calls `search_policy_chunks(..., policy_ids=[policy_id])`
- `RecommendationService` searches evidence separately for each selected candidate
  with `policy_ids=[policy_id]`
- eligibility lifecycle evidence search also uses the resolved policy ID

Therefore broad chunk retrieval must not be treated as the only or most
representative RAG metric.

## 2. Historical vector failure decomposition

Broad vector artifact:

- total cases: 50
- full evidence pass: 40
- expected policy missing from Top 5: 7
- expected policy present but expected section missing: 3

### Candidate-selection failures

- R002
- R008
- R023
- R025
- R029
- R039
- R044

Rate: **7/50 = 14%**

### Section-selection failures after policy hit

- R006
- R019
- R024

Rate: **3/50 = 6%**

This means 70% of the 10 broad vector evidence failures were failures to surface
the expected policy in Top 5, not failures to choose the right section after the
policy was found.

## 3. Oracle policy-scope diagnostic

The same 50 cases were historically run with retrieval constrained to the
expected policy.

Scoped vector result:

- expected section hit: 49/50 = 98%
- section miss: only R019

All seven broad policy-miss cases become section hits when the expected policy is
provided.

This is **not a production accuracy claim** because the expected policy is an
oracle input.

It is a diagnostic upper-bound experiment.

What it proves:

- expected evidence exists for the seven broad policy misses;
- the chunks are retrievable with the same vector mechanism once policy identity
  is known;
- those failures should not initially be blamed on chunk absence or chunk
  boundaries.

## 4. Remaining true within-policy retrieval failure

R019:

> 법률 문제를 무료로 물어보려면 어떤 기관을 찾아가야 하나요?

Expected:

- policy 242
- section `신청 방법`

Broad vector already identifies policy 242, but Top 5 sections are:

1. 신청 기간
2. 공식 지원대상 원문
3. 유의 사항
4. 정리된 지원 조건
5. 조건 구조

Policy-scoped vector still misses `신청 방법`.

Therefore R019 is materially different from the seven policy-selection misses.

Candidate hypotheses:

- application-method text has weak semantic similarity to the query;
- section metadata/evidence-role should contribute to ranking;
- expected section content may use institution/contact wording not represented by
  the question;
- query construction for application questions is insufficient.

This is the best current case for a section-aware retrieval challenger.

## 5. Hybrid interpretation

Historical broad hybrid improves Policy Hit@5 from 86% to 88%.

Inspection shows the policy-level gain comes from R044, where lexical evidence
moves policy 289 into rank 1.

It does not recover the other six policy misses.

Therefore:

- hybrid is evidence that lexical signals can recover some candidate-selection
  misses;
- it is not evidence that always-on hybrid solves the dominant failure class;
- its stored latency cost remains much larger than its measured gain.

## 6. Architecture implication

The RAG deep dive is now split into two explicit layers.

```text
User question
    |
    v
Policy discovery / candidate selection
    |
    | selected policy IDs
    v
Policy-scoped evidence retrieval
    |
    v
Grounded generation
```

Metrics must be separated accordingly.

### Layer A — policy discovery

- candidate Recall@K
- candidate MRR
- candidate filtering/routing errors
- latency

### Layer B — evidence retrieval

- Section Hit@K
- evidence-role coverage
- latency
- missing-section failure

### Layer C — generation

- claim grounding rate
- unsupported claim rate
- abstention/review behavior

A single blended "RAG accuracy" is no longer acceptable.

## 7. Existing production-path evidence

Focused historical branch tests:

- recommendation candidate/service/policy summary: 21 passed
- retriever/evaluator contract: 30 passed
- generated-grounding evaluator contract: 9 passed

The generated-grounding branch also contains a historical live review artifact:

- policies reviewed: 10
- user-visible claims: 65
- grounded claims: 65
- unsupported claims: 0
- critical errors: 0

These numbers are historical evidence and are **not** a fresh 2026-09-30 run.

A fresh live generation/retrieval run is currently blocked because the deployed
backend reports:

```json
{
  "status": "not_ready",
  "checks": {
    "database": {
      "status": "error",
      "error": "InternalServerError"
    }
  }
}
```

The process liveness endpoint still returns HTTP 200.

This is also a full-stack delivery finding: process liveness and operational
readiness are correctly separate, and the current deployment is live but not
ready.

## 8. Next bounded experiments

### A. Candidate-selection evaluator

Do not reuse broad chunk Hit@5 as the final candidate metric.

Evaluate the actual `RecommendationCandidateService` candidate stage using:

- expected policy IDs
- candidate Recall@K
- candidate rank
- failure taxonomy

Use current rule/vector candidate path.

### B. R019 section-aware challenger

Keep policy 242 fixed.

Compare the smallest justified alternatives:

1. current vector baseline
2. evidence-role / section-aware filtering or boost

Do not add a reranker unless the expected section is present in a larger
candidate pool but ranked below K.

### C. Fresh live rerun only after readiness is restored

Once DB readiness is available:

- rerun selected candidate cases;
- rerun selected policy-scoped retrieval cases;
- rerun a bounded generated-grounding sample.

Do not copy historical latency/quality values forward as fresh results.
