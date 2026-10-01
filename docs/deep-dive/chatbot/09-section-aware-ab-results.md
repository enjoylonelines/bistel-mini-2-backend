# Dodam Deep Dive — Section-aware Challenger A/B

> Date: 2026-10-01
> Branch: `deep-dive/dodam-chatbot-3axis`
> Status: implementation + local/synthetic validation
> Production rollout: not enabled globally

## 1. Code change

The current RAG metadata and retrieval helper were extended without changing
existing caller behavior by default.

### Metadata

`PolicyRagService` now distinguishes:

- `신청 방법` → `APPLICATION_METHOD`
- `신청 기간` → `APPLICATION_PERIOD`

The existing broad evidence role remains:

- both → `application`

This preserves compatibility with current code.

Legacy PGVector rows do not need an immediate full re-embedding. If an old row
does not contain `section_subtype`, the search-result adapter derives the subtype
from its existing `section` metadata.

### Retrieval helper

`search_policy_chunks(...)` now supports optional:

- `section_subtype`
- `candidate_k`

When a subtype is supplied:

1. retrieve a slightly larger candidate pool (minimum K=7);
2. keep the normal top-K result if the requested section is already present;
3. if the target section is below the output cutoff but inside the candidate
   pool, replace only the last output item with that target section;
4. preserve the rest of the original vector order.

Default calls without a subtype still use the original K and result behavior.

## 2. Application subtype rule

A bounded rule classifier distinguishes application-method vs application-period
questions.

Examples:

- "어디에서 신청?" → METHOD
- "신청 기간은 언제?" → PERIOD
- "휴가 기간 급여는 어디에 신청?" → METHOD
- "신청할 때 어떤 서류가 필요?" → no application subtype; document question

The local development challenge set initially exposed two errors:

1. "신청 가능한 시기" was incorrectly classified as METHOD.
2. "신청할 때 어떤 서류" was incorrectly classified as METHOD.

After adding explicit period expressions and a document guard:

- initial development challenge accuracy: 22/24 = 91.67%
- refined rule on the same development set: 24/24 = 100%

This is development-set tuning, not held-out accuracy.

## 3. Local synthetic A/B

Environment:

- current backend code
- disposable pgvector PostgreSQL
- synthetic policy 242 with 7 sections
- local `nomic-embed-text` through Ollama
- output K remains 5

Challenge set after adding one previously observed period failure:

- total: 25
- application cases: 17
- non-application negatives: 8

### Baseline

Vector K=5.

Application Section Hit@5:

- 16/17
- 94.12%

### Challenger

Vector candidate K=7, output K=5, subtype-aware guaranteed inclusion.

Application Section Hit@5:

- 17/17
- 100%

Measured delta:

- **+5.88 percentage points**

Recovered case:

> 무료법률상담은 언제 신청할 수 있나요?

Baseline K=5:

1. 조건 구조
2. 공식 지원대상 원문
3. 정리된 지원 조건
4. 신청 방법
5. 유의 사항

Challenger final K=5:

1. 조건 구조
2. 공식 지원대상 원문
3. 정리된 지원 조건
4. 신청 방법
5. 신청 기간

### Negative regression

For 8 non-application questions:

- baseline top-5 vs challenger top-5 unchanged: 8/8 = 100%

This is important because the challenger should not perturb unrelated target,
benefit, caution, or document questions.

### Local latency

One local run over the 25-case set:

| Variant | Median | p95 |
| --- | ---: | ---: |
| baseline K=5 | 47.394 ms | 57.607 ms |
| challenger K=7 → output K=5 | 46.422 ms | 52.536 ms |

Interpretation:

- no measurable local penalty appeared in this tiny synthetic environment;
- these absolute timings are not production evidence;
- the main architectural benefit is still one embedding/vector round trip rather
  than an unconditional second filtered query.

## 4. Historical application subset

The historical policy-scoped vector benchmark contains 10 application cases.

Baseline Section Hit@5:

