# Dodam AI Chatbot 3-Axis Deep Dive Plan

> Date: 2026-09-30  
> Status: Cycles 0–5 have bounded implementation/evidence artifacts; RAG grounding and execution-control closure recorded on 2026-10-09
> Primary repository: `bistel-mini-2/bistel-mini-2-backend`  
> Companion repository: `bistel-mini-2/bistel-mini-2-frontend`  
> Backend baseline: `origin/develop@08ebc167384cca490303059dcb927887c300bd12`  
> Frontend baseline: `origin/develop@50690f27ff6689c2f810509fab96afb5bc913ad1`

## 0. Goal

이번 작업의 목적은 도담을 기능 수가 많은 AI 데모로 확장하는 것이 아니다.

현재 구현의 구조적 결함과 이미 검증된 기능을 먼저 구분하고, 다음 세 축을 먼저 깊게 검증한다.

1. **Conversation / Request Lifecycle**
2. **RAG / Retrieval Quality & Failure Attribution**
3. **Full-stack Chat Service Delivery**

이 세 축은 최종 목적이 아니라 다음 실행 계층의 전제다. durable request,
retrieval trace, fallback 결과를 구분하지 못하면 AI 호출의 동시성·비용·지연을
제어해도 어떤 사용자 결과를 보존했는지 증명할 수 없다.

최종적으로 다음 질문에 실측과 재현 가능한 evidence로 답할 수 있어야 한다.

> 하나의 사용자 메시지가 네트워크 단절·중복 요청·LLM 오류가 있어도 하나의 durable 결과로 수렴하는가?

> 답변 실패가 검색 실패인지 생성 실패인지 분리해 측정할 수 있는가?

> 백엔드의 durable run과 프론트의 ephemeral SSE connection을 분리하고도 사용자 경험이 일관되게 유지되는가?

다음 cycle에서는 질문을 한 단계 아래로 내린다.

> 제한된 외부 LLM/RAG 실행 예산과 가변 지연 아래에서, 어떤 요청을 실행·대기·취소·fallback할지 제어하면서 사용자 결과의 정합성을 보존할 수 있는가?

여기서 안정성은 단독 목적이 아니다. AI 서비스의 제한 자원(동시 in-flight
호출, provider quota, token 비용, tail latency)을 다루기 위한 기본 계약이다.

## 0.1 Current-progress reconciliation (2026-10-07)

이 문서 상단의 최초 상태 표기는 더 이상 현재 branch 상태를 설명하지 못한다.
`deep-dive/dodam-chatbot-3axis@e472bf9`에는 다음 evidence/artifact가 이미 있다.

- lifecycle invariants와 focused regression: `docs/deep-dive/chatbot/03-lifecycle-fullstack-findings.md`
- candidate-selection과 policy-scoped section retrieval의 분리:
  `docs/deep-dive/chatbot/04-retrieval-failure-decomposition.md`
- section-aware challenger와 synthetic/local A/B:
  `docs/deep-dive/chatbot/06-candidate-eval-and-section-challenger.md`,
  `08-section-aware-mechanism-probe.md`, `09-section-aware-ab-results.md`
- original corpus 품질과 분리된 provenance/evidence review의 후속 gate:
  `docs/deep-dive/chatbot/10-evidence-review-gate.md`

이 문서에 기록된 pass count, synthetic latency, historical retrieval 값은 해당
artifact가 가리키는 revision/environment의 증거다. 2026-10-07 현재의 운영
성능이나 production readiness로 다시 표현하지 않는다.

이번 보강은 앞의 세 축을 다시 구현하자는 계획이 아니다. 아직 측정하지 않은
**AI execution control**을 별도 가설·측정·결정 gate로 정의하는 것이다.

---

# 1. Canonical baseline and source-of-truth audit

## 1.1 Fresh clone baseline

현재 로컬 canonical clone:

- `~/Projects/bistel-mini-2-backend`
- `~/Projects/bistel-mini-2-frontend`

두 저장소는 2026-09-30 기준 fresh clone이며 각각 `origin/develop`과 일치한다.

### Backend

- default branch: `develop`
- baseline: `08ebc16`
- working tree at clone: clean

### Frontend

- default branch: `develop`
- baseline: `50690f2`
- working tree at clone: clean

과거 `~/Documents/kosa-course/projects/mini-2`는 Git metadata가 없는 legacy copy이므로 source of truth로 사용하지 않는다.

## 1.2 Important unmerged backend branches

현재 `develop`보다 앞선 관련 branch가 존재한다.

### `origin/feat/chat-retriever-layer`

`develop` 대비 12 commits ahead.

주요 unique work:

- pluggable policy retriever layer
- dense / hybrid retrieval benchmark
- goldset leakage fix
- retrieval label audit
- hybrid fusion tuning
- adaptive retrieval fallback
- retrieval portfolio evidence

### `origin/refactor/chat-handler-result-lifecycle`

`develop` 대비 33 commits ahead.

위 retrieval work를 포함하고 추가로:

- handler output explicit contract
- AI request lifecycle recovery hardening
- generated grounding evaluation
- deployment / CI baseline
- Supabase / Render deployment work

따라서 구현 시작 전에 이 branch를 단순히 cherry-pick하지 않는다.

먼저 각 commit을 다음 세 가지로 분류한다.

