# Security policy

## Reporting a vulnerability

A dedicated public security contact has not yet been selected. Until one is
published, do not place API keys, credentials, proprietary financial data, or
other sensitive material in a public issue. Mark the report for private security
handling through the repository owner's available private contact channel.

Security contact: **TBD before public release**.

## Relevant security boundaries

Reports are especially useful when they concern:

- point-in-time or future-information bypasses;
- evidence/attribution boundary violations;
- logical identity collisions or underbinding;
- cache collision, validation, or atomicity failures;
- artifact hash, schema, reference, or JSON/Markdown-pair bypasses;
- path traversal or unsafe filesystem publication;
- secret, raw provider response, or traceback leakage;
- scheduler lease or fencing integrity.

Please provide a minimal reproduction and safe identifiers. Redact credentials,
provider payloads, personal information, licensed data, and absolute private paths.

## Scope note

This repository is a backend release candidate. Unsupported roadmap features are
not security guarantees. Financial outputs are not investment advice or trading
instructions.
