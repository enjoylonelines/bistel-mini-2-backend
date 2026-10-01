# Dodam AI Chatbot 3-Axis Deep Dive Plan

> Date: 2026-09-30  
> Status: planning / implementation not started  
> Primary repository: `bistel-mini-2/bistel-mini-2-backend`  
> Companion repository: `bistel-mini-2/bistel-mini-2-frontend`  
> Backend baseline: `origin/develop@08ebc167384cca490303059dcb927887c300bd12`  
> Frontend baseline: `origin/develop@50690f27ff6689c2f810509fab96afb5bc913ad1`

## 0. Goal

이번 작업의 목적은 도담을 기능 수가 많은 AI 데모로 확장하는 것이 아니다.

현재 구현의 구조적 결함과 이미 검증된 기능을 먼저 구분하고, 다음 세 축만 깊게 검증한다.

1. **Conversation / Request Lifecycle**
2. **RAG / Retrieval Quality & Failure Attribution**
3. **Full-stack Chat Service Delivery**

최종적으로 다음 질문에 실측과 재현 가능한 evidence로 답할 수 있어야 한다.

> 하나의 사용자 메시지가 네트워크 단절·중복 요청·LLM 오류가 있어도 하나의 durable 결과로 수렴하는가?

> 답변 실패가 검색 실패인지 생성 실패인지 분리해 측정할 수 있는가?

> 백엔드의 durable run과 프론트의 ephemeral SSE connection을 분리하고도 사용자 경험이 일관되게 유지되는가?

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

예정:

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

현재 단계에서는 이 plan 문서만 작성한다.

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

# 15. Review gates before implementation

다음 항목을 먼저 확인하고 Cycle 1 구현을 시작한다.

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

# 16. Immediate next step

계획 승인 후 첫 구현은 대규모 refactor가 아니다.

다음 두 작업만 한다.

### A. Backend baseline environment + branch evidence consolidation

- current develop test baseline 확보
- `feat/chat-retriever-layer`
- `refactor/chat-handler-result-lifecycle`

두 branch의 unique code/evidence를 current develop과 비교하고 integration map 작성.

### B. First lifecycle discriminating test

`request accepted → SSE disconnect → backend completion → status/result recovery`

한 시나리오를 현재 코드로 재현한다.

현재 구현이 이미 invariant를 만족하면 구조를 바꾸지 않는다.

실패할 경우 그 failure를 첫 bounded lifecycle defect로 삼아 최소 수정한다.
