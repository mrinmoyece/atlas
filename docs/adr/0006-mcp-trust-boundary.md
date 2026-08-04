# ADR-0006: Remote MCP servers are untrusted input, not trusted extensions

Status: accepted

## Context
MCP lets an agent consume tools it does not own. The tools describe
themselves, and those descriptions are placed verbatim into a model's
prompt.

## Decision
Treat every remote MCP server as hostile-by-default:
allowlist by server *and* tool name; sanitise descriptions for
injection-shaped content and log when sanitisation fires; namespace tools as
`server::tool`; cap result sizes; fail closed on unknown names without ever
reaching the network.

## Reasoning
A tool description is the cheapest prompt-injection vector in the ecosystem:
it is attacker-authored, high-trust by convention, and lands in the system
context. Namespacing additionally prevents a remote server from shadowing a
local tool name and hijacking calls meant for the confined toolkit.

## Alternatives considered
- **Trust discovered tools.** The default in most MCP client code, and the
  reason this ADR exists.
- **Human approval per tool.** Stronger, and appropriate for destructive
  tools; overkill for read-only ones and unusable at discovery scale.

## Consequences
Adding a remote tool requires an explicit configuration change. That
friction is the control.
