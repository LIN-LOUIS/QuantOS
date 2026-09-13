# QuantOS

[English](README.en.md) | [简体中文](README.md)

**Point-in-Time Safe Financial Intelligence Backend**

QuantOS is a deterministic-first, point-in-time-safe financial intelligence
backend for auditable, reproducible, and evidence-grounded market analysis.
It is an engineering backend—not an AI stock picker, trading bot, or order
execution system.

## Why QuantOS

Financial analysis becomes unreliable when future information leaks into a
historical decision, evidence is confused with background knowledge, or an LLM
is treated as the source of numerical truth. QuantOS makes these boundaries
explicit and validates them across storage, retrieval, synthesis, products,
operations, and replay.

## Design principles

- **Point-in-Time safety:** availability time, event time, and run time remain
  distinct; future-known data is excluded before ranking.
- **Deterministic first:** Python owns market facts, eligibility, identities,
  ranking, and validation. LLMs may produce guarded explanations only.
- **Evidence-grounded attribution:** causal language requires eligible event
  evidence. Retrieved knowledge never becomes attribution evidence.
- **Closed, versioned artifacts:** logical identities bind material inputs;
  storage validates identities and fails closed on corruption or collisions.
- **Auditable operations:** manifests, safe reason codes, exact artifact
  references, and provenance make local replay inspectable.

## Architecture

```text
Market Data
    ↓
Candidate
    ↓
Evidence
    ↓
Attribution
    ↓
Knowledge Retrieval
    ↓
Knowledge Context
    ↓
Guarded LLM Synthesis
    ↓
Daily Intelligence
    ↓
PRE_OPEN / POST_CLOSE
    ↓
Operational Manifest
    ↓
Scheduler
```

See [Architecture](docs/architecture.md) and [Core concepts](docs/concepts.md).

## Core features

- Timezone-aware market and availability timestamps with explicit `as_of_time`
- Immutable local market, evidence, knowledge, context, report, and run artifacts
- PIT-safe lexical retrieval with exact index selection
- Physically separated historical and retrospective knowledge lanes
- Guarded structured synthesis with evidence and knowledge reference validation
- Knowledge-aware Daily Intelligence v1/v2 and TimeSlice products
- PRE_OPEN reuse of an exact prior Daily artifact and POST_CLOSE exact reuse
- Operational readiness, failure manifests, scheduler leases, fencing, and retry
- Offline multi-day evaluation with replay and PIT metrics

## PIT model

`market_event_time` identifies the market event being explained.
`RunContext.as_of_time` is the operational evaluation cutoff. A knowledge item
is historically visible only when its `available_at` is no later than the event
cutoff. Publication time alone is not sufficient. PIT filtering occurs before
BM25 corpus statistics and ranking.

Naive datetimes are rejected for PIT-critical contracts. Equivalent aware
datetimes are canonicalized by instant for logical identity.

## Knowledge is not evidence

QuantOS keeps these types separate:

```text
KnowledgeDocument != EventEvidence != AttributionEvidence
                  != RetrievedContext != LLMContext
```

Knowledge provides background. It does not grant attribution eligibility, and
knowledge-only input cannot support a causal claim.

## STRICT and RESEARCH

- `strict_live` uses only information known by the market-event cutoff.
- `research` may additionally expose retrospective knowledge under an explicit
  corpus cutoff, in a separate lane.

A STRICT consumer rejects a RESEARCH product; retrospective content is never
silently dropped to manufacture a STRICT result.

## Supported in V1

PRE_OPEN and POST_CLOSE intelligence, STRICT and RESEARCH modes, market data,
candidate generation, evidence, attribution, local canonical knowledge,
lexical retrieval, context assembly, guarded synthesis, Daily/TimeSlice
products, operational manifests, deterministic scheduling, and offline
evaluation.

## Explicit non-goals

V1 does not provide INTRADAY Knowledge integration, semantic/vector retrieval,
runtime index rebuilding, autonomous retrieval planning, a REST API, web or
desktop UI, authentication/RBAC, cloud deployment, commercial data licensing,
or trade/order execution.

QuantOS V1 currently publishes the financial-intelligence backend only. Web,
desktop, and commercial product layers are intentionally outside the current
public backend scope. Future product interfaces may be released as open-source
or commercial components depending on the distribution strategy.

## Installation

Python **3.10 or newer** is required.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

The package has not been published to PyPI; do not use `pip install quantos` as
a release-install command.

## Quick start

```bash
git clone https://github.com/LIN-LOUIS/QuantOS.git
cd QuantOS
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
quantos --version
quantos doctor
quantos demo
```

`quantos demo` uses `SYNTHETIC_FIXTURE` and is **NOT REAL-HISTORICAL
PERFORMANCE**. It needs no API key and makes no request to a real LLM or real
market/news provider. Real market reports still require provider/configuration,
canonical local artifacts, and the corresponding Evidence/Knowledge preparation;
cloning the repository alone does not create a real A-share report. See
[Quick start](docs/quickstart.md).

## CLI entry points

```bash
quantos doctor
quantos demo
quantos health --help
python -m quantos --help
python scripts/run_quantos.py --help
python scripts/generate_daily_report.py --help
python scripts/generate_time_slice_report.py --help
python scripts/run_scheduler_once.py --help
python scripts/evaluate_v1.py --help
```

Provider-capable paths are explicit and environment-configured. The default
evaluation and the public CI suite use local fakes and make no provider request.

## Offline evaluation

**Data source: `SYNTHETIC_FIXTURE` — NOT REAL-HISTORICAL PERFORMANCE.**

The audited `evaluation/v1-strict-10d/` snapshot contains 10 trading days, 19
runs (10 POST_CLOSE and 9 eligible PRE_OPEN replays), 20 candidates, 100%
POST_CLOSE and PRE_OPEN completion, 100% evidence and attribution coverage, a
fixture-designed 50% READY / 50% EMPTY knowledge split, 100% synthesis success,
zero PIT violations, and zero determinism mismatches.

> **These values come from deterministic synthetic fixtures and validate
> engineering behavior, not real-world financial performance.** They do not
> measure market accuracy, alpha, PnL, Sharpe ratio, or production latency.

## Repository structure

```text
src/quantos/                  Backend schemas, logic, storage, and runtime
scripts/                      Stable local backend and evaluation CLIs
tests/                        Offline unit, integration, and system tests
evaluation/v1-strict-10d/     Audited synthetic evaluation snapshot
ops/systemd/                  Scheduler service examples
docs/                         Architecture, concepts, and quick start
```

## Current status

This repository candidate is based on the internally audited V1 core freeze
reference `46038d95...` and includes the audited offline evaluation harness.
It is a backend release candidate, not a production-ready trading platform.

## Roadmap

- Real-historical replay evaluation using appropriately licensed data
- Packaging and operational documentation hardening
- Optional semantic/vector retrieval research after measured lexical baselines
- Product interface decisions for API, web, and desktop layers

Roadmap items are not implemented V1 capabilities.

## Disclaimer

QuantOS is software for research and engineering. Its outputs are not investment
advice, trading instructions, or guarantees of future performance. Users are
responsible for data rights, validation, risk controls, and regulatory duties.

## License status

QuantOS is licensed under the Apache License 2.0. See [LICENSE](LICENSE).
