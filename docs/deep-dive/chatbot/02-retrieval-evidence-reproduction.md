# Dodam Deep Dive — Retrieval Evidence Reproduction

> Historical evidence source: `origin/feat/chat-retriever-layer@d92a8d3`  
> Current implementation baseline remains: `origin/develop@08ebc16`

## 1. What was reproduced

The historical retrieval branch was checked out in an isolated worktree.

Focused tests:

```bash
uv run --python /opt/homebrew/bin/python3.12 --with-requirements requirements.txt \
  python -m pytest -q \
  tests/test_policy_retrievers.py \
  tests/eval/test_retrieval_eval.py \
  tests/test_policy_rag_metadata.py
```

Result:

- 30 passed
- 0 failed

This confirms that the branch-level retriever implementation and evaluation code are internally reproducible at unit/evaluation-test level in the current local environment.

A live 50-case rerun was **not** executed because the isolated checkout has no configured database or OpenAI embedding credentials. Historical benchmark numbers are therefore treated as historical evidence, not fresh measurements.

## 2. Historical benchmark baseline

Tracked artifact:

`output/retrieval_benchmark_50.json`

50 questions, Top K=5.

| Strategy | Policy Hit@5 | Section Hit@5 | MRR | p50 | p95 | Fallback |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| SQL keyword | 18.0% | 12.0% | 0.0957 | 1256.644 ms | 1731.826 ms | - |
| vector | 86.0% | 78.0% | 0.7497 | 781.577 ms | 1093.191 ms | - |
| hybrid | 88.0% | 78.0% | 0.7757 | 1291.562 ms | 1717.025 ms | - |
| adaptive | 86.0% | 78.0% | 0.7497 | 808.613 ms | 2290.099 ms | 14.0% |

Interpretation boundary:

- These are historical branch artifacts.
- Latency is especially environment-dependent.
- Do not present these values as a 2026-09-30 fresh benchmark.
- The branch document itself states that answer faithfulness is not measured by this retrieval benchmark.

## 3. Failure inspection

### Vector policy misses

Historical vector misses:

- R002
- R008
- R023
- R025
- R029
- R039
- R044

Hybrid misses:

- R002
- R008
- R023
- R025
- R029
- R039

Therefore the weighted hybrid recovered exactly one policy-level miss in this set: **R044**.

### R044 — lexical signal helps

Question:

> 출산한 지 반년이 안 된 수급 가정도 전기와 난방 비용을 지원받나요?

Expected policy: 289.

Vector Top 5 did not include policy 289.

Hybrid placed policy 289 / `공식 지원대상 원문` at rank 1.

This is a concrete example where lexical contribution corrected a semantic-search miss.

### R002 — both vector and hybrid fail

Question:

> 발달장애가 있는데 직장에서 예절이나 사람들과 지내는 법을 배우고 싶어요

Expected policy: 237 / `지원 내용`.

Vector and hybrid both retrieved other policies in Top 5.

This case cannot be solved merely by the observed hybrid fusion used in the historical branch. It is a better candidate for deeper failure analysis such as:

- corpus/chunk coverage
- embedding representation
- query wording
- policy metadata
- candidate recall

rather than more fusion-weight tuning.

## 4. Failure attribution from scoped retrieval

The historical branch also contains a policy-scoped 50-case artifact.

For every broad vector policy miss:

- R002
- R008
- R023
- R025
- R029
- R039
- R044

the same vector retriever succeeds when constrained to the expected policy.

Scoped result for all seven cases:

- policy hit: true
- expected section hit: true

Examples:

- R002: expected policy 237, expected section `지원 내용` → scoped vector rank 1 / section rank 1
- R044: expected policy 289 → scoped vector rank 1 / expected section rank 1

This materially changes the failure hypothesis.

The observed broad misses are **not primarily evidence-absence or chunking failures** in the stored benchmark. The expected support is present and semantically retrievable once the policy candidate is known.

The next problem is therefore closer to:

> broad multi-policy candidate selection / global ranking

than:

> missing chunk / wrong chunk boundary.

This also explains why a reranker should not be introduced blindly: first determine whether the correct policy enters the candidate pool at a larger K and is merely ranked too low, or whether first-stage retrieval excludes it entirely.

## 5. Current decision

Do not port the historical hybrid/adaptive code wholesale yet.

The historical evidence shows:

- vector is much stronger than the simple SQL baseline;
- weighted hybrid adds only a small policy-hit improvement in the stored run;
- adaptive preserves vector accuracy but acts mainly as a bounded fallback;
- some misses remain shared across all semantic/hybrid variants.

Therefore the next Axis 2 task is **failure attribution**, not another retriever implementation.

## 6. Next experiment

For each historical miss, classify:

1. expected support exists in indexed corpus;
2. expected support is represented by a valid chunk;
3. expected chunk is retrievable but ranked below K;
4. expected chunk is not a candidate at all;
5. query requires normalization/rewrite;
6. gold label or corpus version mismatch.

Only after this classification should query rewrite, reranking, metadata filtering, or new fusion be considered.
