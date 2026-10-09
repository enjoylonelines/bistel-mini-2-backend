# Opt-in Langfuse recommendation telemetry

Date: 2026-10-09  
Scope: completed recommendation executions after the durable result transaction commits

## What is connected

`RecommendationLangfuseTelemetry` is called only after the recommendation
result and terminal execution event have committed in each current execution
entry point:

- background recommendation request;
- recommendation SSE request;
- chat-branch recommendation request.

The feature is disabled by default. Experiment export credentials alone do not
activate application tracing. Enable it explicitly in the local deployment
environment and restart the application:

```dotenv
LANGFUSE_RECOMMENDATION_TRACING_ENABLED=true
```

## Trace schema and privacy boundary

One completed recommendation creates this hierarchy:

```text
recommendation-execution (span)
├── recommendation-rag-evidence (retriever)
└── recommendation-rerank (generation, only when a provider call started)
```

The root records numeric request ID, terminal outcome, elapsed time, result
count, provider-call count, fallback flag, and a coarse error category. The
retriever records candidate/evidence counts, started/avoided calls, tokenizer
estimate, lane capacity/max in-flight/queue-wait p50/p95, and whether a search
error existed. The generation records the rerank model, actual provider token
usage only when returned by the provider, fallback flag, and a coarse error
category.

It never receives or serializes raw query text, profile fields, policy text or
name, evidence snippets, recommendation prose, provider prompt, or provider
completion. A telemetry failure is caught and logged; it cannot fail or roll
back a committed user result.

`usage_details` uses exclusive `input` and `output` buckets derived from the
provider-reported token fields. The local tokenizer estimate for prevented
embedding calls remains separate and is not a provider invoice or cost value.

## Controlled receipt check

A 2026-10-09 controlled fake-provider trace was sent with
`telemetry_source=controlled_fake_provider` and
`data_classification=synthetic_control_only`. Langfuse received all three
observations, with a `GENERATION` rerank observation containing the controlled
rate-limit/fallback fields and separate input/output token usage.

This checks trace shape and redaction only. It does not demonstrate a real
recommendation request, provider latency, provider billing, production traffic,
or capacity. A real application trace requires the explicit flag above and a
fresh application process.

## Local E2E execution record

On 2026-10-09, a freshly restarted local application backed by the disposable
`dodam_e2e` database completed one non-personal, fixed low-income legal-support
recommendation fixture after its follow-up answers were supplied. The committed
result contained two recommendations. The post-answer execution trace recorded:

- root elapsed time: `6678.669 ms`;
- RAG: two started evidence-search calls and four returned evidence chunks;
- avoided RAG calls: zero, because no lane capacity was configured;
- rerank: one provider call, successful augmentation (no fallback);
- provider-reported usage: 3,657 input tokens, 814 output tokens, 4,471 total.

Langfuse received the application root span, RAG retriever, and rerank generation
with the operational-only schema above. Its UI displayed an inferred model cost
of `$0.006406`; this is a Langfuse model-configuration estimate, not a provider
invoice.

This is one local E2E observation using fixture policy data. It is not a
latency percentile, relevance evaluation, production traffic sample, provider
billing measurement, capacity result, or global-concurrency proof. Queue wait
and in-flight fields remain unavailable in this record because admission lanes
were intentionally not configured.
