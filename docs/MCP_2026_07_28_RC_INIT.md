# MCP 2026-07-28 RC Init

This note kicks off Suitest's migration planning against the MCP release candidate announced at:

- `https://blog.modelcontextprotocol.io/posts/2026-07-28-release-candidate/`

## Why this matters

The release candidate introduces breaking protocol changes around stateless requests, extension negotiation, task lifecycle, deprecations, and schema behavior. Suitest is heavily MCP-centric on both sides:

- it exposes an MCP server for IDE agents
- it dispatches test execution through MCP providers

## Current evidence in this repo

- `packages/lifecycle/src/suitest_lifecycle/mcp_server.py`
  - hardcodes `PROTOCOL_VERSION = "2024-11-05"`
  - implements `initialize`, `tools/list`, `tools/call`, `resources/list`, `resources/templates/list`, `prompts/list`
  - stores client capabilities from the `initialize` handshake
- `packages/lifecycle/src/suitest_lifecycle/sampling.py`
  - issues direct `sampling/createMessage` server-to-client requests
- `docs-site/src/content/docs/docs/guides/llm-setup.md`
  - documents MCP sampling as the first LLM path

## Initial requirement themes

### 1. Protocol baseline migration

Suitest needs an explicit compatibility decision for the MCP server:

- keep current behavior for legacy clients
- add support for `2026-07-28`
- define whether both versions can coexist during rollout

Minimum discovery items:

- remove dependency on protocol session state at the protocol layer
- replace handshake-derived state with per-request metadata handling
- evaluate support for `server/discover`

### 2. Stateless request model

The RC removes the protocol-level session and the `initialize` / `initialized` handshake. Suitest currently depends on the handshake to learn client capabilities such as `sampling`.

Requirements to clarify:

- how client info and capabilities should be read from request `_meta`
- whether any current hidden state must become explicit tool arguments or handles
- whether stdio-only support remains sufficient, or whether remote/HTTP support should be prepared too

### 3. Server-to-client request redesign

The RC changes mid-call input flows toward multi-round-trip request handling. Suitest should assess whether any existing or planned elicitation / confirmation workflows need:

- `InputRequiredResult`
- `requestState`
- `inputResponses`

This is especially relevant if the MCP server will support interactive confirmations beyond today's fire-and-forget tool calls.

### 4. Sampling deprecation

The RC deprecates MCP sampling in favor of direct provider integration. Suitest currently treats sampling as the first LLM tier.

Requirements to clarify:

- whether sampling remains as a compatibility path only
- what direct provider path becomes the preferred replacement
- how docs, fallback order, and telemetry should change

### 5. Tasks extension fit

Suitest has long-running operations such as analyze, generate, run, and publish. The new Tasks extension may be a good fit, but it is no longer the old core task model.

Requirements to clarify:

- which tools should stay synchronous
- which tools should return task handles
- whether task progress/cancel semantics map cleanly to existing runner behavior

### 6. Tool schema and error semantics

The RC upgrades tools to full JSON Schema 2020-12 and changes some error expectations.

Requirements to verify:

- whether Suitest emits schemas compatible with the newer expectations
- whether any schema validation code assumes the older subset
- whether any resource-missing logic still depends on `-32002` instead of `-32602`

### 7. Extensions and future-proofing

The RC formalizes extension negotiation and introduces MCP Apps plus Tasks as first-class extensions.

Requirements to clarify:

- whether Suitest should advertise/consume extensions explicitly
- whether any current capability should move under an extension boundary
- whether server-rendered UI ideas are relevant to Suitest's roadmap

## Suggested next steps

1. Build a gap matrix from RC changes to current Suitest behavior.
2. Separate "must migrate for compatibility" from "nice to adopt now".
3. Decide the target rollout shape:
   - legacy compatibility only
   - dual-stack compatibility
   - clean switch to `2026-07-28`
4. Use the installed `grill-me` skill next turn to pressure-test product and implementation requirements before coding.
