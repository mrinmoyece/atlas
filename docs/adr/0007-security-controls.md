# ADR-0007: Spending money is the privileged action

Status: accepted

## Context
Standard API RBAC models read/write. An agent platform has a third axis that
matters more: every run costs money and takes minutes. A stolen read key is
an information problem; a stolen run key is a financial one.

## Decision
- Roles: `viewer` (run:read), `analyst` (+ run:create), `admin`
  (+ admin:manage). Creating a run is the privileged operation.
- Two independent limits: request rate (token bucket) and **daily spend**,
  the latter checked *before* the run with a pessimistic projection.
- Deny-by-default authentication: no keys configured means every route 401s.
- Keys stored as SHA-256 hashes, compared with `hmac.compare_digest`.
- Hash-chained audit log so deletion or edit is detectable.
- `/metrics` is authenticated because it exposes spend and volume.

## Reasoning
Checking spend after a run tells you the money is already gone. Fixed
windows allow a 2x burst across the boundary, which for a budget means
double the budget — hence token buckets.

## Alternatives considered
- **OIDC/JWT from the start.** Better for real deployments; `Principal`
  carries `subject` and `roles` precisely so that swap touches one function.
- **No spend limit, alert only.** Alerting is detection, not prevention, and
  an agent loop can exhaust a budget faster than a human can respond.

## Consequences
In-process limiting is single-node; behind multiple replicas the effective
limit multiplies by replica count. Documented in LIMITATIONS.md with Redis
as the stated fix.
