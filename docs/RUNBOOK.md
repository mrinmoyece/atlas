# Runbook

## Deploy

### Local container

```bash
make up
# Optional authenticated Prometheus:
docker compose --profile metrics up --build
```

Compose binds to loopback, uses the offline scripted provider and persists the
audit JSONL in a named volume. Its documented `dev-analyst-key` is local-only.
Use `make down` to stop containers without deleting audit data; `make clean`
also deletes the volume. Override occupied host ports with `ATLAS_PORT` and
`PROMETHEUS_PORT`.

### Kubernetes template

The manifest references `atlas:0.1.0`; build and publish that image to a
registry your cluster can pull from, then replace the image reference. Create
secrets from the template before applying the workload:

```bash
cp k8s/secrets.example.yaml k8s/secrets.local.yaml
# Replace every placeholder; never commit secrets.local.yaml.
kubectl apply -f k8s/secrets.local.yaml
kubectl apply -f k8s/deployment.yaml

# Optional; requires Prometheus Operator CRDs/controller.
kubectl apply -f k8s/servicemonitor.yaml
```

Required configuration:

| Variable | Purpose | Notes |
|---|---|---|
| `ATLAS_API_KEYS` | `key_id:sha256:roles;...` | **Without this every route 401s.** By design. |
| `ATLAS_PROVIDER` | `scripted` or `anthropic` | `scripted` runs offline |
| `ATLAS_ANTHROPIC_API_KEY` | model access | secret, never logged |
| `ATLAS_OTEL_ENDPOINT` | trace export | unset → tracing is a no-op |
| `ATLAS_MAX_COST_USD` | per-run ceiling | |

Generate a key hash:
```bash
python -c "from atlas.security import hash_key; print(hash_key('your-key'))"
```

### Deployment validation checklist

Before admitting traffic:

- [ ] Run `make gate` (the aggregate local gate: lint, unit tests, standard
      eval quality gate and performance budgets); do not infer quality from a
      healthy container.
- [ ] Configure `ATLAS_API_KEYS` with separate least-privilege principals and
      verify no raw key appears in config, logs or manifests.
- [ ] Keep one replica. The local checkpoint, memory, rate/spend ledger and
      audit chain are not safe to split across replicas.
- [ ] Ensure `ATLAS_DAILY_SPEND_USD >= ATLAS_MAX_COST_USD`. A run reserves its
      complete per-run ceiling; violating this invariant makes every analysis
      fail at startup/authorisation rather than providing a smaller daily cap.
- [ ] Set provider credentials through a secret store, verify outbound policy,
      and confirm the cloud metadata endpoint remains unreachable.
- [ ] Mount durable storage and set `ATLAS_AUDIT_SINK` if local JSONL retention
      is required; verify permissions, restart recovery and chain validation.
- [ ] Probe public `/healthz`, then authenticate `/metrics`, a read request, an
      analysis and an admin-only audit request with the expected roles. Also
      confirm missing, viewer and invalid credentials fail as expected.
- [ ] Exercise one SSE analysis through the real ingress and confirm events
      arrive incrementally; test request-size and 429 behaviour.
- [ ] Confirm alerts, trace export, backups/restore procedure and image/SBOM
      provenance before declaring the instance operational.

## Monitoring

`/healthz` is public; `/metrics` requires `run:read` because it exposes
spend. Scrape it with `Authorization: Bearer <raw-key>` from a dedicated
`viewer` principal; Atlas stores/configures only that key's SHA-256 hash. Do
not put the raw scrape key in a checked-in `ServiceMonitor`.

The shipped Kubernetes YAML does **not** install Prometheus or the Prometheus
Operator. The optional `k8s/servicemonitor.yaml` requires their CRDs/controller
and reads a viewer key from `atlas-secrets`; the checked-in secret file contains
placeholders only. Without the Operator, configure the same bearer header in
your chosen Prometheus scrape job.

Signals worth alerting on:

| Signal | Meaning | Action |
|---|---|---|
| `increase(atlas_cost_usd_total[1h])` above budget | spend runaway | check for a looping client; tighten daily cap |
| `rate(atlas_specialist_runs_total{outcome="error"}[10m])` | agents failing | check provider status, then logs by category |
| `rate(atlas_auth_total{outcome="denied"}[5m])` | credential probing | rotate keys, check source IPs |
| `rate(atlas_rate_limited_total[5m])` | abuse or stuck client | identify principal from audit log |
| `histogram_quantile(0.95, atlas_run_duration_ms_bucket)` | latency regression | compare with recent deploys |
| `no_api_keys_configured` log line | misconfigured deploy | set `ATLAS_API_KEYS` |

Traces: every graph node, model call and tool call is a span. Parallel
specialists appear as concurrent spans — if they appear sequential, fan-out
is broken.

## Incidents

### "Reports are empty or findings dropped"
1. Check `atlas_findings_total` and the `dropped_findings` count in logs.
2. Run `make evals` — if the gate fails locally, it is the agent, not the
   deployment.
3. If evals pass locally but production is empty, suspect the provider:
   check `atlas_specialist_runs_total{outcome="error"}`.

### "Spend is climbing"
1. `GET /v1/audit` (admin) — identify the actor and repo.
2. Lower `daily_spend_usd` or revoke the key by removing it from
   `ATLAS_API_KEYS` and restarting.
3. Hard stop: set `ATLAS_PROVIDER=scripted` and redeploy. Runs continue and
   cost nothing while you investigate.

### "Did agent X really report that?"
`GET /v1/audit` gives actor, action, resource, findings count and cost per
run, hash-chained. `chain_valid: false` means records were altered or
removed — treat as an incident in itself.

### "A quality regression shipped"
Eval gate is a required check, so this means the gate was changed. Review
the PR that edited `GATES` in `src/atlas/evals/runner.py`; the CHANGELOG and
PR description must justify any threshold move.

### Suspected prompt injection

Examples include a repository instruction being echoed as policy, unexpected
tool selection, attempts to suppress findings, or output referring to secrets
or unrelated repositories.

1. Stop new analyses for the affected principal/repository; revoke the key if
   compromise is plausible. Do not execute or blindly paste the suspect text.
2. Preserve the request id, report, evidence locations, audit JSONL, redacted
   logs, traces, model/provider metadata and repository commit. Record hashes
   and times; never copy raw credentials or unrestricted prompts into tickets.
3. Verify the audit chain and inspect bounded tool calls and allowlist
   decisions. Determine whether this was content influence only or whether an
   auth, confinement, secret or cross-tenant boundary was crossed.
4. Reproduce offline with the scripted provider or a cost-capped isolated
   account and a minimal repository. Treat all repository files, MCP output
   and A2A cards as hostile.
5. Rotate exposed credentials, remove untrusted MCP endpoints, quarantine the
   repository/version, and lower spend/rate ceilings as containment requires.
6. Add a regression/eval case before restoring service. If a code boundary
   was crossed, use the private process in `SECURITY.md`; do not disclose the
   payload publicly before remediation.

### Provider outage
Specialists fail individually, runs complete partially with errors named in
the verdict. No corruption. Re-run affected repositories after recovery.

## Rollback

The service is stateless apart from in-process checkpointing, memory, limits
and the bounded audit tail. Rolling back is a normal single-image rollback.
Those in-memory states are lost on restart. An optional JSONL audit sink can
survive on a durable mounted volume, but memory and checkpoints do not; see
LIMITATIONS.md.

## Example service objectives (reference deployment only)

These are starting points for a **single-instance CV/reference deployment**,
not measured guarantees or enterprise commitments:

| Objective | Example target | Scope and caveat |
|---|---|---|
| Availability | 99.0% monthly, excluding announced maintenance | One instance has unavoidable restart/host failure downtime |
| Platform latency | 99% of scripted-provider `/healthz` and `/metrics` requests within the current performance p99 budgets | Excludes ingress and model-provider latency |
| Analysis success | 99% complete or return an explicit partial report/error | Provider failures may degrade a run rather than fail it |
| RTO | 30 minutes | Restore one known-good image, configuration and audit volume |
| RPO | 24 hours for configuration; last flushed JSONL record for audit | Checkpoints and memory have no durable RPO: all in-process state may be lost |

Choose real targets from workload and restore tests. Multi-region availability,
durable run recovery and tighter RPO require the enterprise extension path
described in `LIMITATIONS.md`; changing an SLO does not create that machinery.
