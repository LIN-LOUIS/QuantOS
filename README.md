# QuantOS

[English](README.en.md) | [简体中文](README.md)

**Auditable Financial Research Workspace for Chinese A-shares.** QuantOS joins
grounded Ask, bounded analytics, provenance, traces, and PIT-safe historical
replay in one local workspace. Python computes facts and enforces temporal
policy; language models may only explain validated structured context.

## Quick Demo

Build the Workspace once, then launch the complete offline product with one
command:

```bash
python -m pip install -e '.[dev]'
cd web && npm ci && npm run build && cd ..
quantos doctor --project-root .
quantos start --demo
```

The launcher binds only to loopback, chooses another port if the default 8000
is occupied, waits for `/v1/health`, and then opens the browser. Demo Mode is
clearly marked as synthetic fixture data and never reads provider credentials
or uses the network. Use `--no-browser` in headless environments.

The core workflow is **Observe → Ask → Analyze → Inspect Provenance → Inspect
Replay**. See the [60–90 second demo script](docs/demo-script.md). Product
screenshots below were captured from the deterministic Demo Mode.

### Product preview

![QuantOS Demo Mode overview](docs/screenshots/v0.3.0-overview.jpg)

![Grounded Ask with structured trace](docs/screenshots/v0.3.0-ask-trace.jpg)

## From Fresh Clone to Real Data

Install the package in an isolated Python environment, configure credentials
outside the repository, then inspect and bootstrap the bounded local datasets:

```bash
python -m pip install -e '.[dev]'
quantos doctor --project-root .
quantos data bootstrap security-master --provider tushare --project-root .
quantos data bootstrap market --provider tushare \
  --symbol 600519.SH --trading-days 5 --project-root .
quantos status --project-root .
quantos ask 600519.SH --question "最近一个交易日表现怎么样？"
```

Tushare commands require `TUSHARE_TOKEN` in the process environment. QuantOS
stores only whether configuration is present; it does not persist credential
values. BaoStock can bootstrap a Security Master without a token:

```bash
quantos data bootstrap security-master --provider baostock --project-root .
```

Bootstrap is bounded, append-only, idempotent, and audited under the local
`data/` tree. `quantos data refresh security-master` appends a changed provider
observation while preserving earlier snapshots. Normal `quantos ask` reads the
persistent Security Master and local market storage; it never initializes
identity by making a live `stock_basic` request.

See the public [architecture](docs/architecture.md) and
[core concepts](docs/concepts.md) for provider, PIT, lineage, and historical
coverage details.

## Historical Replay

Historical import is explicit and separate from offline replay:

```bash
quantos replay import-market --provider tushare --symbol 600519.SH \
  --start 2026-06-01 --end 2026-06-30
quantos replay run --dataset-id DATASET_ID --symbol 600519.SH \
  --start 2026-06-01 --end 2026-06-30
quantos replay show CAMPAIGN_ID
quantos replay failures CAMPAIGN_ID
```

Replay reads only successful historical dataset manifests and PIT-visible
Security Master snapshots. It never downloads missing data automatically. See
the public [core concepts](docs/concepts.md).

Strict operational identity remains the default. Retrospective reconstruction
requires an explicitly derived authority artifact and mode:

```bash
quantos replay derive-identity --snapshot-id SNAPSHOT_ID
quantos replay run --mode retrospective-reconstructed \
  --identity-authority-id AUTHORITY_ID --dataset-id DATASET_ID \
  --symbol 600519.SH --start 2026-06-01 --end 2026-06-30
```

Strict and retrospective replay semantics remain explicit in every result.

## Local Research API

Install development dependencies and start the read-only API on the loopback
interface:

```bash
python -m pip install -e '.[dev]'
quantos serve --project-root .
curl -s http://127.0.0.1:8000/v1/health
curl -s http://127.0.0.1:8000/v1/status
```

Run bounded semantic analytics:

```bash
curl -s http://127.0.0.1:8000/v1/analytics/query \
  -H 'content-type: application/json' \
  -d '{"metrics":["close"],"dimensions":["trading_date"],
       "entities":["600519.SH"],
       "time_range":{"start":"2026-08-20","end":"2026-09-18"},
       "filters":[],"sort":[],"limit":20,
       "as_of_time":"2026-09-18T18:00:00+08:00"}'
```

Ask through the canonical offline application service, then inspect the
returned `trace_id`:

```bash
curl -s http://127.0.0.1:8000/v1/ask \
  -H 'content-type: application/json' \
  -d '{"symbol":"600519.SH","question":"最近一个交易日表现怎么样？",
       "no_research":true}'
curl -s http://127.0.0.1:8000/v1/traces/TRACE_ID
```

The server accepts loopback hosts only. API endpoints never bootstrap or
refresh data and never call a provider. See the public
[architecture](docs/architecture.md) for availability, provenance, and
security boundaries.

## Local Research Workspace

For the integrated product path, build assets and run one launcher:

```bash
cd web && npm ci && npm run build && cd ..
quantos start --project-root .
```

This serves the built Workspace and Research API from one loopback origin. It
does not bootstrap, refresh, or contact providers. A fresh project with no
local data starts safely and explains how to use Demo Mode or bootstrap data.

Developers can still start the API and Vite separately:

```bash
quantos serve --project-root .
cd web
npm install
npm run dev
```

Open `http://127.0.0.1:5173`. The browser uses relative `/v1` requests through
the Vite proxy; it never reads QuantOS storage or contacts providers directly.

The product default API target remains `http://127.0.0.1:8000`. If that port is
already occupied in a local development environment, run QuantOS on another
loopback port and explicitly override only the Vite proxy target:

```bash
quantos serve --project-root . --host 127.0.0.1 --port 8010
cd web
QUANTOS_API_TARGET=http://127.0.0.1:8010 npm run dev
```

Validate the frontend with `npm test`, `npm run typecheck`, and
`npm run build`. See the public [quickstart](docs/quickstart.md) and
[architecture](docs/architecture.md) for development and security boundaries.
