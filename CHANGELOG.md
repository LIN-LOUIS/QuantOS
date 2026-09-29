# Changelog

All notable public-backend changes will be documented here.

## Unreleased — Public Preview candidate

- Added the synthetic-only, single-instance Public Preview container.
- Added request size, concurrency, timeout, and Ask rate boundaries.
- Added bounded ephemeral AskTrace retention without user identity collection.
- Added Chinese/English Preview onboarding and truthful capability explanations.
- Added safe Preview build, start, stop, status, and log operator commands.
- Added deterministic clean-room acceptance and content-only public export.
- Expanded CI with frontend and containerized Public Preview release gates.

The released version remains `v0.3.1`. No Preview tag, GitHub Release, public
HTTPS deployment, authentication, billing, real Provider connectivity, or
trading capability is included by this synchronization branch.

## Unreleased — 0.1.0 public backend candidate

Historical note: this section described the original pre-release 0.1.0
candidate before the later `v0.3.0` and `v0.3.1` public releases.

### Included

#### Real-World Command Wrappers V1

- Added `quantos status`.
- Added unified `quantos report daily`.
- Added `quantos report pre-open`.
- Added `quantos report post-close`.
- Added `quantos scheduler once`.
- Existing script entry points remain supported.
- Real report commands fail closed when required local artifacts are unavailable.

#### User Command Layer V1

- Added installed `quantos` console entry point.
- Added offline read-only `quantos doctor`.
- Added provider-free deterministic `quantos demo`.
- Preserved existing health and advanced backend CLIs.

- Deterministic, PIT-aware market-data and candidate foundations
- Evidence calibration and attribution eligibility
- Immutable local Knowledge documents, deterministic chunking, and lexical retrieval
- Historical/retrospective Context assembly with STRICT/RESEARCH separation
- Guarded synthesis, validated cache identity, collision safety, and atomic publication
- Knowledge-aware Daily Intelligence v1/v2 and TimeSlice products
- Operational readiness, versioned run manifests, and failure observability
- Scheduler windows, claims, leases, fencing, retry, and stale takeover
- Audited 10-day synthetic offline evaluation harness and example snapshot

This candidate is not a production-ready trading platform and does not include
real-historical performance validation, order execution, a public HTTP API, or UI.
# v0.3.0

- Added the local QuantOS Research Workspace and one-command offline Demo Mode.
- Added grounded Ask with structured trace inspection.
- Added bounded semantic analytics with charts, tables, and provenance.
- Added explicit availability semantics and dual historical replay semantics.
- Added deterministic release packaging, startup health checks, and safe local
  port fallback.

# v0.3.1

- Fixed Python 3.10 TOML parsing with a conditional `tomli` compatibility
  dependency while retaining the standard library on Python 3.11 and newer.
- Fixed market-date filtering so UTC CI runners preserve Asia/Shanghai trading
  dates.
- Made the public README Chinese-first and retained a complete English README.
- Kept the Python 3.10, 3.11, and 3.12 CI matrix with independent job results.
- Added no research, data-provider, replay, or trading capabilities.
