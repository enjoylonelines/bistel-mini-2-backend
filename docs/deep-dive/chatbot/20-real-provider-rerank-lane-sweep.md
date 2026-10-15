# Real-provider rerank lane sweep

Date: 2026-10-13
Runner: `experiments/chatbot/run_real_provider_rerank_lane_sweep.py`

## Scope

This is a bounded real-provider measurement using one fixed, non-personal
rerank fixture. It does not call the recommendation API, database, or RAG
search. Each capacity ran five times, with request concurrency bounded to the
smaller of `capacity × 2` and five. The lane is process-local.

Command:

```bash
PYTHONPATH=. .venv/bin/python experiments/chatbot/run_real_provider_rerank_lane_sweep.py \
  --execute --runs 5 --capacities 1 2 4 \
  --output /tmp/dodam-real-provider-rerank-lane-sweep-v1.json
```

## Result

| Lane capacity | Request concurrency | Elapsed p50 / p95 | Queue wait p50 / p95 | Fallback / error | Total tokens / request |
| ---: | ---: | --- | --- | --- | ---: |
| 1 | 2 | 5,512.317 / 6,769.326 ms | 2,838.064 / 3,641.667 ms | 0 / 0 | 3,195.4 |
| 2 | 4 | 5,126.642 / 5,222.587 ms | 2,510.161 / 2,911.025 ms | 0 / 0 | 3,176.2 |
| 4 | 5 | 2,797.436 / 4,804.852 ms | 0.001 / 2,254.493 ms | 0 / 0 | 3,174.0 |

The 15 calls consumed 47,728 provider-reported total tokens. Token volume was
effectively stable across capacity values because the same fixed fixture was
sent once per run. The latency movement is therefore consistent with local lane
queueing rather than a prompt-size change.

## Permitted conclusion

For this fixed fixture and this one process, capacity 4 reduced p95 elapsed
time relative to capacity 1 by about 29%, while preserving zero fallbacks and
errors. This is a directional admission-control result, not a production SLO,
capacity recommendation, provider quota measurement, multi-worker result, or
user-traffic result. Five samples per capacity are insufficient to choose a
permanent default.

The next run should compare the two best candidates (2 and 4) with 20 runs
each, retaining the same fixture and setting an explicit request/token budget
before execution.

## Expanded proportional-load run (2026-10-13)

The next bounded run executed 20 requests for each capacity. To keep each lane
under the same relative offered load, request concurrency was `capacity × 2`:
four for capacity 2 and eight for capacity 4.

| Lane capacity | Request concurrency | Elapsed p50 / p95 / max | Queue wait p50 / p95 / max | Fallback / error | Total tokens / request |
| ---: | ---: | --- | --- | --- | ---: |
| 2 | 4 | 5,218.311 / 6,568.528 / 6,618.390 ms | 2,606.279 / 3,362.837 / 3,628.103 ms | 0 / 0 | 3,182.0 |
| 4 | 8 | 4,939.391 / 5,261.062 / 5,738.343 ms | 2,491.325 / 2,821.803 / 2,914.108 ms | 0 / 0 | 3,185.45 |

The 40 calls consumed 127,349 provider-reported total tokens. Under this
proportional-load setup, capacity 4 had a 19.9% lower p95 elapsed time and a
16.1% lower p95 queue wait than capacity 2, with no fallback or recorded
errors.

This is **not** a same-offered-load capacity selection. The concurrency differs
between rows, so the result says that capacity 4 handled twice the offered load
better than capacity 2 handled its own relative load. A direct choice requires
one more 20-request capacity-2 run at the same concurrency 8 already used for
capacity 4; then the existing capacity-4 row can serve as the matching control.

## Matched offered-load comparison (2026-10-13)

A further 20-request run held request concurrency at 8 for capacity 2. The
capacity-4 row from the expanded run used the same fixed fixture, run count,
provider configuration, and offered load, so it is the direct control.

| Lane capacity | Request concurrency | Elapsed p50 / p95 / max | Queue wait p50 / p95 / max | Fallback / error | Total tokens / request |
| ---: | ---: | --- | --- | --- | ---: |
| 2 | 8 | 11,853.960 / 12,991.330 / 13,385.679 ms | 9,109.522 / 9,897.906 / 10,025.841 ms | 0 / 0 | 3,165.2 |
| 4 | 8 | 4,939.391 / 5,261.062 / 5,738.343 ms | 2,491.325 / 2,821.803 / 2,914.108 ms | 0 / 0 | 3,185.45 |

At the same offered load, capacity 4 reduced p95 elapsed time by **59.5%** and
p95 queue wait by **71.5%** relative to capacity 2. Provider-reported token
volume stayed within 0.6% per request, and no fallback or recorded error
occurred in either row. For the fixed fixture and one process, this supports
choosing capacity 4 over capacity 2 as the current baseline.

It still does not establish a universal production default: the experiment has
one fixture, 20 observations per row, one provider configuration, and no
multi-worker or user-traffic sample. Treat capacity 4 as a monitored starting
value, revisiting it when production traces show a different request mix,
provider quota, or queueing pattern.