1. 현재 deep dive baseline에 반드시 필요한 코드
2. 이미 develop에서 다른 방식으로 대체된 코드
3. 실험 evidence만 참고하고 code integration은 필요 없는 작업

## 1.3 Frontend branch state

확인한 과거 chat 관련 branch 대부분은 현재 `develop`보다 뒤에 있다.

예:

- `feat/145-sse-progress-intent-ui`
- `feature/113-mypage-chat-history`
- `feature/120-chat-markdown-rendering`

즉 frontend에서는 과거 feature branch를 기준선으로 삼지 않고 현재 `develop`을 우선한다.

---

# 2. Phase 0 — Current structure defect audit

기능을 추가하기 전에 현재 구조를 아래 항목으로 감사한다.

## 2.1 Current strengths to preserve

현재 develop에서 이미 확인된 기반:

### Backend

- persistent chat sessions/messages
- `chat_request` model/repository
- idempotency key
- request status 조회
- incomplete request handling
- SSE events
  - accepted
  - token
  - progress
  - intent
  - done
  - error
  - cancelled
- session delete + cancellation path
- RAG repository/service
- policy evidence persistence
- chat graph state / slot filling
- chat evaluation scenarios

### Frontend

- REST chat API adapter
- POST SSE streaming client
- accepted/token/progress/intent/done/error/cancelled handling
- idempotency key transmission
- duplicate AI submission guard
- chat progress calculation
- history/session UI
- structured chat result components

이미 존재하는 기능은 이름을 바꾸거나 framework를 교체하기 위해 다시 만들지 않는다.

---

# 3. Confirmed structural defect candidates

아래는 현재 코드 inspection으로 확인된 **결함 후보**다. 아직 성능/장애 문제로 확정하지 않고 실험으로 판별한다.

## D1. Duplicate state ownership in ChatGraphState

현재 `ChatSlot`에:

- `profile`
- `pending`

이 존재하지만 `ChatGraphState` 최상위에도:

- `profile`
- `pending`
- `pending_intent`
- `awaiting_slots`

이 중복되어 있다.

### Risk

- 어느 값이 authoritative한지 불분명
- resume/follow-up 시 state divergence
- reducer/node가 서로 다른 state path를 갱신할 가능성
- test fixture가 특정 경로만 검증할 가능성

### Audit question

> 동일 conversation에서 slot resume를 여러 턴 반복했을 때 `slot.profile`과 top-level `profile`이 항상 동일한가?

### Decision gate

실제 divergence가 관측되거나 state update contract가 두 경로 모두를 요구한다면 single source of truth로 축소한다.

단지 타입이 중복되어 있다는 이유만으로 refactor하지 않는다.

---

## D2. Oversized orchestration boundaries

현재 파일 크기:

- `app/services/ai_request_lifecycle_service.py`: 약 83 KB
- `app/services/chat/chat_service.py`: 약 45 KB
- `app/ai/nodes/chat/chat_nodes.py`: 약 27 KB
- frontend `app/chat/page.js`: 약 90 KB

파일 크기 자체를 결함으로 판정하지 않는다.

문제는 한 모듈이 여러 변경 이유를 동시에 가지는지다.

### Audit dimensions

`ChatService`:

- session CRUD
- message persistence
- graph execution
- streaming
- cancellation
- lifecycle transition
- follow-up
- retry/recovery

`AiRequestLifecycleService`:

- request state transition
- eligibility
- recommendation
- RAG/evidence
- follow-up
- persistence
- recovery

`app/chat/page.js`:

- remote session data
- message view model
- SSE transport
- optimistic/temporary message
- progress state
- retry/error state
- intent-specific rendering
- business payload normalization

### Audit question

> 하나의 lifecycle 변경이 몇 개의 unrelated concern을 함께 수정하게 만드는가?

### Gate

실제 change-coupling/test burden이 확인된 경계만 분리한다.

---

## D3. Cancellation is partly process-local

현재 chat cancellation path에는 process-memory registry가 존재한다.

### Risk

- multi-worker에서 다른 process가 같은 cancellation signal을 보지 못함
- process restart 시 cancellation state 유실
- persistent request state와 in-memory cancellation state가 달라질 수 있음

### Audit question

> running request가 process 경계를 넘거나 restart된 후에도 cancel/delete가 durable invariant를 유지하는가?

### Scope

이번 cycle은 distributed job queue 구축이 목적이 아니다.

필요하면 DB request state를 authoritative cancellation contract로 두고 memory registry는 fast-path로 제한하는 정도까지만 검토한다.

---

## D4. Durable request vs SSE connection boundary is not explicit enough

Frontend streaming client는 transport가 terminal event를 받지 못하면 `STREAM_INCOMPLETE`를 반환한다.

그러나 backend request 자체는 이미 accepted되어 계속 처리되었을 수 있다.

### Risk

```text
request accepted
→ SSE disconnected
→ server completes
→ frontend shows error
→ user retries
→ duplicate semantic work / confusing UI
```

idempotency가 존재하더라도 UX가 durable request state를 다시 조회하지 않으면 “연결 실패 = 요청 실패”로 오해할 수 있다.

### Core design invariant

> **Run/request는 durable하고 SSE connection은 ephemeral하다.**

이 invariant를 Axis 1과 Axis 3의 중심으로 둔다.

---

## D5. Frontend API normalization layer is broad

`apis/chatApi.js`는 여러 legacy/camel/snake 형태를 폭넓게 normalize한다.

