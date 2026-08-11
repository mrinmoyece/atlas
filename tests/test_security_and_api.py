"""Security controls and the HTTP surface."""

from __future__ import annotations

import json

import httpx
import pytest

from atlas.api.app import create_app
from atlas.config import Settings
from atlas.domain.types import DueDiligenceReport
from atlas.security import ApiKeyAuthenticator, AuditLog, AuthError, Role, hash_key
from atlas.security.ratelimit import RateLimiter, TokenBucket

# ------------------------------------------------------------------- auth


def test_unconfigured_authenticator_fails_closed():
    """Misconfiguration must never mean 'open mode'."""
    with pytest.raises(AuthError):
        ApiKeyAuthenticator({}).authenticate("anything")


def test_wrong_and_missing_keys_are_rejected(authenticator):
    for candidate in (None, "", "wrong-key"):
        with pytest.raises(AuthError):
            authenticator.authenticate(candidate)


def test_valid_key_yields_scoped_principal(authenticator):
    from tests.conftest import ANALYST_KEY, VIEWER_KEY

    analyst = authenticator.authenticate(ANALYST_KEY)
    viewer = authenticator.authenticate(VIEWER_KEY)
    assert analyst.can("run:create") and analyst.can("run:read")
    assert viewer.can("run:read") and not viewer.can("run:create")
    assert not analyst.can("admin:manage")


def test_keys_are_stored_hashed_not_plaintext(authenticator):
    serialised = repr(authenticator.__dict__)
    from tests.conftest import ANALYST_KEY

    assert ANALYST_KEY not in serialised
    assert hash_key(ANALYST_KEY) in serialised


def test_env_parsing_ignores_malformed_entries():
    auth = ApiKeyAuthenticator.from_env(f"good:{hash_key('k')}:analyst;garbage;x:y")
    assert auth.configured
    assert auth.authenticate("k").roles == (Role.ANALYST,)


# ------------------------------------------------------------- rate limits


def test_token_bucket_allows_burst_then_throttles():
    bucket = TokenBucket(capacity=2, refill_per_s=0.0001)
    assert bucket.consume().allowed
    assert bucket.consume().allowed
    denied = bucket.consume()
    assert not denied.allowed and denied.retry_after_s > 0


def test_spend_budget_is_reserved_before_the_run_not_checked_after():
    limiter = RateLimiter(daily_spend_usd=1.0)
    limiter.record_spend("p", 0.9)
    assert not limiter.reserve_spend("p", 0.5).allowed
    assert limiter.reserve_spend("p", 0.05).allowed


def test_limits_are_per_principal():
    limiter = RateLimiter(requests_per_minute=1, burst=1)
    assert limiter.check("alice").allowed
    assert not limiter.check("alice").allowed
    assert limiter.check("bob").allowed  # bob is unaffected


def test_rate_limiter_identity_state_is_bounded():
    limiter = RateLimiter(requests_per_minute=1, burst=1, max_buckets=2)
    for principal in ("a", "b", "c", "d"):
        limiter.check(principal)
    assert limiter.tracked_principals == 2


def test_rate_limiter_evicts_idle_identity_state(monkeypatch):
    now = [0.0]
    monkeypatch.setattr("atlas.security.ratelimit.time.monotonic", lambda: now[0])
    limiter = RateLimiter(bucket_ttl_s=60)
    limiter.check("old")
    now[0] = 61.0
    limiter.check("current")
    assert "old" not in limiter._buckets  # noqa: SLF001
    assert "current" in limiter._buckets  # noqa: SLF001


# ------------------------------------------------------------------ audit


def test_audit_chain_detects_tampering():
    log = AuditLog()
    log.record(actor="a", action="run:create", resource="repo")
    log.record(actor="a", action="run:complete", resource="repo")
    assert log.verify()

    log._entries[0] = log._entries[0].model_copy(update={"actor": "attacker"})  # noqa: SLF001
    assert not log.verify()


