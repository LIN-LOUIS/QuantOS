# QuantOS Public Preview Deployment

Phase 8A.1 defines a bounded, single-instance deployment for the synthetic
QuantOS Demo. It is a product preview, not a production financial-data service.

## Modes

QuantOS keeps data mode and deployment mode separate:

| Concern | Local default | Public preview |
|---|---|---|
| Deployment mode | `local` | `public_preview` |
| Data mode | `LOCAL` or explicit `DEMO` | `DEMO` only |
| Default binding | `127.0.0.1` | explicit container binding to `0.0.0.0` |
| Providers | unchanged local behavior | never initialized |
| OpenAPI, Swagger, ReDoc | available | disabled |
| AskTrace | existing persistent repository | temporary, one-hour TTL, newest 200 |
| Request body | existing local behavior | 64 KiB maximum |
| Concurrent protected API requests | existing local behavior | 8 per process |
| Request timeout | existing local behavior | 15 seconds |
| Ask rate | existing local behavior | 12 requests per rolling 60 seconds |

The rate and concurrency counters are process-local. This is deliberate: the
preview supports one process and one replica and does not introduce Redis,
accounts, API keys, billing, or distributed coordination.

## Build and run

Build from an audited source checkout. `web/dist` is produced inside the image
and remains ignored by Git.

Routine operation uses scripts that target only the labeled QuantOS Preview
container:

```bash
scripts/preview-build.sh
scripts/preview-up.sh
scripts/preview-status.sh
scripts/preview-logs.sh --follow
scripts/preview-down.sh
```

An exported source snapshot has no private `.git` directory. In that case the
build identity must be supplied explicitly:

```bash
QUANTOS_BUILD_COMMIT="public-snapshot-sha256:<digest>" scripts/preview-build.sh
```

The default host binding is `127.0.0.1:8080`. `preview-up.sh` waits for
`/v1/health`, builds the image when absent, and is safe to run repeatedly. It
will not remove an unlabeled same-name container. LAN access requires the
explicit `scripts/preview-up.sh --lan` flag and prints a warning plus detected
LAN URLs. Set `QUANTOS_PREVIEW_PORT` to override the host port.

`preview-build.sh` records the current source identity with:

```bash
--build-arg QUANTOS_BUILD_COMMIT="$(git rev-parse HEAD)"
```

The equivalent raw Docker workflow is:

```bash
docker build \
  --build-arg QUANTOS_BUILD_COMMIT="$(git rev-parse HEAD)" \
  -t quantos-public-preview:local .

docker run --rm \
  --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=128m \
  -p 127.0.0.1:8080:8000 \
  quantos-public-preview:local
```

The image starts:

```text
quantos start --demo --public-preview --host 0.0.0.0 --port $PORT --no-browser
```

No provider credential or data volume is required. `/v1/health` is the
container health endpoint. The Workspace and API remain same-origin, so CORS is
not enabled.

## Deployment architecture

```text
Internet
   ↓
Managed TLS reverse proxy / platform ingress
   ↓
One QuantOS Public Preview container
   ├── built Workspace static assets
   ├── /v1 Research API
   └── temporary synthetic Demo workspace
```

The ingress should add its own connection and bandwidth limits. QuantOS still
enforces application-level body, concurrency, timeout, and Ask-rate bounds.

## Security boundary

The Docker context excludes `.env`, credentials, `data/`, databases, Parquet,
provider payloads, local release artifacts, caches, `node_modules`, and
developer directories. The runtime image runs as the unprivileged `quantos`
user. Demo startup creates only a temporary fixture and never initializes
Tushare, BaoStock, Tavily, DeepSeek, or another external provider.

Public preview mode intentionally does not add authentication, user identity,
production providers, a real LLM, multi-tenancy, billing, or trading behavior.

## Operational limits

- One replica only; request counters are not distributed.
- Ask traces disappear on restart and may expire while a browser remains open.
- The service is an anonymous product demonstration and must not receive PII.
- TLS, DNS, platform-level DDoS controls, logs, and container orchestration are
  the responsibility of the selected hosting platform.
- Local `quantos start` and `quantos serve` remain loopback-only by default.