- 9/10 = 90%

The current subtype rule maps the expected application subtype correctly for all
10 historical cases.

The single baseline miss, R019, asks for an institution and expects
`신청 방법`. That target section exists in the same policy's section inventory.

Therefore the **oracle section-aware upper bound** is:

- baseline: 90%
- oracle upper bound: 100%
- potential lift: +10 percentage points

This is **not** a measured production K=7 result.

The historical artifact records only Top-5, so it does not prove that R019's
`신청 방법` chunk would enter a production K=7 candidate pool.

## 5. What can be claimed now

Safe engineering claim:

> 신청 방법과 신청 기간이 동일 APPLICATION 역할로 뭉쳐 발생하는 실패를 분해하고,
> 더 넓은 단일 vector candidate pool에서 질문 subtype에 맞는 section을 보존하는
> challenger를 구현했다. 로컬 synthetic 17개 application 질의에서 Section Hit@5가
> 94.12%에서 100%로 개선됐고, 8개 non-application 질의의 Top-5 결과는 모두 유지됐다.

Do **not** use those local numbers as portfolio production-performance claims.

Historical evidence may be described separately:

> 기존 scoped application 평가 10건의 Section Hit@5는 90%였고, 실패 1건은
> target section이 정책 내부에 존재하는 stable section-selection failure였다.

## 6. Gate before production activation

Before switching current chat/RAG callers to the challenger:

1. restore original DB/corpus access;
2. run K=5 / K=7 / K=10 against the same production embedding path;
3. compare full 50-case and application subset;
4. confirm non-application regression = 0;
5. collect p50/p95 and vector-call count;
6. freeze a new post-change challenge set and run it without further tuning.

Target decision table:

| Metric | Baseline | Challenger | Gate |
| --- | ---: | ---: | --- |
| Full Section Hit@5 | historical 98% | fresh measure | no regression |
| Application Hit@5 | historical 90% | fresh measure | improve |
| Non-application Hit@5 | fresh measure | fresh measure | no regression |
| subtype challenge accuracy | fresh held-out | fresh held-out | acceptable |
| vector calls/request | 1 | 1 | keep 1 |
| p50 / p95 | fresh measure | fresh measure | bounded increase |

A second filtered vector query remains fallback-only and is not part of the
default challenger.

## 7. Fresh original-corpus evaluator prepared

A dedicated evaluator was added at:

- `experiments/chatbot/live_section_aware_eval.py`

It performs one K=10 query per historical application case against the expected
policy and derives, from that exact same retrieval result:

- baseline Section Hit@5 from ranks 1..5;
- challenger Section Hit@5 from candidate ranks 1..7 with section-aware output K=5;
- target Recall@7;
- target Recall@10;
- first target-section rank;
- recovered and still-missed cases;
- K=10 retrieval p50/p95.

This avoids comparing separately embedded baseline/challenger calls and makes the
R019 decision explicit:

- target rank 6..7 -> K=7 challenger can recover it;
- target rank 8..10 -> K=7 is insufficient and K=10/fallback must be compared;
- target absent by K=10 -> larger-K is not the primary fix.

The evaluator logic has unit coverage, and the full backend regression suite
currently passes with **431 passed, 1 skipped, 0 failed**.

## 8. Current original-environment blocker

The repository execution connection was restored and the deployed service was
rechecked.

Current deployed state:

- `/health/live`: `ok`
- `/health/ready`: `not_ready`
- database readiness error: `InternalServerError`

The local deep-dive checkout does not contain `DATABASE_URL`,
`PSYCOPG_DATABASE_URL`, or `OPENAI_API_KEY` credentials for the original corpus.
The GitHub repository currently exposes no DB/OpenAI Actions secrets; only the
Discord webhook secret is present. Historical deployment metadata identifies the
Render database deployment, but does not expose credentials.

Therefore the fresh evaluator is ready but has **not** been run against the
original database. No synthetic or historical proxy result is promoted to a
fresh production metric.
