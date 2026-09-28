# QuantOS Product Status — 2026-09

## Released

The current public release is **v0.3.1**. It provides the local Auditable
Financial Research Workspace, offline Synthetic Demo, Ask/Trace, bounded
Analytics with Provenance, Reports, and PIT-aware Replay.

## Unreleased candidate

The `public-sync/phase-8a2-1` branch adds the accepted Public Preview candidate:

- one synthetic-only container running as a non-root user;
- same-origin Workspace and Research API;
- disabled public API documentation routes;
- bounded body size, concurrency, timeout, Ask rate, and ephemeral traces;
- Chinese and English onboarding plus explicit Demo labeling;
- deterministic clean-room release acceptance and operator scripts.

This branch is synchronization and validation work. It is not a published
release and is not deployed over public HTTPS.

## Explicitly unavailable

The candidate has no authentication, accounts, billing, BYOK, multi-tenancy,
real public Provider access, distributed rate limiting, trading execution, or
investment recommendation behavior.
