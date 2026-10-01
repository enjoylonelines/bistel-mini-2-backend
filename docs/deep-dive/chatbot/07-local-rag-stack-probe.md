# Dodam Deep Dive — Local RAG Stack Probe

> Date: 2026-09-30
> Status: local synthetic diagnostic only
> Production benchmark claim: prohibited

## 1. Why a local stack was created

The deployed backend is alive but its readiness check currently reports a DB
error, so fresh live retrieval cannot be executed against the deployed data.

To keep testing the current FastAPI/PGVector path without relabeling historical
numbers as fresh results, a disposable local stack was created.

Local components:

- FastAPI backend: current deep-dive branch
- PostgreSQL + pgvector: `pgvector/pgvector:pg16`
- local DB port: 55432
- embedding endpoint: Ollama OpenAI-compatible API
- local embedding model alias: `text-embedding-3-large` -> `nomic-embed-text`

The local stack uses a **synthetic policy 242 fixture** reconstructed only to
exercise the real application path.

It is not equivalent to the historical production corpus or OpenAI
`text-embedding-3-large`.

## 2. Local server boot result

The backend starts successfully with normal application lifespan enabled against
the disposable DB.

OpenAPI:

- title: `policy-rag-backend`
- paths: 47
- RAG ingest/search routes available

The first attempt against the host PostgreSQL failed because pgvector was not
installed. This was resolved by using a disposable pgvector container instead
of mutating the host PostgreSQL installation.

## 3. Ollama compatibility issue found

The first local embedding attempt failed even though Ollama exposes an
OpenAI-compatible `/v1/embeddings` endpoint.

Cause:

- LangChain OpenAI embeddings perform token-length-safe preprocessing;
- that path sent tokenized integer-array input;
- the local Ollama endpoint rejected that request shape as `invalid input type`.

Local-only fix:

- instantiate `OpenAIEmbeddings` with
  `check_embedding_ctx_length=False`;
- send raw string input to Ollama.

This change is confined to the experiment runner and is **not** a production code
change.

## 4. Synthetic R019 fixture

The local policy 242 fixture contains seven standard sections:

1. 정리된 지원 조건
2. 조건 구조
3. 공식 지원대상 원문
4. 지원 내용
5. 신청 방법
6. 신청 기간
7. 유의 사항

All seven chunks were ingested through the actual FastAPI RAG ingest endpoint.

Result:

- requested: 7
- embedded: 7
- collection: `policy_documents`

## 5. K=5 / K=7 / K=10 probe

Query:

> 법률 문제를 무료로 물어보려면 어떤 기관을 찾아가야 하나요?

Synthetic local result:

### K=5

1. 조건 구조
2. 정리된 지원 조건
3. 공식 지원대상 원문
4. 지원 내용
5. 신청 방법

### K=7

1. 조건 구조
2. 정리된 지원 조건
3. 공식 지원대상 원문
4. 지원 내용
5. 신청 방법
6. 유의 사항
7. 신청 기간

### K=10

Only seven chunks exist in this synthetic policy fixture, so the same seven are
returned.

Important boundary:

> This does **not** answer the historical R019 rank question.

The fixture text and embedding model differ from the historical production
benchmark. It only verifies that the current server, local pgvector storage, and
larger-K retrieval path work end to end.

## 6. Query-rewrite probe

Three query variants were tested against the same synthetic fixture.

### A. Original user query

`신청 방법` rank: 5

### B. Prefix only

> 신청 방법: + original query

`신청 방법` rank: still 5

The label alone slightly changes distances but does not materially change rank.

### C. Concrete method-context expansion

> 무료법률상담 신청 방법 기관 주민센터 대한법률구조공단

`신청 방법` rank: 1

Interpretation:

- a generic section-name prefix is not enough in this local probe;
- stronger lexical/context expansion can move the target section;
- however this is vulnerable to leaking known answer terms into the query.

Therefore the current evidence does **not** justify production query rewriting
with institution names.

A metadata-aware section filter/boost remains the cleaner challenger because the
application subtype is derived from user intent rather than from the expected
answer text.

## 7. Current decision

Do not claim local K results as production retrieval improvement.

Do not adopt concrete answer-term query expansion.

Keep the next production challenger bounded to:

```text
policy candidate known
    ↓
application subtype = method | period
    ↓
section-aware metadata filter/boost
    ↓
vector ranking inside that narrowed evidence space
```

The live production/historical question still requires either:

1. restored DB readiness and a fresh run against the original data/model path; or
2. a reproducible snapshot of the historical corpus.

## 8. Local experiment helpers

- `experiments/chatbot/bootstrap_local_rag_fixture.py`
- `experiments/chatbot/run_local_server.py`

Both are explicitly local diagnostic helpers and must not be used as portfolio
performance evidence.
