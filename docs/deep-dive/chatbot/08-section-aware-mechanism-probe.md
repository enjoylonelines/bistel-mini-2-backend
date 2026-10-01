# Dodam Deep Dive — Section-aware Retrieval Mechanism Probe

> Date: 2026-09-30
> Environment: disposable local pgvector + synthetic policy 242 fixture + local Ollama
> Production behavior changed: no

## 1. Goal

The previous experiment showed that the current coarse `APPLICATION` evidence role
does not distinguish:

- application method (`신청 방법`)
- application period (`신청 기간`)

This cycle tested the smallest runtime mechanism for using that distinction
without introducing a reranker.

The mechanism:

```text
policy already selected
    ↓
vector top-k retrieval
    ↓
classify application subtype
    ↓
target section already present?
    ├ yes → reuse current result
    └ no  → one section-filtered vector query
```

The section-filtered query uses existing pgvector metadata:

- `policy_id`
- `source_type`
- `section`

No production service code was changed.

## 2. Local method-query probe

Query:

> 법률 문제를 무료로 물어보려면 어떤 기관을 찾아가야 하나요?

Target subtype:

- method

Target section:

- `신청 방법`

### K=5 baseline

Synthetic local vector result:

1. 조건 구조
2. 정리된 지원 조건
3. 공식 지원대상 원문
4. 지원 내용
5. 신청 방법

The target section is already present, so the fallback does not fire.

### Forced missing-section mechanism test

The same local fixture was queried with K=4 only to exercise the fallback path.

Baseline K=4 omits `신청 방법`.

The section-filtered second query returns:

- chunk `24290000004`
- section `신청 방법`

This is only a mechanism test. K=4 is not a production recommendation.

## 3. Local period-query probe

Query:

> 무료법률상담은 언제 신청할 수 있나요?

Target subtype:

- period

Target section:

- `신청 기간`

### K=5 baseline

Result:

1. 조건 구조
2. 공식 지원대상 원문
3. 정리된 지원 조건
4. 신청 방법
5. 유의 사항

The target `신청 기간` section is absent.

The section-filtered fallback returns:

- chunk `24290000005`
- section `신청 기간`

This gives a naturally occurring local example where the fallback path is useful
without artificially reducing K.

### K=7 single-query candidate pool

With K=7, the same target appears as rank 7:

1. 조건 구조
2. 공식 지원대상 원문
3. 정리된 지원 조건
4. 신청 방법
5. 유의 사항
6. 지원 내용
7. 신청 기간

Therefore two possible mechanisms are now distinguishable:

### Option A — larger candidate pool + section-aware post-selection

```text
single vector query K=7
→ choose target section if present
```

### Option B — K=5 + section-filtered fallback query

```text
vector query K=5
→ target section missing
→ second filtered vector query
```

## 4. Local timing comparison

10 repeated synthetic/local runs after warm-up:

| Path | Median | p95 |
| --- | ---: | ---: |
| K=5 baseline | 86.622 ms | 155.952 ms |
| K=7 single-query candidate pool | 96.207 ms | 135.133 ms |
| K=4 baseline before forced fallback | 90.085 ms | 171.645 ms |
| section-filtered second query | 92.368 ms | 129.575 ms |
| two-query total | 182.997 ms | 273.485 ms |

Interpretation boundary:

- these are local synthetic timings;
- embedding model is local `nomic-embed-text`, not production OpenAI embedding;
- dataset has one policy and seven chunks;
- these numbers are not portfolio performance evidence.

What this mechanism probe does show is that the current implementation performs
another embedding/search call for the fallback, so the two-query path roughly
doubles local request cost.

## 5. Current design decision

Do not implement always-on second-query fallback yet.

If the production/historical target section is present in a modestly larger
candidate pool, prefer:

```text
one slightly larger vector retrieval
    ↓
section-aware post-selection
```

over:

```text
K=5 retrieval
    ↓
second embedding + filtered vector retrieval
```

because the former preserves one embedding/search round trip.

Use the second filtered query only when the desired section is genuinely absent
from the larger candidate pool.

## 6. Why no reranker yet

The experiments still do not justify a general reranker.

A reranker becomes relevant only if:

1. the target section is present in the candidate pool;
2. section metadata alone cannot select it reliably;
3. there are multiple plausible chunks within the same target section or role;
4. ranking quality remains the actual bottleneck after section narrowing.

