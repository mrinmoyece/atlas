# Runbook

## Deploy

```bash
docker compose up --build          # local, hardened container
kubectl apply -f k8s/deployment.yaml
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

## Monitoring

`/healthz` is public; `/metrics` requires `run:read` because it exposes
spend.

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

### Provider outage
Specialists fail individually, runs complete partially with errors named in
the verdict. No corruption. Re-run affected repositories after recovery.

## Rollback

The service is stateless apart from in-process memory and audit. Rolling
back is a normal image rollback; in-memory audit and memory are lost, which
is why both have documented persistent-sink upgrade paths in LIMITATIONS.md.
