# Architecture

[English](architecture.md) | [简体中文](architecture.zh-CN.md)

QuantOS is a local, deterministic-first pipeline whose durable boundaries are
typed, versioned, and validated before downstream use.

## Layer map

1. **Market and Candidate** — canonical market observations are queried with an
   explicit PIT cutoff; deterministic anomaly and triage logic selects ranked
   candidates.
2. **Evidence** — announcements and web/news records remain event records with
   publication, collection, and availability provenance.
3. **Attribution** — deterministic eligibility chooses which Event Evidence may
   support an explanation. STRICT eligibility does not accept retrospective
   proxies.
4. **Knowledge Documents** — immutable, versioned reference material has its own
   document, family, content, and provenance identities.
5. **Retrieval** — an exact configured lexical index is selected. PIT-ineligible
   chunks are removed before document frequency, average length, and BM25 scores.
6. **Context Assembly** — ordered retrieval results are bounded by a versioned
   policy and split into historical and retrospective lanes.
7. **Guarded Synthesis** — validated Evidence and K-reference namespaces are
   supplied to structured synthesis. Cache validation occurs before lookup;
   causal language requires Attribution Evidence.
8. **Daily Product** — v1 represents legacy no-Knowledge output; v2 binds the
   supplied Knowledge context and structured synthesis into product identity.
9. **TimeSlice Product** — PRE_OPEN reuses an exact prior Daily artifact;
   POST_CLOSE reuses the exact target-date Daily artifact.
10. **Orchestration and Manifest** — readiness, execution results, exact artifact
    references, safe failures, and Knowledge operational state are persisted.
11. **Scheduler** — trading windows, leases, fencing, retry, and stale takeover
    trigger the generic operational adapter without knowing retrieval semantics.

## Core invariants

```text
KnowledgeDocument != EventEvidence != AttributionEvidence
                  != RetrievedContext != LLMContext
```

- Knowledge retrieval never grants attribution eligibility.
- STRICT and RESEARCH are separate contracts. Retrospective context cannot enter
  STRICT output.
- `available_at`, not merely `published_at`, controls historical visibility.
- PIT filtering precedes ranking statistics.
- Naive PIT-critical datetimes fail closed.
- Generated timestamps, paths, hostnames, mtime, and execution durations are
  observational and do not enter logical identity unless a byte-artifact
  contract explicitly includes them.

## Identity and storage

The identity graph runs from document and chunk IDs through index, request,
context, synthesis-input, report, artifact SHA, operational state, and run
identities. Each level binds the inputs that affect that level while excluding
deployment paths and runtime observations.

Repositories use canonical serialization, identity revalidation, atomic
publication, append-only or collision-safe writes, and corruption detection.
Daily JSON/Markdown pairs and referenced artifacts are verified together.

## Failure semantics

Operational Knowledge states are closed: `NOT_CONFIGURED`, `EMPTY`, and `READY`
are soft; `UNAVAILABLE`, `STALE`, `CORRUPT`, `FAILED_INTEGRITY`, and `FAILED` are
hard. Missing products, stale contracts, corrupt bytes, semantic mismatches, and
unexpected internal failures remain distinct. Persisted reasons are deterministic
codes and do not include raw exceptions, provider content, or credentials.

## Scheduler boundary

The scheduler owns schedule slots, due/grace windows, claims, leases, fencing,
retry, and takeover. It does not own BM25, index versions, K references, context
policy, or retrieval. Scheduler idempotency is not a claim of business-level
exactly-once execution.