예:

- policy id/name variants
- slot request variants
- summary card variants
- session/message variants

### Risk

- backend contract drift가 frontend normalization에 숨겨짐
- migration 완료 후에도 compatibility code가 누적
- malformed response를 valid response처럼 복구할 가능성

### Audit question

> 현재 develop backend의 실제 response contract만 기준으로 했을 때 어떤 normalization branch가 아직 필요한가?

실제 runtime/API contract evidence 없이 compatibility code를 삭제하지 않는다.

---

## D6. Retrieval quality evidence exists outside current develop

현재 develop에는 RAG implementation/eval 기반이 존재하지만, retrieval deep dive의 중요한 개선 commit은 `feat/chat-retriever-layer` / `refactor/chat-handler-result-lifecycle`에 남아 있다.

### Risk

- portfolio claim과 canonical branch가 불일치
- benchmark 결과를 현재 develop code로 재현하지 못할 수 있음

### Required action

implementation 전에 retrieval branch의 code/evidence를 현재 develop과 diff하여:

- 재현 가능한 것
- stale한 것
- integration할 것

을 구분한다.

---

# 4. Baseline verification status

## Frontend

실행:

```text
node --test app/chat/chatProgress.test.mjs
```

결과:

- 8 tests
- 8 passed
- 0 failed

검증 범위:

- initial progress
- intent alias profile
- completed step handling
- out-of-order event monotonicity
- interpolation
- pre-done < 100%
- default profile
- invalid metadata fallback

## Backend

현재 isolated worktree에는 Python dependency environment가 설치되지 않아 direct `pytest` 명령을 실행할 수 없었다.

`pytest: command not found`

따라서 implementation 시작 전 환경을 repository requirements에 맞게 별도 구성하고 다음 baseline test set을 먼저 통과시킨다.

- `tests/test_chat_request_repository.py`
- `tests/test_chat_service.py`
- `tests/test_chat_e2e.py`
- `tests/eval/test_live_eval_metrics.py`

기존 local/global Python 환경을 임의로 오염시키지 않는다.

---

# 5. Three-axis deep dive overview

```text
                        User
                          |
                          v
                    Next.js Chat UI
                          |
                 REST + POST/SSE
                          |
                          v
               Durable Chat Request/Run
                          |
                          v
               Conversation State / Route
                          |
                +---------+---------+
                |                   |
                v                   v
           Retrieval              Tools
                |
                v
         Evidence sufficiency
                |
                v
               LLM
                |
                v
        Grounding / Validation
                |
                v
          Durable Persistence
                |
                v
          SSE / re-query state
                |
                v
                UI
```

---

# 6. Axis 1 — Conversation / Request Lifecycle

## Research question

> 네트워크/프로세스 오류가 있어도 한 사용자 메시지가 하나의 durable result로 수렴하는가?

## 6.1 State model

명시적으로 분리한다.

### Conversation

장기 사용자 문맥.

### Message

사용자가 보낸 입력 또는 assistant output.

### Request / Run

한 message를 처리하는 실행 단위.

추천 conceptual state:

```text
accepted
  ↓
processing
  ├──→ completed
  ├──→ failed
  └──→ cancelled
```

기존 DB enum/contract를 우선 사용하며 새로운 state를 불필요하게 추가하지 않는다.

## 6.2 Invariants

1. 동일 idempotency key는 semantic duplicate work를 만들지 않는다.
2. completed request는 reconnect 후 동일 durable result를 조회할 수 있다.
3. SSE disconnect가 request failure를 의미하지 않는다.
4. assistant message는 한 successful request당 최대 하나의 canonical persisted result를 가진다.
5. failed/cancelled state는 retry policy와 구분된다.
6. session deletion 후 늦게 도착한 generation 결과가 다시 메시지를 생성하지 않는다.

## 6.3 Failure matrix

최소 다음 fixture를 만든다.

| Case | Fault |
| --- | --- |
| L1 | duplicate submit before accepted |
| L2 | duplicate submit after accepted |
| L3 | disconnect before first token |
| L4 | disconnect mid-stream |
| L5 | browser refresh while processing |
| L6 | LLM timeout |
| L7 | generation succeeds, persistence fails |
| L8 | process interrupted while request is processing |
| L9 | session deleted during generation |
| L10 | retry after terminal failed state |
| L11 | reconnect after server already completed |
| L12 | out-of-order/duplicate SSE events |

## 6.4 Metrics

- duplicate durable request count
- duplicate assistant message count
- terminal convergence rate
- reconnect recovery success rate
- orphan processing request count
- cancellation consistency
- request-to-first-event latency
- request total latency

## 6.5 Stop condition

위 failure matrix에서 request/message invariants의 pass/fail을 모두 설명할 수 있고, 발견된 defect가 수정 후 재현 fixture로 방지되면 종료한다.

distributed queue/Kafka/Celery 도입으로 자동 확장하지 않는다.

---

# 7. Axis 2 — RAG / Retrieval Quality & Failure Attribution

## Research question

> 잘못된 답변의 원인이 retrieval인지 generation인지 독립적으로 측정할 수 있는가?

## 7.1 Existing assets to reuse

먼저 unmerged retrieval branch를 검토한다.

확인된 work:

- pluggable retriever
- three-retriever benchmark
- goldset leakage correction
- label audit
- hybrid RRF/fusion tuning
- adaptive fallback