def test_audit_redacts_secrets():
    log = AuditLog()
    entry = log.record(actor="a", action="x", token="sk-ant-abcdef1234567890")
    assert "sk-ant-abcdef1234567890" not in entry.model_dump_json()
    assert "REDACTED" in entry.model_dump_json()


def test_audit_retains_a_bounded_verifiable_tail():
    log = AuditLog(max_memory_entries=3)
    for i in range(10):
        log.record(actor="a", action=f"action-{i}")
    assert len(log) == 3
    assert log.written_count == 10
    assert [entry.seq for entry in log] == [7, 8, 9]
    assert log.verify()


def test_audit_restores_and_validates_a_persistent_sink(tmp_path):
    sink = tmp_path / "audit.jsonl"
    first = AuditLog(sink=sink, max_memory_entries=2)
    for i in range(4):
        first.record(actor="a", action=f"action-{i}")

    restored = AuditLog(sink=sink, max_memory_entries=2)
    assert restored.written_count == 4
    assert [entry.seq for entry in restored] == [2, 3]
    assert restored.verify()


def test_audit_rejects_a_tampered_persistent_sink(tmp_path):
    sink = tmp_path / "audit.jsonl"
    log = AuditLog(sink=sink)
    log.record(actor="original", action="run:create")
    sink.write_text(sink.read_text().replace("original", "attacker"))

    with pytest.raises(ValueError, match="invalid audit chain"):
        AuditLog(sink=sink)


def test_create_app_preserves_an_injected_empty_audit_log(authenticator, tmp_path):
    supplied = AuditLog(sink=tmp_path / "audit.jsonl")
    app = create_app(
        settings=Settings(provider="scripted"),
        authenticator=authenticator,
        audit=supplied,
    )
    assert app.state.audit_log is supplied


# -------------------------------------------------------------------- api


@pytest.fixture
def client_app(authenticator):
    app = create_app(
        settings=Settings(provider="scripted"),
        authenticator=authenticator,
        allowed_origins=["https://example.com"],
    )
    return app


async def _client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=60
    )


async def test_health_is_public(client_app):
    async with await _client(client_app) as c:
        async with client_app.router.lifespan_context(client_app):
            body = (await c.get("/healthz")).json()
            assert body["status"] == "ok" and body["auth_configured"] is True


async def test_authn_and_authz_enforced(client_app, auth_headers):
    async with await _client(client_app) as c:
        async with client_app.router.lifespan_context(client_app):
            payload = {"repo": "legacy-billing"}
            assert (await c.post("/v1/analyses", json=payload)).status_code == 401
            assert (
                await c.post("/v1/analyses", json=payload, headers={"authorization": "Bearer nope"})
            ).status_code == 401
            assert (
                await c.post("/v1/analyses", json=payload, headers=auth_headers["viewer"])
            ).status_code == 403
            assert (await c.get("/v1/audit", headers=auth_headers["analyst"])).status_code == 403
            assert (await c.get("/v1/audit", headers=auth_headers["admin"])).status_code == 200


@pytest.mark.parametrize(
    "payload",
    [
        {"repo": "../../etc"},
        {"repo": "legacy-billing", "pattern": "not-a-pattern"},
        {"repo": "legacy-billing", "categories": ["nonsense"]},
        {"repo": "legacy-billing", "unexpected_field": 1},
    ],
)
async def test_malformed_requests_are_rejected(client_app, auth_headers, payload):
    async with await _client(client_app) as c:
        async with client_app.router.lifespan_context(client_app):
            resp = await c.post("/v1/analyses", json=payload, headers=auth_headers["analyst"])
            assert resp.status_code == 422


async def test_oversized_body_rejected(client_app, auth_headers):
    async with await _client(client_app) as c:
        async with client_app.router.lifespan_context(client_app):
            resp = await c.post(
                "/v1/analyses",
                json={"repo": "legacy-billing", "pattern": "x" * 2_000_000},
                headers=auth_headers["analyst"],
            )
            assert resp.status_code == 413


