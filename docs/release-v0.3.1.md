# QuantOS v0.3.1 — Python 3.10 Compatibility Maintenance

QuantOS v0.3.1 is a focused maintenance release for the public Auditable
Financial Research Workspace. It adds no research, provider, replay, dashboard,
agent, prediction, or trading capabilities.

## Fixed

- Restored Python 3.10 compatibility for release metadata and bundle creation.
- Added `tomli` only on Python versions earlier than 3.11; Python 3.11 and newer
  continue to use the standard-library `tomllib`.
- Kept public CI coverage for Python 3.10, 3.11, and 3.12, with independent
  matrix results.

## Documentation

- `README.md` is now the Chinese-first product homepage.
- `README.en.md` retains the complete English product, installation, safety,
  and development documentation.

## Product scope

The existing v0.3 product remains unchanged: grounded Ask, structured Trace,
bounded Analytics, Provenance, Reports, PIT Replay, explicit Availability, and
the offline deterministic Demo Mode.

## Limitations

- QuantOS is local-first and has no production authentication yet.
- Demo data is synthetic and visibly labeled.
- Strict operational replay history accumulates from real observations.
- Historical Evidence and Knowledge coverage depends on available persisted data.
- Provider availability and rate limits vary.
- QuantOS is for research and engineering; it is not investment advice.

QuantOS remains under the v0.3 Product Discovery Feature Freeze.
