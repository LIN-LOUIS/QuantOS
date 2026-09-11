# Contributing

## Development environment

Use Python 3.10 or newer and an isolated environment:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
python -m pytest -q
```

## Correctness expectations

- Keep financial transformations deterministic Python.
- Require timezone-aware PIT-critical timestamps.
- Never expose data whose `available_at` exceeds the applicable cutoff.
- Filter PIT-ineligible records before ranking statistics.
- Keep Knowledge, Event Evidence, Attribution Evidence, and LLM context distinct.
- Preserve STRICT/RESEARCH and historical/retrospective separation.
- Bind every logical identity to material inputs and exclude observational paths,
  clocks, durations, and host state.
- Preserve append-only, atomic, collision-safe storage behavior.
- Do not weaken validators to make provider output pass.

Any change to PIT, Evidence/Attribution, identity, storage, synthesis cache,
operational manifests, or scheduler semantics requires focused regression tests
and the complete offline test suite.

## Tests and style

Add pytest coverage for public behavior and failure paths. Tests must be local,
deterministic, and free of real provider requests. Before proposing a change, run:

```bash
python -m pytest -q
python -m compileall -q src scripts tests
git diff --check
```

Do not commit credentials, `.env` files, licensed datasets, runtime databases,
cache artifacts, raw provider responses, or personal research material.

## Commits

Keep commits narrowly scoped and state the affected contract. Do not combine
semantic changes with unrelated formatting or packaging work. Include the
regression that closes each correctness bug.