None of those conditions has been demonstrated yet for this bounded failure.

## 7. Next production-grade gate

When original DB readiness is restored or a reproducible original corpus snapshot
is available:

Run application cases with:

- K=5
- K=7
- K=10

Record:

- target section rank
- Section Hit@K
- subtype accuracy on a held-out set
- added latency
- fallback rate

Decision rule:

```text
target section usually enters K=7/10
→ larger pool + section-aware selection

target section often absent even at K=10
→ section-filtered second query / metadata filter

target present but ambiguous within target section
→ consider reranker
```

Until that gate is satisfied, production retrieval behavior remains unchanged.

## 9. Revalidation — 2026-10-01

The mechanism was revalidated from a **freshly recreated** disposable pgvector
container rather than reusing the previous local DB state.

### Environment recreation

- removed and recreated `dodam-deepdive-pg`
- bootstrapped synthetic policy 242 fixture again
- started the current deep-dive backend
- ingested all seven chunks again through the actual RAG ingest endpoint

Result:

- requested chunks: 7
- embedded chunks: 7
- ingest success: true

### Method query

> 법률 문제를 무료로 물어보려면 어떤 기관을 찾아가야 하나요?

Fresh local ranking reproduced the previous result.

K=5:

1. 조건 구조
2. 정리된 지원 조건
3. 공식 지원대상 원문
4. 지원 내용
5. 신청 방법

K=7/K=10:

- 신청 방법 remains rank 5
- 신청 기간 appears at rank 7

### Period query

> 무료법률상담은 언제 신청할 수 있나요?

Fresh local ranking also reproduced the previous result.

K=5:

1. 조건 구조
2. 공식 지원대상 원문
3. 정리된 지원 조건
4. 신청 방법
5. 유의 사항

The desired `신청 기간` section is absent.

K=7/K=10:

- `신청 기간` enters at rank 7

Therefore the earlier mechanism finding is reproducible from a clean local DB:

> a modestly larger single-query candidate pool can expose a relevant application
> subtype section that K=5 misses.

### Section-aware fallback re-run

The local section-filter probe was rerun after recreation.

Observed:

- method K=5: target already present, fallback not used
- forced method K=4 miss: section-filtered fallback recovers `신청 방법`
- period K=5: fallback recovers `신청 기간`
- period K=7: target present, fallback not used

### Timing re-run

10 warm local synthetic runs:

| Path | Median | p95 |
| --- | ---: | ---: |
| K=5 baseline | 52.366 ms | 85.504 ms |
| K=7 single-query candidate pool | 49.689 ms | 64.744 ms |
| K=4 baseline before forced fallback | 50.868 ms | 62.052 ms |
| section-filtered second query | 51.495 ms | 59.037 ms |
| two-query total | 103.444 ms | 121.089 ms |

The absolute timings differ from the previous run because this is a local
non-production environment, but the structural relationship reproduced:

- K=7 single-query cost is in the same range as K=5;
- the two-query fallback path costs roughly two retrieval round trips.

### Historical repeated evidence cross-check

The historical repeated scoped benchmark was checked again.

For R019, across all five stored runs:

- vector: section miss every run
- adaptive: section miss every run
- adaptive fallback: never fired
- returned section order was stable:
  `신청 기간 → 공식 지원대상 원문 → 유의 사항 → 정리된 지원 조건 → 조건 구조`

Historical scoped vector Section Hit@5 was also stable at:

- 98%
- 98%
- 98%
- 98%
- 98%

This reinforces the earlier diagnosis that R019 is not random benchmark noise.
It is a stable section-selection failure under the historical retrieval setup.

### Deployment status

The deployed backend is still not ready:

- process liveness: previously healthy
- current readiness: `not_ready`
- database check: `InternalServerError`

Therefore the clean local rerun strengthens **mechanism reproducibility**, but it
still does not replace a production/original-corpus rerun.

### Revalidated decision

The current decision remains unchanged, now with stronger evidence:

1. do not add a general reranker;
2. test a slightly larger candidate pool first;
3. use application subtype metadata for post-selection;
4. reserve a second section-filtered query for cases where the target section is
   still absent from that larger pool;
5. keep all local synthetic latency numbers out of portfolio performance claims.