이 작업이 현재 develop 기준으로 재현 가능한지 먼저 확인한다.

## 7.2 Evaluation funnel

```text
Question
   ↓
Retriever
   ↓
Expected support hit?
   ├ no  → RETRIEVAL_MISS
   |
   yes
   ↓
Correct section/evidence?
   ├ no  → RETRIEVAL_HIT_WRONG_SECTION
   |
   yes
   ↓
Generation
   ├ invalid/unsupported → RETRIEVAL_HIT_GENERATION_FAIL
   └ grounded           → RETRIEVAL_HIT_GROUNDED_PASS
```

unanswerable:

```text
no valid support
  ├ abstain → UNANSWERABLE_ABSTAIN_PASS
  └ answer  → UNANSWERABLE_UNSUPPORTED_ANSWER
```

pipeline fault:

`PIPELINE_ERROR`

## 7.3 Primary retrieval metrics

- Hit@K / Recall@K
- MRR
- retrieval latency
- fallback rate
- context/evidence count

## 7.4 End-to-end metrics

- grounded answer rate
- unsupported answer rate
- correct abstention rate
- generation failure after retrieval hit
- structured result validity where applicable

## 7.5 Experiment order

모든 기법을 brute-force하지 않는다.

1. current develop baseline
2. reproduce unmerged retriever evidence
3. failure analysis
4. only if needed:
   - adaptive fallback
   - fusion adjustment
   - query rewrite
   - reranker

reranker는 candidate recall이 확보됐지만 ordering failure가 실제로 존재할 때만 실험한다.

## 7.6 Stop condition

- current retrieval baseline의 주요 miss 유형 설명
- adaptive/hybrid의 실제 개선 여부 설명
- retrieval vs generation failure 분리
- unanswerable 질문에서 answer suppression 검증

이 네 가지에 답하면 새로운 embedding model/vector DB를 추가하지 않고 종료한다.

---

# 8. Axis 3 — Full-stack Chat Service Delivery

## Research question

> backend durable lifecycle과 frontend streaming UX가 같은 request state를 일관되게 표현하는가?

## 8.1 Target frontend boundaries

현재 거대한 `app/chat/page.js`를 무조건 component 수로 쪼개지 않는다.

먼저 다음 책임 경계를 확인한다.

```text
ChatPage
  |
  +-- conversation/session state
  +-- run/request state
  +-- transport adapter
  |      + REST
  |      + SSE
  +-- server payload → view model
  +-- intent result rendering
  +-- input composer
```

실제 change coupling이 큰 부분부터 분리한다.

## 8.2 Streaming UX invariants

- accepted 후 request ID를 보존
- token은 partial presentation일 뿐 canonical result가 아님
- done 후 persisted payload와 UI가 일치
- disconnect 시 immediately duplicate request를 만들지 않음
- incomplete stream이면 durable request state 재조회 가능
- refresh 후 running/completed request를 복구
- retryable/non-retryable error를 구분
- cancel/delete 이후 stale token/done을 화면에 반영하지 않음

## 8.3 Browser-level scenarios

- normal streaming
- duplicate click
- slow first token
- mid-stream network failure
- refresh while processing
- session switch while processing
- delete session while processing
- retry after failed
- history re-entry
- citation/evidence rendering
- structured intent result rendering

## 8.4 Frontend metrics

성능 경쟁을 위한 benchmark가 아니라 UX evidence로 기록한다.

- accepted → first visible feedback
- first token latency
- recovery time after reconnect/refresh
- duplicate submit prevented count
- terminal UI consistency
- console/runtime error count in representative flows

## 8.5 Stop condition

주요 browser failure scenario에서 durable backend state와 rendered UI가 일치하고, 이를 자동화된 integration/E2E test로 재현할 수 있으면 종료한다.

---

# 9. Cross-axis integration experiment

세 축을 독립적으로 끝낸 뒤 하나의 representative vertical slice를 검증한다.

## Scenario

사용자가 복지 정책 질문을 보낸다.

```text
1. frontend creates idempotent request
2. backend persists accepted run
3. intent/context resolved
4. retrieval executes
5. evidence sufficiency checked
6. LLM generates grounded answer
7. result validated
8. assistant result persisted
9. SSE streams progress/tokens
10. frontend renders evidence/citation
11. refresh/reconnect reads same durable result
```

### Fault injection

같은 scenario에서 mid-stream disconnect를 주입한다.

기대 invariant:

- backend run은 독립적으로 terminal state로 수렴
- reconnect/reload에서 동일 result 복구
- duplicate assistant message 없음
- retrieval trace/evidence 유지

이 한 시나리오가 3축의 연결 증거가 된다.

---

# 10. Observability

새 observability framework 도입은 목표가 아니다.

최소 correlation keys:

- chat_session_id
- user_message_id
- request_id
- idempotency_key
- assistant_message_id
- retrieval run/config ID
- trace/correlation ID if existing

최소 stages:

```text
request.accept
request.processing
routing
retrieval
generation
validation
persistence
stream.done
request.terminal
```

각 단계에서 가능하면:

- latency
- error type
- retry
- retrieval evidence IDs
- final request state

를 남긴다.

---

# 11. Implementation order

## Cycle 0 — Baseline consolidation

