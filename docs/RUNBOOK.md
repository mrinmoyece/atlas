# Runbook

## Deploy

### Local container

```bash
make local-config
make up
# Optional authenticated Prometheus:
docker compose --profile metrics up --build
source .env
```

Compose binds to loopback, uses the offline scripted provider and persists the
audit JSONL in a named volume. `make local-config` creates an untracked `.env`
with random analyst and viewer keys plus a gitignored viewer-only Prometheus
token; it refuses to overwrite existing credentials. Use `make down` to stop
containers without deleting audit data; `make clean` also deletes the volume.
Override occupied host ports with `ATLAS_PORT` and `PROMETHEUS_PORT`.

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

### Configuration reference

Application settings use the `ATLAS_` prefix
([source](../src/atlas/config.py)):

| Variable | Default | Purpose |
|---|---:|---|
| `ATLAS_API_KEYS` | unset | `key_id:sha256:roles;...`; without it every protected route 401s |
| `ATLAS_PROVIDER` | `scripted` | `scripted` or `anthropic` |
| `ATLAS_MODEL` | `claude-sonnet-4-5` | live-provider model identifier |
| `ATLAS_ANTHROPIC_API_KEY` | unset | live-provider credential; secret |
| `ATLAS_CONTEXT_TOKEN_BUDGET` | `12000` | estimated input tokens per model call |
| `ATLAS_COMPACTION_KEEP_RECENT` | `6` | recent messages retained during compaction |
| `ATLAS_MAX_STEPS_PER_SPECIALIST` | `12` | tool/model loop bound |
| `ATLAS_MAX_COST_USD` | `5.0` | pre-call run-cost brake and admission reservation |
| `ATLAS_SPECIALIST_TIMEOUT_S` | `60` | wall-clock wait per specialist |
| `ATLAS_DAILY_SPEND_USD` | `25.0` | per-principal daily admission ledger |
| `ATLAS_REQUESTS_PER_MINUTE` | `60.0` | per-principal token-bucket refill |
| `ATLAS_RATE_LIMIT_BURST` | same as rate | optional token-bucket capacity |
| `ATLAS_RATE_LIMIT_MAX_BUCKETS` | `10000` | bound on tracked principal state |
| `ATLAS_RATE_LIMIT_BUCKET_TTL_S` | `3600` | idle principal eviction |
| `ATLAS_AUDIT_SINK` | unset | optional append-only JSONL path |
| `ATLAS_AUDIT_MEMORY_MAX_ENTRIES` | `10000` | retained in-process audit tail |
| `ATLAS_MEMORY_ENABLED` | `true` | enable the in-process memory hub |
| `ATLAS_MEMORY_TOP_K` | `3` | maximum semantic lessons retrieved |
| `ATLAS_MEMORY_DECAY_HALF_LIFE_RUNS` | `50` | semantic recency half-life |
| `ATLAS_OTEL_ENDPOINT` | unset | OTLP HTTP trace endpoint; unset is a no-op |
| `ATLAS_SERVICE_NAME` | `atlas` | OpenTelemetry service name |

Path and harness overrides are read directly by their owning entry points:

| Variable | Used by | Purpose |
|---|---|---|
| `ATLAS_FIXTURES_ROOT` | API | analysable fixture directory for installed/container layouts |
| `ATLAS_PROJECT_ROOT` | eval runner | repository root containing fixtures and answer key |
| `ATLAS_LOAD_TEST_KEY` | Locust | raw load-test API key; never commit it |
| `ATLAS_PORT`, `PROMETHEUS_PORT` | Compose | loopback host-port overrides |

The scripted provider supplies synthetic token and priced-cost metadata for
offline tests. The current Anthropic adapter does not convert usage into
`cost_usd`; do not rely on `ATLAS_MAX_COST_USD` or
`ATLAS_DAILY_SPEND_USD` as monetary controls for live-provider traffic
([limitation](LIMITATIONS.md#security)).

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
- [ ] On Kubernetes, verify a default `StorageClass` exists and the
      `atlas-audit` PVC is `Bound` before admitting traffic.
- [ ] Probe public `/healthz`, then authenticate `/metrics`, a read request, an
      analysis and an admin-only audit request with the expected roles. Also
      confirm missing, viewer and invalid credentials fail as expected.
- [ ] Exercise one SSE analysis through the real ingress and confirm events
      arrive incrementally; test request-size and 429 behaviour.
- [ ] Install site-specific alerts from the signals below, test trace export,
      rehearse audit-volume restore, and verify image/SBOM provenance. Atlas
      does not ship Alertmanager or `PrometheusRule` resources.

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

Atlas intentionally does not ship alert-manager configuration: routing,
escalation and paging ownership are deployment-specific. The queries above are
signal examples, not installed alerts.

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

1. Stop new traffic and preserve the request IDs, current image digest,
   configuration and audit volume.
2. Verify the JSONL audit chain by starting the known-good image against a
   read-only copy first; startup rejects a malformed chain.
3. Kubernetes: `kubectl rollout undo deployment/atlas`, then wait for
   `kubectl rollout status deployment/atlas`. Compose has no release registry
   in this repository; pin the previously tested image in an override before
   recreating the service.
4. Probe `/healthz`, authenticate `/metrics`, run one fixture analysis and
   verify `/v1/audit` reports `chain_valid: true`.
5. Re-run analyses that were in flight. Checkpoints, memory, request buckets
   and spend-ledger state are process-local and are lost on restart; they have
   no recovery path.

The optional JSONL audit sink can survive on a durable mounted volume, but it
does not protect against host loss or an attacker controlling that host. See
[limitations](LIMITATIONS.md).

## Reference SLIs and example objectives

These are starting points for a **single-instance CV/reference deployment**,
not measured guarantees or enterprise commitments:

| Objective | Example target | SLI source and caveat |
|---|---|---|
| Availability | 99.0% monthly, excluding maintenance | external `/healthz` probe or ingress status metrics; Atlas emits no HTTP request SLI |
| Platform analysis latency | p95/p99 within the current scripted-provider budgets | `histogram_quantile` over `atlas_run_duration_ms_bucket`; excludes ingress and provider latency |
| Specialist success | 99% of specialist runs have `outcome="ok"` | `atlas_specialist_runs_total`; partial reports remain successful HTTP responses |
| RTO | 30 minutes | timed restore drill of image, configuration and audit volume |
| Audit RPO | last fsynced JSONL entry | one `fsync` per record when `ATLAS_AUDIT_SINK` is set |
| Memory/checkpoint RPO | none | all in-process state can be lost on restart |

These targets are examples, not measured guarantees. Choose production targets
only after deployed load and restore drills. Multi-region availability, durable
run recovery and tighter RPO require the extension path in
[LIMITATIONS.md](LIMITATIONS.md); changing an SLO does not create that machinery.
