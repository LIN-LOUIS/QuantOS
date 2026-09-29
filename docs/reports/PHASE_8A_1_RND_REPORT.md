# Phase 8A.1 Public Preview R&D Report

## Outcome

The implementation establishes a single-process, synthetic-only Preview while
leaving local QuantOS and Research Core semantics unchanged.

## Verified boundaries

- Public Preview requires both `--public-preview` and `--demo`.
- Local commands remain loopback-only by default.
- The image builds the Workspace, runs as non-root, and requires no Provider
  credential or real dataset.
- `/docs`, `/redoc`, and `/openapi.json` are unavailable in Preview mode.
- Anonymous requests have bounded body size, concurrency, timeout, Ask rate,
  and ephemeral trace retention.
- The Demo is explicitly labeled synthetic and makes no live Provider call.

## Clean-room evidence

Phase 8A.2 accepted the content snapshot recorded in
`PUBLIC_EXPORT_ACCEPTANCE.json`. The acceptance used fresh Python and frontend
dependencies, a no-cache Docker build, a network-isolated container, full
synthetic product smoke, secret/runtime scans, and content-only export.

## Current publication state

The code is present on the unreleased `public-sync/phase-8a2-1` branch for CI
and diff review. The released version remains `v0.3.1`. No Preview tag,
GitHub Release, or public HTTPS deployment has been created.