1. setup isolated backend test environment
2. run current develop chat/lifecycle/eval tests
3. diff unmerged retrieval/lifecycle branches
4. classify unique commits: adopt / obsolete / evidence-only
5. freeze deep-dive baseline revision

**No product feature changes yet.**

## Cycle 1 — Lifecycle failure proof

가장 작은 failure matrix부터 만든다.

우선순위:

1. disconnect after accepted
2. duplicate submit
3. reconnect after completion
4. session deletion during processing

관측된 defect가 있을 때만 refactor.

## Cycle 2 — Retrieval attribution

- current baseline
- unmerged retriever evidence reproduction
- failure classes
- adaptive/hybrid decision

## Cycle 3 — Frontend state/transport boundary

Cycle 1 lifecycle contract를 frontend가 정확히 소비하도록 정리.

- request state
- SSE connection state
- rendered message state

를 분리한다.

## Cycle 4 — Integrated vertical slice

browser → backend → retrieval → generation → persistence → reconnect까지 검증.

## Cycle 5 — Bounded AI execution control

Cycle 0–4의 결과를 전제로, LLM/RAG 호출을 무제한 병렬 처리하지 않는 실행
경계를 실험한다. 이 cycle은 provider·model 교체나 worker 수 튜닝이 아니라,
고정된 offered load에서 concurrency/deadline/fallback 정책이 만드는 trade-off를
측정하는 작업이다. 상세 가설과 gate는 §17에 정의한다.

---

# 12. Explicit non-goals

이번 cycle에서 자동으로 하지 않는다.

- 챗봇 framework 전면 교체
- LangGraph를 쓰기 위한 LangGraph 확장
- multi-agent
- 새로운 vector DB
- embedding model sweep
- LLM model benchmark sweep
- Local LLM serving
- Kafka/Celery/Redis Streams 도입
- Kubernetes
- microservice 분리
- 디자인 전면 개편
- 모든 legacy compatibility 제거
- unrelated recommendation domain rewrite

이 항목은 실제 측정된 defect의 해결에 필수일 때만 별도 cycle로 제안한다.

---

# 13. Evidence outputs

Backend repo를 이 cross-repo deep dive의 primary planning source로 사용한다.

초기 예정 경로는 다음과 같았다.

```text
docs/deep-dive/chatbot/
  00-current-state.md
  01-three-axis-plan.md
  02-lifecycle-baseline.md
  03-lifecycle-failure-analysis.md
  04-retrieval-baseline.md
  05-retrieval-failure-attribution.md
  06-fullstack-delivery.md
  07-integrated-evaluation.md
  08-final-report.md
```

현재 branch에는 위 초기 구조와 달리 `00-current-state.md`부터
`10-evidence-review-gate.md`까지의 실제 evidence 문서가 있다. 향후 Cycle 5는
기존 결과를 덮어쓰지 않고, 별도 `11-ai-execution-control-*.md` 계열과 raw
generated artifact로 남긴다.

raw benchmark/result는 generated artifact로 분리하고 문서에는:

- revision
- environment
- command
- fixture
- metric
- result path
- limitation

을 남긴다.

---

# 14. Portfolio claim boundary

목표 문장은 기능 나열이 아니다.

최종적으로 다음 수준의 claim을 **실측으로 확인된 범위만** 사용할 수 있게 만든다.

> 도담 AI 챗봇에서 대화 요청의 durable lifecycle과 SSE streaming connection을 분리하고, 중복 요청·연결 단절·재접속 상황에서 request/message 일관성을 검증했습니다.

> RAG에서는 검색 실패와 생성 실패를 분리하는 evaluation funnel을 구성하고, 실제 failure case를 기준으로 retrieval 전략을 비교했습니다.

> 프론트엔드에서는 persistent request state를 기준으로 streaming·retry·history 복구를 연결해 full-stack chatbot lifecycle을 검증했습니다.

수치와 개선 폭은 실험 종료 전에는 작성하지 않는다.

---

# 15. Historical review gates before implementation

다음 항목은 Cycle 1을 시작하기 전의 review gate였으며, 현재는 §0.1의
artifact로 결과를 추적한다. 다시 열린 질문은 Cycle 5 gate와 혼동하지 않는다.

1. **Baseline integration**
   - `refactor/chat-handler-result-lifecycle`의 33 unique commits 중 무엇을 가져올지
   - current develop에 없는 retrieval/lifecycle improvements를 selective integration할지
   - 아니면 current develop에서 실험을 재구현할지

2. **State authority**
   - ChatGraphState의 duplicate profile/pending path 중 authoritative contract 결정

3. **Durable run authority**
   - DB `chat_request`를 request lifecycle source of truth로 둘지 확인

4. **Frontend recovery UX**
   - stream incomplete 후 자동 status lookup/recovery를 기본 UX로 할지 결정

5. **Evaluation corpus**
   - 기존 retrieval gold set을 canonical evaluation set으로 재사용 가능한지 leakage/audit commit 기준 재확인

---

# 16. Historical first step

이 문서가 최초 작성됐을 때 첫 구현은 대규모 refactor가 아니었다.

다음 두 작업만 한다.

### A. Backend baseline environment + branch evidence consolidation

- current develop test baseline 확보
- `feat/chat-retriever-layer`
- `refactor/chat-handler-result-lifecycle`

두 branch의 unique code/evidence를 current develop과 비교하고 integration map 작성.

### B. First lifecycle discriminating test

