# QuantOS V1 Multi-day Offline Evaluation

**SYNTHETIC FIXTURE — NOT REAL-HISTORICAL PERFORMANCE**

This benchmark validates the evaluation harness, PIT plumbing, artifact chain,
deterministic replay, and failure-safe product flow. It does not measure financial
accuracy, alpha, investment return, or production latency.

- Evaluation ID: `cb16cf52706b843b1c022cc02b02204735b8d053423904ebd6851c5a52390249`
- Data source: `SYNTHETIC_FIXTURE`
- Trading days: 10
- Runs: 19
- Candidates: 20
- POST_CLOSE success: 100.00%
- PRE_OPEN exact reuse: 100.00%
- Evidence coverage: 100.00%
- Attribution coverage: 100.00%
- Candidate Knowledge READY: 50.00%
- Candidate Knowledge EMPTY: 50.00%
- Run-level operational READY: 19/19
  (aggregate READY means at least one candidate is READY; other candidates may be EMPTY)
- Local fake-provider invocations: 20
- External provider network requests: 0
- PIT violations: 0
- Determinism mismatches: 0
- Average local POST_CLOSE fixture-chain runtime: 62.2 ms

The 50/50 READY/EMPTY split is deliberately constructed by the fixture and is not
a natural Knowledge hit rate. Runtime covers local candidate, Evidence, Attribution,
retrieval/context, fake synthesis, storage, operational manifest, and POST_CLOSE
TimeSlice work; it excludes PRE_OPEN replay and is not production latency.
