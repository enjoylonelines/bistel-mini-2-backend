# Policy-quality gold-set candidate v1

Date: 2026-10-13
Dataset: `experiments/chatbot/datasets/dodam_policy_quality_v1.jsonl`

## What v1 is

This is a 21-case, source-backed quality gold set. Historical cases whose
internal policy ID could not be mapped to a current official source were
removed rather than retained as unreviewed evaluation rows.

| Quality axis | Cases | Expected signal |
| --- | ---: | --- |
| application method | 7 | expected policy and `신청 방법` section |
| benefit content | 7 | expected policy and `지원 내용` section |
| eligibility | 7 | expected policy and `공식 지원대상 원문` section |

Each row retains policy IDs, expected sections, expected decision, label
origin, and review state. The validator reports whether the set is eligible
for accuracy reporting:

```bash
PYTHONPATH=. .venv/bin/python experiments/chatbot/policy_quality_goldset.py
```

Current result: 21 cases, all approved, and
`ready_for_accuracy_reporting: true`.

## First isolated corpus run

On 2026-10-15, the seven source-mapped policies were rebuilt in an isolated
local PostgreSQL/PGVector corpus. Each policy contributed exactly its official
target, benefit, and application-method section (21 chunks total), embedded
with the current application embedding configuration.

The section evaluator filters each query to its reviewed expected policy. It
measures section retrieval only; candidate-policy discovery is reported below
as a separate unfiltered vector-retrieval measurement. Neither measures
full-chat answer quality or a production latency SLO.

| Metric | Result |
| --- | ---: |
| section hit@1 | 71.43% (15/21) |
| section hit@3 | 100.00% (21/21) |
| MRR | 0.8492 |
| query p50 / p95 | 263.818ms / 965.007ms |

The six not-top-1 cases were Q005, Q008, Q014, Q019, Q023, and Q027. They are
the concrete improvement set for section metadata or section-aware ordering;
the 100% hit@3 result alone does not justify claiming top-answer quality.

## Query-only section-aware ordering comparison

The six failures all had explicit query wording for one of three evidence
sections: target eligibility, benefit content, or application method. The
retrieval path now derives a conservative section hint from that wording and
only promotes an already-retrieved matching section. It does not read the
gold-set `quality_axis`, expected policy, or expected section to order results;
ambiguous and document-oriented queries keep the vector order.

| Metric | Vector baseline | Query-only ordering | Delta |
| --- | ---: | ---: | ---: |
| section hit@1 | 71.43% (15/21) | 100.00% (21/21) | +28.57pp |
| section hit@3 | 100.00% (21/21) | 100.00% (21/21) | 0.00pp |
| MRR | 0.8492 | 1.0000 | +0.1508 |

This comparison uses one identical top-3 vector result per question, then
applies an in-process ordering transform; it does not add a second provider
call. The run observed query p50/p95 of 289.530ms / 692.924ms, but latency is
an isolated local observation rather than a production SLO.

The result is deliberately narrow: source-backed 21 questions across seven
policies, with the expected policy already filtered. Because the set contains
clear section-intent wording and is not a held-out production traffic sample,
the 100% result is evidence of this ordering mechanism on this fixture, not a
policy recommendation accuracy claim or a generalization claim. Candidate
policy discovery is evaluated separately without `expected_policy_ids`.

## Unfiltered candidate-policy discovery baseline

For the same 21 questions, a separate search removed the expected-policy
filter and retrieved all 21 chunks from the isolated seven-policy corpus. The
first occurrence of each distinct policy is its candidate rank; this is a
chunk-vector candidate baseline, not the application's full eligibility or
recommendation pipeline.

| Metric | Raw vector query | Curated alias expansion | Delta |
| --- | ---: | ---: | ---: |
| policy hit@1 | 85.71% (18/21) | 100.00% (21/21) | +14.29pp |
| policy hit@3 | 100.00% (21/21) | 100.00% (21/21) | 0.00pp |
| policy MRR | 0.9286 | 1.0000 | +0.0714 |

The raw-vector top-1 misses were Q006, Q017, and Q027. Inspection showed a
terminology gap rather than a missing chunk: `임신 준비 시술` does not name
`난임치료 휴가`, while `신생아 목돈` does not name `출생아 첫만남 이용권`.
The retrieval query now appends only those two reviewed, narrow aliases; the
original user query remains the audit value. A query about newborn vaccination,
for example, does not trigger the first-meeting voucher alias.

This latest local run observed candidate query p50/p95 of 335.195ms / 441.562ms.
There are no negative or abstention cases in this fixture, and the corpus has
only the seven source-mapped target policies. Therefore this establishes a
reproducible candidate-retrieval improvement on the fixture, not a claim that
the service can safely recommend among the full policy universe or abstain on
out-of-scope questions.

Re-run the measurement only after rebuilding the isolated corpus from the
current official source:

```bash
PYTHONPATH=. .venv/bin/python experiments/chatbot/bootstrap_policy_quality_corpus.py
PYTHONPATH=. .venv/bin/python experiments/chatbot/evaluate_policy_quality_goldset.py
```

### First source-backed approvals

The local deep-dive database contained source detail only for policy 242
(`무료법률상담`) among the nine referenced policy IDs. Its target, benefit, and
application-method fields support approval of Q005, Q014, and Q024.

The configured data.go.kr API then provided official detail records for three
unambiguous historical query matches: policy 255 / `첫만남이용권`, policy 289
/ `에너지바우처`, and policy 292 / `스포츠강좌이용권`. Their official detail
fields support Q008–Q010, Q017–Q019, and Q027–Q029. Each approved row records
the API service ID, field, and fetch date.

The same API search and detail endpoints uniquely matched policy 240 to
`건강보험 임신출산 진료비(국민행복카드)` and policy 246 to
`난임치료휴가급여 지원`. Their list/detail fields support Q004, Q006, Q013,
Q015, Q023, and Q025.

On 2026-10-14, a broader API search uniquely matched policy 251 to
`인플루엔자 국가예방접종 지원사업` (`WLF00003213`). Its service definition,
benefit, and delivery-provider fields support Q007, Q016, and Q026. Together,
these source-backed cases make up the current 21-row set.

### Removed historical-only rows

Nine rows for historical internal policy IDs 237 and 238 were removed because
their current official source could not be established. The v1 metric scope is
therefore the remaining 21 source-backed rows only; it makes no claim about
the removed policies or the full historical corpus.

## Review gate

A policy reviewer must check the current source policy for every case and
either approve it with an evidence reference or reject/replace it. Only after
all evaluation rows used in a metric are approved may the project report:

- policy-selection Recall@K / MRR;
- section hit rate;
- evidence-link rate; or
- false recommendation and follow-up-question rates.

The current v1 has no reviewed boundary, rejection, or follow-up labels. Those
are deliberately excluded rather than invented from missing policy conditions.
They are the next annotation batch after this 21-case recommendation-oriented
set is measured.
