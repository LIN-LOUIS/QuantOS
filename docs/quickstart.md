# Quick start

[English](quickstart.md) | [简体中文](quickstart.zh-CN.md)

## Requirements

- Python 3.10 or newer
- Git for cloning
- A POSIX-style environment is recommended for the current cache-locking runtime

## Install from source

```bash
git clone https://github.com/LIN-LOUIS/QuantOS.git
cd QuantOS
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

QuantOS is not currently published on PyPI.

## First run

```bash
quantos --version
quantos doctor
quantos demo
```

`quantos doctor` is an offline, read-only installation check. `quantos demo`
uses `SYNTHETIC_FIXTURE` and is **NOT REAL-HISTORICAL PERFORMANCE**. The demo
needs no API key and contacts no real LLM or market/news provider.

## Verify the backend

```bash
python -m pytest -q
python -m compileall -q src scripts
```

The test suite uses local fakes and fixtures. It does not require provider
credentials or make a real LLM request.

## Run the synthetic evaluation

Use a new output directory; evaluation sessions are immutable.

```bash
python scripts/evaluate_v1.py --help
python scripts/evaluate_v1.py \
  --output-dir /tmp/quantos-v1-evaluation \
  --trading-days 10
```

The command runs a deterministic `SYNTHETIC_FIXTURE` benchmark. It is an
engineering validation, not a real-historical or investment-performance test.

## Inspect operational CLIs

```bash
quantos health --help
python -m quantos --help
python scripts/run_quantos.py --help
python scripts/generate_daily_report.py --help
python scripts/generate_time_slice_report.py --help
python scripts/run_scheduler_once.py --help
python scripts/evaluate_v1.py --help
```

All timestamp arguments must include a timezone offset. For example:

```bash
python scripts/run_quantos.py \
  --trade-date 2026-09-10 \
  --as-of-time 2026-09-10T18:00:00+08:00 \
  --run-type POST_CLOSE \
  --mode strict_live \
  --top-n 20 \
  --no-llm
```

This operational command consumes existing canonical local artifacts. It may
fail closed with structured readiness/failure information when those artifacts
are absent, stale, corrupt, or incompatible. It does not download or fabricate
a real market product.

Real market reports require provider/configuration, canonical local artifacts,
and corresponding Evidence/Knowledge preparation. Cloning the repository alone
does not generate a real A-share report. Daily generation requires prepared
local market/evidence artifacts:

```bash
python scripts/generate_daily_report.py --help
```

Scheduler execution requires a valid local calendar artifact and runtime DB.
Inspect the exact options before supplying either:

```bash
python scripts/run_scheduler_once.py --help
```

## Optional provider configuration

Provider-capable code reads configuration from environment variables. Do not
commit their values. The synthetic evaluation and CI do not need them. Relevant
names include `DEEPSEEK_API_KEY`, `TUSHARE_TOKEN`, `TAVILY_API_KEY`,
`EXA_API_KEY`, and the `QUANTOS_LLM_*` / `QUANTOS_KNOWLEDGE_*` settings.

No `.env.example` is included yet because the default public verification path
is fully local and provider-free.