async def test_security_headers_present(client_app):
    async with await _client(client_app) as c:
        async with client_app.router.lifespan_context(client_app):
            resp = await c.get("/healthz")
            for header in (
                "content-security-policy",
                "x-content-type-options",
                "x-frame-options",
                "strict-transport-security",
                "x-request-id",
            ):
                assert header in {k.lower() for k in resp.headers}


async def test_full_analysis_returns_grounded_report(client_app, auth_headers):
    async with await _client(client_app) as c:
        async with client_app.router.lifespan_context(client_app):
            resp = await c.post(
                "/v1/analyses",
                json={"repo": "legacy-billing", "pattern": "react"},
                headers=auth_headers["analyst"],
            )
            assert resp.status_code == 200
            body = resp.json()
            assert body["findings"] and body["cost_usd"] > 0
            assert all(f["evidence"] for f in body["findings"])
            assert "NOT CLEAR TO PROCEED" in body["verdict"]
            assert body["request_id"]


async def test_streaming_emits_specialist_then_complete(client_app, auth_headers):
    from atlas.observability import metrics

    metrics.reset()
    async with await _client(client_app) as c:
        async with client_app.router.lifespan_context(client_app):
            resp = await c.post(
                "/v1/analyses/stream",
                json={"repo": "legacy-billing"},
                headers=auth_headers["analyst"],
            )
            assert resp.status_code == 200
            events = [
                line.split(": ", 1)[1]
                for line in resp.text.splitlines()
                if line.startswith("event:")
            ]
            # Order and completeness only. This test used to be cited as
            # evidence of streaming, which it never was: it passes
            # identically against a fully buffered response. The timing
            # property lives in
            # `test_stream_endpoint_delivers_the_first_event_before_the_run_ends`,
            # which drives the ASGI app directly because httpx buffers.
            assert events[0] == "accepted"
            assert events.count("specialist") == 4
            assert events[-1] == "complete"
            # Each specialist frame must carry its own payload - four
            # identical frames would satisfy the count above.
            payloads = [
                json.loads(line.split(": ", 1)[1])
                for line in resp.text.splitlines()
                if line.startswith("data: ")
            ]
            categories = {p["category"] for p in payloads if "category" in p}
            assert categories == {"security", "architecture", "dependency", "delivery"}
            assert "atlas_runs_total 1" in metrics.render()


async def test_audit_records_every_privileged_action(client_app, auth_headers):
    async with await _client(client_app) as c:
        async with client_app.router.lifespan_context(client_app):
            await c.post(
                "/v1/analyses",
                json={"repo": "legacy-billing"},
                headers=auth_headers["analyst"],
            )
            body = (await c.get("/v1/audit", headers=auth_headers["admin"])).json()
            actions = {e["action"] for e in body["entries"]}
            assert {"run:create", "run:complete"} <= actions
            assert body["chain_valid"] is True


async def test_partial_run_is_recorded_as_partial(monkeypatch, authenticator, auth_headers):
    def partial_run(**kwargs):
        return DueDiligenceReport(repo=kwargs["repo"], errors={"security": "provider failed"})

    monkeypatch.setattr("atlas.api.app.run_due_diligence", partial_run)
    app = create_app(settings=Settings(provider="scripted"), authenticator=authenticator)

    async with await _client(app) as c:
        async with app.router.lifespan_context(app):
            response = await c.post(
                "/v1/analyses",
                json={"repo": "legacy-billing"},
                headers=auth_headers["analyst"],
            )

    assert response.status_code == 200
    completion = next(e for e in app.state.audit_log if e.action == "run:complete")
    assert completion.outcome == "partial"
    assert completion.detail["failed_specialists"] == ["security"]


async def test_metrics_require_auth_and_expose_counters(client_app, auth_headers):
    async with await _client(client_app) as c:
        async with client_app.router.lifespan_context(client_app):
            assert (await c.get("/metrics")).status_code == 401
            resp = await c.get("/metrics", headers=auth_headers["analyst"])
            assert resp.status_code == 200
            assert "atlas_auth_total" in resp.text
