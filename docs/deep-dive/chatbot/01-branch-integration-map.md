# Dodam Deep Dive — Branch Evidence Integration Map

> Compared against `origin/develop@08ebc16`

## 1. Principle

Do not merge either historical branch wholesale.

Each unique change is classified as:

- **adopt** — needed for current deep-dive question and still valid against develop
- **reproduce** — keep concept/evidence, rerun on current baseline rather than copy code
- **evidence-only** — useful historical proof, but no current code integration needed
- **obsolete** — superseded by current develop or outside current scope

## 2. Retrieval branch

Source: `origin/feat/chat-retriever-layer@d92a8d3`

| Change area | Initial classification | Reason |
| --- | --- | --- |
| pluggable policy retriever layer | reproduce/adopt after baseline | current deep dive needs failure attribution, but abstraction should only be brought in if current service cannot support the experiment cleanly |
| retrieval 50-case gold set | reproduce | useful evaluation asset; leakage/audit fixes make it more trustworthy than early variants |
| dense / hybrid benchmark | reproduce | results must be rerun on current develop data/environment before reuse |
| hybrid fusion tuning | evidence-only until miss reproduced | tuning is not justified unless current baseline shows ranking failure |
| adaptive retrieval fallback | candidate adopt | directly relevant if current baseline reproduces the failure cases that motivated it |
| retrieval portfolio docs | evidence-only | documentation is not implementation source of truth |
| unrelated assessment/idempotency changes | obsolete/out-of-scope for retrieval cycle | avoid coupling unrelated lifecycle changes |

## 3. Lifecycle branch

Source: `origin/refactor/chat-handler-result-lifecycle@ab56916`

| Change area | Initial classification | Reason |
| --- | --- | --- |
| handler output explicit contract | candidate adopt | may reduce cross-handler implicit state if current defect tests expose ambiguity |
| request recovery hardening | reproduce first | current develop already has durable chat request recovery; only missing behavior should be selectively ported |
| generated grounding evaluation | reproduce | directly useful for Axis 2 generation-failure attribution |
| health/deployment/CI | evidence-only/out-of-scope | not required for current first bounded cycle |
| Supabase/Render migration | out-of-scope | deployment target is not the current research question |
| retrieval changes inherited from retriever branch | handle under retrieval map | avoid duplicate integration decisions |

## 4. Integration gate

A historical commit is only integrated if all are true:

1. current develop reproduces the problem it addresses;
2. the change is still compatible with current contracts;
3. a focused regression test is added first or alongside it;
4. the change does not bring unrelated historical branch behavior.

## 5. Immediate decisions

- Do **not** merge `refactor/chat-handler-result-lifecycle` wholesale.
- Do **not** merge `feat/chat-retriever-layer` wholesale.
- Keep current develop as the implementation baseline.
- Use historical branches as experiment/evidence sources.
- Next code change should be driven by a failing discriminating test, not by branch availability.
