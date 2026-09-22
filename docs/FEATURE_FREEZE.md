# QuantOS v0.3.0 Product Discovery Feature Freeze

QuantOS v0.3.0 enters **Product Discovery Feature Freeze** when the `v0.3.0`
release is published.

## Changes allowed without additional customer evidence

- P0 security fixes
- P0 data-loss fixes
- P1 crashes
- Release or installation blockers
- Major correctness regressions, including PIT or provenance violations

## Changes requiring evidence

New providers, metrics, dashboards, agents, models, replay modes, and trading
features require at least one of:

- A real user request
- Repeated workflow pain
- A pilot requirement
- A measured activation or retention problem
- A security or correctness requirement

Requests without this evidence go to the post-v0.3 backlog. This freeze does
not weaken the normal test, audit, PIT, credential, or release gates.
