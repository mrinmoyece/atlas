# Security model

## What makes an agent platform different

Three properties that ordinary services do not have, and that drive every
control below:

1. **Every request spends money.** A compromised credential is a financial
   incident, not only a data one.
2. **The system reads attacker-authored content.** Repository files, tool
   outputs and remote tool descriptions are all untrusted input that lands
   in a model's context.
3. **The model chooses actions.** Tool selection is influenced by content
   the attacker may control, so tool permissions cannot be decided by the
   model.

## Trust boundaries

```
                    ┌─ untrusted ──────────────────────────────┐
  API client ──▶ Atlas ──▶ model ──▶ tool selection            │
       │            │        ▲                │                │
   (authn/z,        │        │                ▼                │
    rate, spend)    │        └──── repository content ◀────────┤
                    │                 remote MCP descriptions ─┘
                    └─ trusted: our code, our tool implementations
```

## Controls

### Identity and access
| Control | Implementation |
|---|---|
| Deny by default | No keys configured → every authenticated route 401s. Never "open mode". |
| Key storage | SHA-256 hashes only; raw keys never stored or logged. |
| Comparison | `hmac.compare_digest` against all entries — no early exit to time. |
| RBAC | `viewer` / `analyst` / `admin`; **run creation is privileged** (ADR-0007). |
| Enumeration resistance | Generic failure message; failure reason never disclosed. |

Role grants are cumulative and fixed in code:

| Role | `run:read` | `run:create` | `admin:manage` | Typical use |
|---|---:|---:|---:|---|
| `viewer` | yes | no | no | Read memory status and authenticated metrics |
| `analyst` | yes | yes | no | Submit and stream analyses, which may spend money |
| `admin` | yes | yes | yes | Read audit data and perform administrative operations |

There is no anonymous fallback. `/healthz` is the only public operational
endpoint; `/metrics` requires at least `viewer`.

### Abuse and cost
| Control | Implementation |
|---|---|
| Request rate | Per-principal token bucket (no fixed-window 2x burst). |
| Spend budget | Daily per-principal cap, checked **before** the run with a pessimistic projection. |
| Step budget | Per-specialist max steps enforced by the runtime, halting tool loops. |
| Context budget | Compaction bounds tokens per call. |

Configuration invariant: `ATLAS_DAILY_SPEND_USD` must be greater than or equal
to `ATLAS_MAX_COST_USD`. Atlas reserves the full per-run ceiling before work
starts, then reconciles actual cost. A daily cap below one possible reservation
would reject every run, not create a useful smaller cap.

### Untrusted content
| Vector | Control |
|---|---|
| Repository files | Prompt injection guard in every specialist prompt; tool outputs capped; findings must cite verifiable locations, and citations are checked against the filesystem. |
| Remote MCP tool descriptions | Allowlist, injection-pattern sanitisation (logged), `server::tool` namespacing, result caps. |
| A2A agent cards | Untrusted cards sanitised on registration. |
| Model output | Strict validation: findings lacking severity, title or location are **dropped**, never defaulted. |

### Tool safety
| Control | Implementation |
|---|---|
| Path confinement | Every path resolved and verified inside the repo root; traversal rejected. |
| Output bounds | Per-tool caps; a 40MB lockfile cannot evict the context or blow the budget. |
| Error containment | Tool exceptions become observations; a tool bug cannot crash the run. |
| No shell, no eval | There is no generic exec tool. Adding one requires an external sandbox — see LIMITATIONS.md. |

### Transport and platform
| Control | Implementation |
|---|---|
| Security headers | CSP (`default-src 'none'`), HSTS, nosniff, frame-deny, referrer, permissions policy, COOP/CORP. |
| CORS | Explicit origin allowlist; never `*`; credentials disabled. |
| Body limits | Pure-ASGI byte counting (not `Content-Length` alone, which chunked requests can lie about). |
| Input validation | `extra="forbid"`, pattern/enum validation, traversal-shaped repo names rejected before path resolution. |
| Error handling | Unhandled exceptions → generic 500 + request id. No stack traces on the wire. |
| Container | Non-root uid 10001, read-only rootfs, all capabilities dropped, `no-new-privileges`, seccomp RuntimeDefault. |
| Network | Egress NetworkPolicy denies by default and **excludes 169.254.169.254** — the cloud metadata endpoint, the classic SSRF target. |

### Auditability
| Control | Implementation |
|---|---|
| Audit log | Every privileged action: actor, action, resource, outcome, cost. The in-memory tail is bounded. |
| Tamper evidence | Hash-chained entries; `verify()` detects edits and deletions. |
| Secret hygiene | Redaction at the logging boundary and per-value in audit details, so no call site can forget. |
| Metrics access | `/metrics` requires authentication — it exposes spend and volume. |

The default audit store is a bounded in-memory tail (10,000 entries by
default). `ATLAS_AUDIT_SINK` optionally appends JSONL and restores/verifies it
at startup, but a file on the same host is neither highly available nor
tamper-proof. A production extension should ship records and chain-head
anchors off-box to access-controlled WORM storage.

### Supply chain
| Control | Implementation |
|---|---|
| Dependency audit | `pip-audit` in CI. |
| SBOM | CycloneDX Python dependency SBOM generated and uploaded per build. |
| Static analysis | CodeQL (`security-and-quality`) on PRs and weekly. |
| Updates | Dependabot for pip, GitHub Actions and Docker. |
| Container check | CI asserts the image does not run as uid 0. |

The current SBOM is dependency-focused. Useful next steps are to attach it to
the released image with provenance, inventory the base image and system
packages, and scan the actual runtime image rather than treating a Python
lock/dependency inventory as a complete runtime SBOM.

## Known gaps (deliberate, not overlooked)

1. **No external code-execution sandbox.** In-process guards protect
   well-typed tools; they do not contain arbitrary code. A `run_python` tool
   would require a container or microVM boundary.
2. **In-process rate limiting.** Behind N replicas the effective limit is
   N× the configured one. Redis is the fix.
3. **API keys, not OIDC.** `Principal` is shaped for a JWT swap; that swap
   has not been made.
4. **No tenancy isolation.** Memory and audit are global. Multi-tenant
   deployment needs a tenant dimension throughout.
5. **Prompt injection is contained, not solved.** Structural controls bound
   the blast radius (tool allowlists, budgets, validation); they cannot stop
   an injected instruction from influencing *content*. Report consumers
   should treat findings as leads with evidence, not verdicts.
6. **Single-instance deployment.** Checkpoints, memory, limits and the audit
   tail are local. PostgreSQL, Redis, OIDC/JWKS, tenant isolation, managed
   secrets, off-box WORM audit and multi-region HA are roadmap extension
   points, not features of this repository.

## Reporting

See [SECURITY.md](../SECURITY.md) for disclosure. Please do not open public
issues for exploitable problems.
