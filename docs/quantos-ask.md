# QuantOS Ask

`quantos ask 600519.SH` starts an interactive session. Enter `:q` to leave.
For one answer, use `quantos ask 600519.SH --question "最近一个交易日表现怎么样？"`.
`--as-of-time` accepts an ISO timestamp with a timezone offset; omitting it
samples the current Shanghai time once at the Ask service boundary. Search
collection never advances that cutoff.
`--json` emits the validated result for a single question. `--no-research`
skips Tavily and lets DeepSeek answer from the market fact alone.

Ask resolves the symbol through a PIT-visible SecurityMaster record, reads a
recent completed Tushare daily bar, and searches Tavily once per session. Each
market or evidence question may make one DeepSeek structured request. Search results
are metadata only and carry `strict_pit=false`; they do not prove historical
availability or price causality. A missing market bar stops the answer. A Tavily
failure yields market facts only without an LLM request. Unknown source references, unsupported causal
claims, and certain future price claims are rejected before rendering. Questions
about price causality use persisted Attribution eligibility; without a strict,
eligible result they return a deterministic insufficient-attribution response.
The `--json` result includes the canonical plan lineage and a safe AskTrace.
An explicit historical cutoff requires a PIT-visible historical identity record;
the current live CLI only fetches today's listed SecurityMaster snapshot.

See [Phase 6A.1 canonical contract](phase-6a1-canonical-ask.md) for the
capability, PIT, and trace policies.
See [Phase 6A.2 bounded capabilities](phase-6a2-bounded-capabilities.md) for
capability availability, local adapters, attribution limits, and persistent
trace storage.

Live use requires `TUSHARE_TOKEN`, `TAVILY_API_KEY`, `DEEPSEEK_API_KEY`,
`QUANTOS_LLM_PROVIDER=deepseek`, and an approved `QUANTOS_LLM_MODEL`. Provider
failures return safe reason codes without response bodies or credentials.