`request accepted → SSE disconnect → backend completion → status/result recovery`

한 시나리오를 현재 코드로 재현한다.

현재 구현이 이미 invariant를 만족하면 구조를 바꾸지 않았다.

실패할 경우 그 failure를 첫 bounded lifecycle defect로 삼아 최소 수정했다.

---

# 17. Next deep dive — Bounded AI Execution Control

## 17.1 Why this is the next lower-layer question

세 축의 결과만으로는 AI 서비스가 부하·provider failure·사용자 취소 상황에서
제한된 자원을 어떻게 사용하는지 설명할 수 없다. 특히 hosted LLM을 호출하는
현재 구조에서 app-server CPU 사용률만으로 병목을 판단하면 안 된다. 우선
제약으로 모델링할 대상은 다음이다.

- concurrent in-flight LLM/embedding call
- provider rate limit 및 transient failure
- request deadline과 tail latency
- token/call budget
- client disconnect 또는 explicit cancel 뒤의 wasted work
- retry가 만드는 duplicate cost와 downstream overload

따라서 이 cycle의 목적은 “안정적인 챗봇”이라는 포괄적 표현이 아니다.

> LLM을 가변 지연·비용을 가진 외부 실행 자원으로 다루고, 제한된 concurrency와 deadline 안에서 durable 사용자 결과를 보존하는 정책을 실측한다.

## 17.2 Scope and selected execution path

첫 대상은 이미 deterministic fallback을 가진 **추천 request의 LLM rerank**로
한정한다. 현재 workflow에서 candidate search, rule filter, assessment, result
build는 LLM rerank보다 앞서며, rerank 실패 뒤에도 rule/assessment 기반 결과가
남아야 한다. 이 경로는 “LLM이 실패해도 전체 request가 실패해야 하는가”를
판단하기에 가장 작은 bounded slice다.

Chat token streaming과 policy summary는 동일한 control contract를 적용할 수
있지만, 첫 실험에 동시에 포함하지 않는다. 서로 다른 UX/streaming 문제를
섞으면 execution-control 결과를 해석할 수 없기 때문이다.

```text
recommendation request
  → deterministic candidate/rule/assessment result
  → admission to bounded LLM rerank lane
  → completed LLM augmentation
      ├── success  → augmented durable result
      ├── deadline / provider failure → labelled deterministic fallback
      └── caller cancel → no late overwrite; terminal state follows request contract
```

## 17.3 Research questions and non-assumptions

1. 현재 구현에서 LLM rerank의 actual in-flight concurrency, queueing point,
   timeout, retry, cancellation authority는 어디인가?
2. offered load가 concurrency limit을 넘을 때, unbounded dispatch와 bounded
   dispatch는 p95 completion, fallback rate, timeout/429 rate, wasted calls에
   어떤 차이를 만드는가?
3. LLM 실패 또는 deadline expiry 뒤에도 deterministic result가 사용자에게
   반환되고, LLM late completion이 이를 덮어쓰지 않는가?
4. 어느 concurrency 값이 주어진 provider/environment에서 더 높은 처리량을
   보이는가가 아니라, 정해진 latency/cost/error budget 안에서 가장 좋은
   completion 결과를 만드는가?

이 문서는 아직 현재 코드가 unbounded이거나 특정 concurrency 값이 최적이라고
주장하지 않는다. 먼저 topology와 baseline을 관측해야 한다.

## 17.4 Contracts to preserve

- LLM augmentation은 candidate/rule/assessment의 authoritative 판단을 바꾸지 않는다.
- fallback은 정상 LLM success처럼 기록하지 않고 `fallback_used`, failure reason,
  provider-call outcome을 distinguish한다.
- 동일 idempotency key는 LLM rerank를 중복 실행하지 않는다.
- caller cancellation 또는 request terminal transition 뒤의 late LLM completion은
  durable result를 overwrite하지 않는다.
- concurrency limit은 global/per-process/per-tenant 중 실제 scope를 명시한다.
  multi-worker global limit이라고 추정하지 않는다.
- queue/deadline 예산을 초과한 request의 user-visible 결과와 retry 권한을 명시한다.

## 17.5 Measurement design

### Controlled harness

실제 provider 비용이나 production DB를 먼저 사용하지 않는다. controllable fake
LLM/provider adapter로 다음 fault를 주입하고, isolated DB에서 durable request와
result transition을 관측한다.

- fixed delay / long-tail delay
- timeout
- rate limit (429)
- transient provider failure (5xx)
- completion 직전 cancel
- late completion after fallback or terminal transition

같은 input fixture, same arrival pattern, same deadline을 고정한 뒤 concurrency
limit 후보(예: 1, 2, 4, 8)는 **환경별 experiment variable**로만 비교한다.
임의의 값 하나를 production default로 정하지 않는다.

실제 provider 재현은 credentials, quota, corpus provenance가 확인된 뒤 별도
measurement으로 수행한다. fake-provider 결과는 control-flow/cost-model evidence이지
provider latency 또는 production capacity claim이 아니다.

### Metrics

