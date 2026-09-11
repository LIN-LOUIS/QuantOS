# Core concepts

[English](concepts.md) | [简体中文](concepts.zh-CN.md)

- **Event time:** the market event being interpreted.
- **Availability time:** when information became knowable to QuantOS.
- **Run cutoff:** the explicit operational `as_of_time`; it never rewrites event time.
- **Evidence:** an event record that may become attribution-eligible.
- **Knowledge:** background material retrieved from an exact local corpus/index.
- **Historical lane:** knowledge available by the market-event cutoff.
- **Retrospective lane:** RESEARCH-only knowledge available after the event but by
  an explicit research cutoff.
- **Guarded synthesis:** bounded prose generation whose references, causal status,
  schema, and input identity are deterministically validated.
- **Product identity:** the logical identity of Daily or TimeSlice content.
- **Artifact identity:** the SHA-256 identity of persisted bytes.
- **Operational state identity:** the identity of the observed product state.
- **Run identity:** execution intent and context, not product content.
