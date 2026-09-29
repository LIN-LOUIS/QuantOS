# Phase 8A.2 Clean-room Release Acceptance

Phase 8A.2 proves that the Public Preview can be reproduced from committed,
public-eligible source without relying on a developer virtual environment,
`node_modules`, built frontend assets, local data, credentials, or runtime
artifacts. It is a release gate and does not publish to the public repository.

## Canonical operator command

From the private source-of-truth checkout, with Docker, Python, Node, and npm
available:

```bash
python scripts/accept_release_candidate.py \
  --repository . \
  --output /tmp/quantos-phase-8a2-acceptance \
  --no-cache
```

When a Python 3.10 interpreter is locally available, add:

```bash
--python310 /path/to/python3.10
```

The default path supports Docker layer cache for faster repeated diagnostics.
The release gate should use `--no-cache` at least once for the accepted
snapshot.

## Isolation and gates

The command:

1. creates a source tree with `git archive HEAD`, so untracked and ignored
   developer files are absent;
2. stages only paths in `public_export_manifest.txt`;
3. validates required files, version metadata, credential-like content, and
   forbidden runtime paths;
4. creates a new Python virtual environment and installs from the staged source;
5. runs the full Python suite, `pip check`, and `compileall`;
6. runs `npm ci`, frontend tests, typecheck, build, and `npm audit`;
7. builds the Public Preview image and optionally disables Docker cache;
8. starts it with `--network none`, waits for Docker health, and exercises the
   synthetic Overview/status, Ask, Trace, Analytics/Provenance, Reports, and
   Replay APIs plus the SPA and disabled documentation routes;
9. checks the runtime UID is non-root and verifies `runtime_mode=DEMO` and
   `deployment_mode=public_preview`;
10. removes the acceptance container in a `finally` cleanup path;
11. computes the canonical public payload digest, excluding only the three
    declared attestation files;
12. writes the candidate acceptance manifest and then reruns the secret,
    runtime-artifact, exact-surface, provenance, and payload-digest checks.

No private Git SHA is written to the public export. The public payload snapshot
ID is a content digest of the intended public payload. It excludes only
`PUBLIC_EXPORT_ACCEPTANCE.json`, `PUBLIC_EXPORT_METADATA.json`, and
`PUBLIC_SOURCE_ACCEPTANCE.json`; these files attest to the payload and are
covered by the complete public Git commit instead of recursively hashing
themselves. The historical policy is
`content-only-export-preserve-public-history-no-private-ancestry`.

## PASS / FAIL

`PASS` is emitted only when every mandatory gate passes. A failure exits
non-zero and prints a small JSON object with:

- `stage`;
- safe `reason`;
- `remediation` hint.

There is no partial acceptance and no flag that silently skips Docker,
security, health, or test gates.

## Output

For `--output /tmp/quantos-phase-8a2-acceptance`:

- public staging: `/tmp/quantos-phase-8a2-acceptance/public-export`;
- public manifest:
  `/tmp/quantos-phase-8a2-acceptance/public-export/PUBLIC_EXPORT_ACCEPTANCE.json`;
- duplicate operator result:
  `/tmp/quantos-phase-8a2-acceptance/acceptance-result.json`;
- gate logs: `/tmp/quantos-phase-8a2-acceptance/logs`.

`PUBLIC_SOURCE_ACCEPTANCE.json` retains the independently accepted source
evidence under schema `quantos-clean-room-acceptance-v1`.
`PUBLIC_EXPORT_ACCEPTANCE.json` uses schema
`quantos-clean-room-acceptance-v2` and records both the accepted source ID and
the independently recomputed public payload ID, plus current public-candidate
test counts and gate results. It intentionally has no volatile timestamp or
private repository commit.

## Troubleshooting

| Stage | Typical cause | Remediation |
| --- | --- | --- |
| `SOURCE_SNAPSHOT` | invalid Git checkout | Commit intended files and rerun from the private source repository. |
| `REQUIRED_FILES` | incomplete release surface | Restore the file or update the explicit allowlist with review. |
| `SECRET_SCAN` | credential-like literal | Remove the credential; do not weaken the scanner for real values. |
| `RUNTIME_ARTIFACT_SCAN` | local data/build output | Remove it from tracked public content and regenerate. |
| `PYTHON_*` | install or offline test failure | Inspect the corresponding log and fix the deterministic failure. |
| `FRONTEND_*` / `NPM_*` | lockfile, test, type, build, or audit failure | Repair the frontend gate; do not bypass it. |
| `DOCKER_BUILD` | daemon/build failure | Start Docker and inspect `logs/docker-build.log`. |
| `CONTAINER_HEALTH` | server did not become healthy | Inspect container startup and `/v1/health`. |
| `E2E_DEMO` | product surface regression | Repair the existing synthetic Demo or API boundary. |
| `EXPORT_DIFF` | staging differs from allowlist | Review the intended public surface; fail closed. |
| `PAYLOAD_DIGEST` | final payload differs from the recorded digest | Restore the canonical payload or regenerate all candidate attestations. |
| `PROVENANCE` / `ATTESTATION` | source and candidate identities are missing, conflated, or stale | Keep source evidence separate and regenerate the candidate manifest. |

Publication remains a later Phase 8A.2.1 controlled public-sync operation.
