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