| Layer | Required measurement | Interpretation boundary |
| --- | --- | --- |
| Admission | offered requests, admitted count, max in-flight calls, queue wait p50/p95 | per-process/global scope를 함께 기록 |
| Completion | durable terminal completion rate, success/fallback/failed/cancelled ratio, end-to-end latency p50/p95 | fallback은 success와 합치지 않음 |
| Provider | call count/request, timeout/429/5xx rate, retry count, in-flight duration | fake provider에서는 provider quality 수치가 아님 |
| Waste | cancelled/terminal request 뒤 시작 또는 완료된 call 수, late-write count, duplicate call count | request idempotency와 분리해 기록 |
| Quality boundary | deterministic fallback validity, evidence-review verdict distribution | answer factuality/production retrieval quality를 대리하지 않음 |
| Cost | known token count 또는 provider usage가 있는 경우 token/request, token spent after cancel | 가격은 provider/model/date를 붙일 때만 금액 환산 |

### Decision table

| Observation | Permitted next decision |
| --- | --- |
| Lower concurrency lowers 429/timeout and keeps p95 within agreed budget | bounded admission policy 후보로 채택 검토 |
| Higher concurrency improves throughput but causes tail latency, provider failure, or waste amplification | concurrency increase를 거부하고 queue/deadline 정책을 검토 |
| Fallback remains deterministic and late overwrite is zero | degradation contract 유지 |
| Fallback leaks unsupported policy output or late completion overwrites result | capacity tuning을 멈추고 correctness defect부터 수정 |
| No meaningful difference under controlled load | production control 변경 없이 topology/observability만 기록 |

수치 목표(p95, acceptable fallback rate, maximum token waste)는 provider quota,
사용자 SLA, 비용 예산을 확인한 뒤 human gate에서 정한다. 이 계획은 임의의
pass threshold를 발명하지 않는다.

## 17.6 Evidence and claim boundary

Cycle 5 종료 시 최소 산출물은 다음이다.

```text
docs/deep-dive/chatbot/
  11-ai-execution-control-baseline.md
  12-ai-execution-control-fault-matrix.md
  13-ai-execution-control-results.md
experiments/chatbot/
  <controlled execution harness and machine-readable result>
```

가능한 claim의 예시는 다음으로 제한한다.

> 추천 rerank를 가변 지연 외부 의존성으로 모델링하고, controlled failure/load에서
> concurrency·deadline·fallback 정책별 durable completion, tail latency, provider
> failure, cancellation waste를 분리해 측정했다.

fake provider만 사용한 경우 “운영 API 비용 절감”, “production capacity 확보”,
“최적 worker 수”라고 주장하지 않는다. 실제 provider와 workload에서 측정한
값은 environment, revision, quota, input distribution을 포함해 별도 표기한다.

## 17.7 Human gates before implementation

다음 결정은 코드 변경이나 load execution 전에 사람이 승인한다.

1. 첫 대상이 recommendation rerank인지, chat generation인지
2. 적용하려는 limit scope가 process-local인지 global인지
3. 대표 arrival pattern과 user-visible deadline
4. 허용 가능한 fallback semantics와 사용자 문구
5. 실제 provider/quota를 쓰는 measurement의 비용 예산과 권한

승인 전에는 current topology를 read-only로 추적하고 controlled fake-provider
harness의 계약만 설계한다.

---

# 18. Superseded closure decision — RAG grounding and bounded execution control

## 18.1 Agreed stopping point

이전 Dodam deep dive 종료선은 다음 두 산출물이 검증되면 닫는 것으로 정했다.

1. **RAG grounding:** 실제 `search_policy_chunks()` 호출이 최종적으로 반환한
   chunk만 `decision_run → decision_claim → evidence_span → claim_evidence`에
   retrieval evidence로 기록하고, isolated DB에서 그 row 수를 확인한다.
2. **Bounded execution control:** recommendation rerank의 durable ownership,
   cancellation, fallback, late-write fence, process-local admission telemetry를
   controlled fake-provider/isolated DB 조건에서 확인한다.

검색 trace는 answer correctness나 retrieval quality score를 뜻하지 않는다. 이는
"어떤 query가 어떤 근거 chunk를 반환하여 사용자 결과에 사용될 수 있었는가"를
재현 가능하게 남기는 provenance다.

## 18.2 Explicitly excluded from this repository cycle

다음 항목은 이번 종료 조건이 아니다.

- multi-worker/global provider admission, durable queue, lease heartbeat, restart
  recovery, distributed retry;
- provider별 quota·가격·token budget에 기반한 production capacity tuning;
- Kafka, Celery, Redis Streams, Kubernetes 또는 microservice 분리;
- real-provider latency/quality benchmark 또는 production SLA claim.

현재 lane은 설정 시 process-local semaphore일 뿐이다. 이를 global control로
표현하지 않는다.

## 18.3 Cross-project infrastructure boundary

대규모 **LLM** 실행 제어는 EDA의 compute-job infrastructure와 동일하지 않다.
LLM 서비스에는 provider quota, token/call cost, retrieval fan-out, model fallback,
tenant fairness, streaming disconnect가 추가된다. 따라서 Dodam은 위 종료선까지
LLM-specific evidence/degeneration contract를 남긴다.

반면 durable `Run / Attempt / Lease / Worker / Recovery` 인프라의 장시간 실행·
worker crash·recovery 입증은 EDA를 primary proving ground로 둔다. ECOUNT는
DB-authoritative mutation과 reconciliation의 별도 증거를 제공한다. 향후 Dodam에
multi-worker LLM admission을 도입할 필요가 실제로 확인되면, provider scope와
quota/cost budget을 먼저 human gate에서 정한 별도 cycle로 시작한다.

