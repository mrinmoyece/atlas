# Security policy

## Reporting a vulnerability

Report privately via GitHub Security Advisories, or email the maintainer.
Please do not open public issues for exploitable problems. Expect
acknowledgement within 72 hours.

## Scope

In scope: authentication and authorisation bypass, path traversal, injection
into tool execution, secret leakage in logs or audit records, audit-chain
forgery, rate/spend limit bypass, container escape from the shipped image.

Out of scope: findings that require an already-compromised API key with the
`admin` role; the deliberately vulnerable code under `fixtures/repos/`,
which exists to be found.

## Threat model

Full model and control mapping: [docs/SECURITY_MODEL.md](docs/SECURITY_MODEL.md).

Summary: Atlas accepts requests from clients that may be compromised,
analyses repositories whose contents are attacker-authored, and spends money
on every run. Controls are grouped as *who is calling* (`security/auth.py`),
*how much they may do* (`security/ratelimit.py`), *what we can prove
afterwards* (`security/audit.py`), and transport hardening
(`security/middleware.py`).

## Known gaps

Documented, not hidden — see the "Known gaps" section of the security model.
The largest is the absence of an external sandbox for arbitrary code
execution; Atlas therefore ships no exec tool.

## Secrets

Configuration is environment-only. API keys are stored as SHA-256 hashes.
Logs and audit records pass through redaction before being written.