## 18.4 Final measurement gate

실제 vector search probe는 query text를 configured embedding provider에 전송할 수
있다. 따라서 다음 한 번의 isolated probe는 그 external egress와 provider 비용에
대한 명시적 승인 뒤에만 실행한다.

```text
search_policy_chunks(query, policy_id, top_k)
  → returned chunk IDs
  → decision_run / decision_claim / evidence_span counts
```

기록할 수치는 returned chunk 수, persisted decision run 수, retrieval evidence
claim 수, evidence span 수와 local search-and-trace elapsed time이다. local elapsed
time은 provider latency나 retrieval quality metric으로 해석하지 않는다.

### Execution record (2026-10-09)

명시적으로 승인된 비개인 query `산재근로자 심리상담 신청 방법`을 policy ID `1`,
`top_k=3`으로 한 번 실행했다. configured embedding provider와 isolated local
PGVector를 사용한 이 실행은 chunk `20002`, `20001`, `20005` 세 건을 반환했고,
새로운 `decision_run=1`, `retrieved evidence claim=3`, `evidence_span=3`을
기록했다. local search-and-trace elapsed time은 `3621.173 ms`였다.

이는 한 query의 provenance wiring evidence다. retrieval relevance, recall/precision,
provider latency, real-user traffic, production cost 또는 capacity claim은 아니다.
§18.1의 두 종료 산출물은 충족됐다. 다만 이 판단은 candidate-level RAG fan-out의
별도 resource control을 확인하기 전의 종료선이었다. 이 문서의 종료 표현은 §19의
bounded follow-up으로 대체한다.

---

# 19. Bounded follow-up — separate RAG fan-out control

## 19.1 Why the earlier closure was insufficient

RAG evidence trace는 반환 근거의 provenance를 남기지만, 후보별
`asyncio.gather()`가 외부 chunk search에 만드는 동시 실행 피크를 제어하지 않는다.
Rerank admission lane만으로는 이 이전 단계의 quota pressure, queue wait, 또는
candidate fan-out blast radius를 측정할 수 없다.

## 19.2 Implemented boundary

candidate-level evidence search에는 rerank와 공유하지 않는 optional
process-local lane을 둔다. unset은 기존 unbounded 동작을 보존하고, 설정된 값은
한 Python process에서만 유효하다. result summary에는 evidence search call count와
lane capacity/max in-flight/queue-wait p50/p95를 남긴다.

## 19.3 Controlled evidence

`docs/deep-dive/chatbot/15-rag-fanout-execution-control.md`의 fake-search
comparison은 4개 후보 fixture에서 peak 4 → 2 → 1을 확인했고, 같은 4개 호출을
완료하는 대신 elapsed p50이 약 21.255 → 42.627 → 84.699 ms로 증가함을 기록한다.
이는 control tradeoff evidence이며 provider capacity, actual cost saving 또는
production concurrency claim이 아니다.

## 19.4 Remaining explicit boundary

RAG 큐 대기 항목은 admission 직후 durable execution ownership을 재확인하고,
취소가 이기면 `RAG_EVIDENCE_DROPPED`로 provider call 없이 종료한다. 이때
방지 호출 수와 tokenizer 기반 예상 embedding input tokens를 별도 기록한다.
actual LLM usage는 provider response가 반환할 때만 `llm_provider_token_usage`로
기록한다. 자세한 controlled boundary는
`docs/deep-dive/chatbot/16-token-waste-telemetry.md`에 남긴다.

동일한 후보 4개/capacity 1/첫 검색 직후 취소 fixture를 baseline과 treatment에
각각 100회 반복했을 때, baseline의 started calls는 400(취소 뒤 300)이었고
treatment는 100(취소 뒤 0)이었다. 즉 이 조건에서 started calls 75% 감소,
post-cancel call 300 → 0을 관측했다. 이는 deterministic fake-search control-flow
evidence이며 production cancellation rate, provider cost, global capacity claim은 아니다.

이는 final recheck 뒤 이미 시작한 call의 cancellation 또는 global/multi-worker
admission을 해결하지 않는다. durable queue, provider quota/cost budget, real-provider
usage sample도 다음 human gate의 결정 항목이다.

---

# 20. Versioned execution dataset experiment

단일 fixture의 반복 횟수를 headline으로 쓰지 않기 위해, `dodam_execution_v1`은
후보 2/4/8, lane capacity 1/2, 취소 시점 두 종류, fake provider 성공/오류를
조합한 24개의 명시적 scenario를 versioned JSONL로 고정한다. Baseline과 treatment는
같은 dataset item을 한 번씩 실행하고 item-level call/error/cancellation score와
aggregate scorecard를 비교한다.

이 방식은 처음에는 Langfuse-style dataset experiment의 구조를 로컬에서 구현한
것이었다. 이후 opt-in Langfuse SDK export를 추가해 같은 24개 local dataset item을
baseline/treatment 각각의 experiment run으로 전송하고, item-level boolean score와
run-level synthetic call-count score를 확인했다. 이는 experiment observability
연결 증거일 뿐 production request instrumentation은 아니다.
`docs/deep-dive/chatbot/17-execution-dataset-experiment.md`의 v1 결과는
post-cancel starts 94 → 0, fake-search starts 112 → 18(-83.929%)를 기록한다.
이는 versioned local fake-search dataset에 한정된 control-flow claim이다.
