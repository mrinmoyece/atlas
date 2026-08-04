"""FastAPI surface.

Security posture is applied in layers, outermost first:

    RequestContext   -> request id, safe error containment
    BodySizeLimit    -> reject oversized payloads
    SecurityHeaders  -> CSP/HSTS/nosniff/...
    CORS             -> explicit origin allowlist (never "*")
    auth dependency  -> API key -> Principal (deny by default)
    permission check -> RBAC per route
    rate limit       -> request rate AND spend budget, checked pre-run
    audit            -> hash-chained record of every privileged action

`/v1/analyses` runs the multi-agent graph; `/v1/analyses/stream` emits the
same run as Server-Sent Events so a UI can show specialists finishing as
they finish, rather than staring at a spinner for the length of the slowest
one - which for parallel fan-out is the whole point.
"""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse, StreamingResponse
from starlette.background import BackgroundTask

from atlas.api.schemas import AnalysisRequest, AnalysisResponse, FindingView, HealthResponse
from atlas.config import Settings, get_settings
from atlas.domain.types import Category, DueDiligenceReport
from atlas.evals.scenarios import model_for
from atlas.graph.build import _context_key as graph_context_key
from atlas.graph.build import run_due_diligence, stream_due_diligence
from atlas.llm.factory import build_model
from atlas.memory.hub import MemoryHub
from atlas.observability import metrics, setup_logging, setup_tracing
from atlas.observability.logging import get_logger
from atlas.security import (
    ApiKeyAuthenticator,
    AuditLog,
    AuthError,
    BodySizeLimitMiddleware,
    Principal,
    RateLimiter,
    RateLimitMiddleware,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
)

log = get_logger(__name__)


def _fixtures_root() -> Path:
    """Where analysable repositories live.

    `parents[3]` only works from a source checkout; once the package is
    pip-installed (as it is in the container) that path resolves inside
    site-packages and every request 404s. So: explicit setting first, then
    CWD, then the source layout as a development convenience.
    """
    configured = os.environ.get("ATLAS_FIXTURES_ROOT")
    if configured:
        return Path(configured).resolve()
    cwd_candidate = Path.cwd() / "fixtures" / "repos"
    if cwd_candidate.is_dir():
        return cwd_candidate.resolve()
    return (Path(__file__).resolve().parents[3] / "fixtures" / "repos").resolve()


FIXTURES = _fixtures_root()


def _projected_run_cost(cfg: Settings) -> float:
    """What to reserve before a run.

    This MUST be the per-run ceiling, not a guess. A hardcoded $0.25 against
    a $5.00 ceiling meant four pre-authorised runs could spend $20 under a $1
    daily cap - the reservation machinery was correct and the number made it
    meaningless. Reserving the ceiling is the only value that makes the daily
    cap an actual bound; the unused remainder is released at settlement.
    """
    return max(0.01, cfg.max_cost_usd)


class BudgetConfigurationError(ValueError):
    """Budget settings that would reject every request."""


def _validate_budget_invariant(cfg: Settings, limiter: RateLimiter) -> None:
    """Reserving the per-run ceiling implies `daily_cap >= per_run_ceiling`.

    Nobody stated that invariant when the reservation was changed to project
    the ceiling, and violating it does not fail loudly: every request 429s
    with "daily spend limit exceeded" while the service has spent exactly
    nothing. Checking it at startup turns a 3am debugging session into a
    message that names both numbers and the environment variable to change.
    """
    projected = _projected_run_cost(cfg)
    if limiter.daily_spend_usd < projected:
        raise BudgetConfigurationError(
            f"daily spend cap ${limiter.daily_spend_usd:.2f} is below the per-run "
            f"ceiling ${projected:.2f}, so every run would be rejected before it "
            f"starts. Raise ATLAS_DAILY_SPEND_USD to at least ${projected:.2f}, "
            f"or lower ATLAS_MAX_COST_USD."
        )


def create_app(
    *,
    settings: Settings | None = None,
    authenticator: ApiKeyAuthenticator | None = None,
    rate_limiter: RateLimiter | None = None,
    audit: AuditLog | None = None,
    memory: MemoryHub | None = None,
    allowed_origins: list[str] | None = None,
) -> FastAPI:
    cfg = settings or get_settings()
    auth = authenticator or ApiKeyAuthenticator.from_env()
    limiter = rate_limiter or RateLimiter(
        requests_per_minute=cfg.requests_per_minute,
        burst=cfg.rate_limit_burst,
        daily_spend_usd=cfg.daily_spend_usd,
    )
    audit_log = audit or AuditLog()
    _validate_budget_invariant(cfg, limiter)
    hub = memory or MemoryHub(
        enabled=cfg.memory_enabled,
        top_k=cfg.memory_top_k,
        decay_half_life_runs=cfg.memory_decay_half_life_runs,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        setup_logging()
        setup_tracing(service_name=cfg.service_name, endpoint=cfg.otel_endpoint)
        if not auth.configured:
            # Loud, because the service will reject every request until this
            # is fixed - silence here would look like a broken deployment.
            log.warning(
                "no_api_keys_configured",
                extra={"ctx": {"hint": "set ATLAS_API_KEYS; all routes will 401"}},
            )
        yield

    app = FastAPI(
        title="Atlas",
        description="Evaluation-driven multi-agent technical due diligence",
        version="0.1.0",
        lifespan=lifespan,
    )

    # Order matters: added last == outermost in Starlette.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins or [],  # explicit allowlist, never "*"
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["authorization", "content-type", "x-request-id"],
    )
    app.add_middleware(SecurityHeadersMiddleware)
    # Before routing, so 404s, 422s and unauthenticated probes are metered
    # too - see the class docstring for the measurement that forced this.
    app.add_middleware(RateLimitMiddleware, limiter=limiter, authenticator=auth)
    app.add_middleware(BodySizeLimitMiddleware)
    app.add_middleware(RequestContextMiddleware)

    # ------------------------------------------------------------------
    # auth / rbac dependencies
    # ------------------------------------------------------------------

    def current_principal(authorization: str | None = Header(default=None)) -> Principal:
        token = ""
        if authorization and authorization.lower().startswith("bearer "):
            token = authorization[7:]
        try:
            principal = auth.authenticate(token)
        except AuthError:
            metrics.record_auth(outcome="denied")
            raise HTTPException(status_code=401, detail="invalid or missing credentials") from None
        metrics.record_auth(outcome="allowed")
        return principal

    def requires(permission: str):
        def dependency(principal: Principal = Depends(current_principal)) -> Principal:
            if not principal.can(permission):
                audit_log.record(
                    actor=principal.subject,
                    action=permission,
                    outcome="denied",
                    reason="insufficient_role",
                )
                raise HTTPException(status_code=403, detail="insufficient permissions")
            return principal

        return dependency

    def enforce_limits(principal: Principal) -> float:
        """Reserve budget for a run that is about to happen.

        Spend only. The request-rate token is taken separately and earlier,
        by `enforce_request_rate` - see the note there for why combining them
        was a security bug rather than a tidiness question.

        The caller MUST release the reservation if the run does not happen -
        see `_reservation` below. Reserving before validating the request was
        a real defect: a client probing unknown repo names got 404s while the
        reservations accumulated, and ~100 probes permanently exhausted that
        principal's daily budget having spent nothing.
        """
        # Reserve, do not merely check: two concurrent requests that both
        # pass a read-only check will both spend, and the budget overshoots.
        spend = limiter.reserve_spend(principal.subject, _projected_run_cost(cfg))
        if not spend.allowed:
            metrics.record_rate_limit(principal_kind="api_key_spend")
            raise HTTPException(status_code=429, detail=spend.reason)
        return _projected_run_cost(cfg)

    @contextmanager
    def _reservation(principal: Principal):
        """Hold a spend reservation for the duration of a run.

        Ordering matters: the repository is resolved *before* the reservation
        is taken, so a 404 costs nothing. Anything that raises afterwards
        releases via `finally`, including a client that disconnects before
        consuming a stream.
        """
        reserved = enforce_limits(principal)
        released = False

        def settle(actual_cost: float) -> None:
            nonlocal released
            if not released:
                released = True
                limiter.record_spend(principal.subject, actual_cost, reserved=reserved)

        try:
            yield settle
        finally:
            if not released:
                limiter.release_reservation(principal.subject, reserved)

    def resolve_repo(request_body: AnalysisRequest) -> Path:
        """Resolve a repo name to a path inside the fixtures root.

        Path confinement at the API boundary as well as in the toolkit:
        defence in depth, because this is the layer an attacker reaches
        first.
        """
        root = (FIXTURES / request_body.repo).resolve()
        # Path ancestry, not string prefix - see RepoToolkit._resolve for why
        # startswith() is a broken confinement check.
        if not root.is_relative_to(FIXTURES.resolve()) or not root.is_dir():
            raise HTTPException(status_code=404, detail="unknown repository")
        return root

    # ------------------------------------------------------------------
    # routes
    # ------------------------------------------------------------------

    @app.get("/healthz", response_model=HealthResponse)
    async def healthz() -> HealthResponse:
        return HealthResponse(status="ok", version="0.1.0", auth_configured=auth.configured)

    @app.get("/metrics", response_class=PlainTextResponse)
    async def metrics_endpoint(
        principal: Principal = Depends(requires("run:read")),
    ) -> str:
        # Metrics are authenticated: they expose spend and volume, which is
        # commercially sensitive.
        return metrics.render()

    @app.post("/v1/analyses", response_model=AnalysisResponse)
    async def create_analysis(
        body: AnalysisRequest,
        request: Request,
        principal: Principal = Depends(requires("run:create")),
    ) -> AnalysisResponse:
        # The request-rate token was already consumed by RateLimitMiddleware,
        # before routing - it has to be, or an invalid body escapes limiting.
        # Here we only resolve (an unknown repository must not consume
        # budget) and then reserve spend.
        root = resolve_repo(body)
        request_id = getattr(request.state, "request_id", "")

        audit_log.record(
            actor=principal.subject,
            action="run:create",
            resource=body.repo,
            request_id=request_id,
            pattern=body.pattern,
        )

        with _reservation(principal) as settle:
            report = await asyncio.to_thread(
                run_due_diligence,
                repo=body.repo,
                repo_root=str(root),
                model=_model_for(body.repo, cfg),
                settings=cfg,
                memory=hub,
                pattern_name=body.pattern,
                categories=_categories(body),
                thread_id=f"{principal.key_id}-{request_id}",
            )
            settle(report.cost_usd)

        audit_log.record(
            actor=principal.subject,
            action="run:complete",
            resource=body.repo,
            request_id=request_id,
            findings=len(report.findings),
            cost_usd=report.cost_usd,
        )

        return AnalysisResponse(
            repo=report.repo,
            verdict=report.verdict,
            findings=[FindingView.from_finding(f) for f in report.by_severity()],
            summaries=report.summaries,
            counts=report.counts(),
            tokens_used=report.tokens_used,
            cost_usd=report.cost_usd,
            duration_ms=report.duration_ms,
            errors=report.errors,
            request_id=request_id,
        )

    @app.post("/v1/analyses/stream")
    async def stream_analysis(
        body: AnalysisRequest,
        request: Request,
        principal: Principal = Depends(requires("run:create")),
    ) -> StreamingResponse:
        root = resolve_repo(body)
        request_id = getattr(request.state, "request_id", "")
        audit_log.record(
            actor=principal.subject,
            action="run:create:stream",
            resource=body.repo,
            request_id=request_id,
        )

        # Reserve in the HANDLER, not inside `events()`.
        #
        # Reserving inside the generator solved a real problem - a response
        # whose body is never consumed would otherwise hold the reservation
        # forever - and created a worse one. By the time a generator body
        # runs, Starlette has already sent `http.response.start`, so the
        # HTTPException raised on an exhausted budget cannot become a
        # response. Measured: the non-streaming route returned 429 while
        # this one raised `RuntimeError: Caught handled exception, but
        # response already started` and the client saw a truncated 200. No
        # status, no `Retry-After`, and a 500 in the logs for a condition
        # that is entirely expected.
        #
        # Reserving here restores the 429. The never-consumed-body case is
        # handled by `BackgroundTask` below, which Starlette runs after the
        # response completes *however* it completes - including a client
        # that disconnects mid-stream.
        reserved = enforce_limits(principal)
        settled = {"done": False}

        def release_if_unsettled() -> None:
            if not settled["done"]:
                settled["done"] = True
                limiter.release_reservation(principal.subject, reserved)

        async def events():
            """Emit events as nodes complete, not after the run finishes.

            The graph is driven on a worker thread which pushes each node
            update onto a queue; this coroutine drains the queue
            concurrently. Awaiting the worker before draining (the obvious
            shortcut) makes every event arrive at the same instant - it
            looks like streaming in a test that only checks event order,
            and is useless to a real UI.
            """
            queue: asyncio.Queue[str | None] = asyncio.Queue()
            loop = asyncio.get_running_loop()
            totals: dict[str, Any] = {"findings": 0, "cost_usd": 0.0, "report": None}
            collected: list[Any] = []
            stream_errors: dict[str, str] = {}

            def emit(payload: str) -> None:
                loop.call_soon_threadsafe(queue.put_nowait, payload)

            def work() -> None:
                try:
                    for update in stream_due_diligence(
                        repo=body.repo,
                        repo_root=str(root),
                        model=_model_for(body.repo, cfg),
                        settings=cfg,
                        memory=hub,
                        pattern_name=body.pattern,
                        categories=_categories(body),
                        thread_id=f"{principal.key_id}-{request_id}",
                    ):
                        for node, delta in update.items():
                            stream_errors.update(delta.get("errors") or {})
                            if node == "plan":
                                emit(_sse("plan", {"specialists": delta.get("plan", [])}))
                                continue
                            if node == "synthesise":
                                totals["report"] = DueDiligenceReport(
                                    repo=body.repo,
                                    findings=tuple(collected),
                                    verdict=delta.get("verdict", ""),
                                    cost_usd=round(float(totals["cost_usd"]), 8),
                                    errors=dict(stream_errors),
                                )
                                emit(_sse("verdict", {"verdict": delta.get("verdict", "")}))
                                continue
                            findings = delta.get("findings") or []
                            collected.extend(findings)
                            totals["findings"] += len(findings)
                            totals["cost_usd"] += float(delta.get("cost_usd") or 0.0)
                            emit(
                                _sse(
                                    "specialist",
                                    {
                                        "category": node,
                                        "findings": len(findings),
                                        "summary": (delta.get("summaries") or {}).get(node, ""),
                                        "error": (delta.get("errors") or {}).get(node, ""),
                                    },
                                )
                            )
                    limiter.record_spend(principal.subject, totals["cost_usd"], reserved=reserved)
                    settled["done"] = True
                    # Learn, exactly as the non-streaming path does. Wiring
                    # memory into only one of two run routes is how "the hub
                    # stays empty forever" comes back through the side door.
                    if hub.enabled and totals["report"] is not None:
                        report = totals["report"]
                        hub.learn_from_report(
                            report,
                            context_key=graph_context_key({"repo": body.repo}),
                            strategy=body.pattern or "react",
                            success=not report.errors
                            and any(f.is_grounded() for f in report.findings),
                        )
                    audit_log.record(
                        actor=principal.subject,
                        action="run:complete",
                        resource=body.repo,
                        request_id=request_id,
                        findings=totals["findings"],
                        cost_usd=round(totals["cost_usd"], 6),
                    )
                    emit(
                        _sse(
                            "complete",
                            {
                                "findings": totals["findings"],
                                "cost_usd": round(totals["cost_usd"], 6),
                            },
                        )
                    )
                except Exception as e:  # noqa: BLE001
                    log.exception("stream_failed", extra={"ctx": {"repo": body.repo}})
                    emit(_sse("error", {"error": type(e).__name__}))
                finally:
                    # Release an unused reservation even when the run failed
                    # or the client vanished mid-stream. `release_if_unsettled`
                    # is idempotent and also runs as a BackgroundTask, so the
                    # reservation is settled exactly once on every path.
                    release_if_unsettled()
                    loop.call_soon_threadsafe(queue.put_nowait, None)

            yield _sse("accepted", {"repo": body.repo, "request_id": request_id})
            worker = asyncio.create_task(asyncio.to_thread(work))
            try:
                while True:
                    item = await queue.get()
                    if item is None:
                        break
                    yield item
            finally:
                await worker

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
            # Runs after the response finishes by any route - completion,
            # client disconnect, or an exception mid-stream. This is what
            # makes reserving in the handler safe.
            background=BackgroundTask(release_if_unsettled),
        )

    @app.get("/v1/audit")
    async def read_audit(
        principal: Principal = Depends(requires("admin:manage")),
    ) -> dict[str, Any]:
        return {
            "entries": [e.model_dump() for e in audit_log.tail(50)],
            "chain_valid": audit_log.verify(),
            "count": len(audit_log),
        }

    @app.get("/v1/memory")
    async def read_memory(
        principal: Principal = Depends(requires("run:read")),
    ) -> dict[str, Any]:
        return {
            "enabled": hub.enabled,
            **hub.stats(),
            "recent": [
                {"text": r.text, "repo": r.metadata.get("repo", "")} for r in hub.history(limit=5)
            ],
        }

    app.state.audit_log = audit_log
    app.state.rate_limiter = limiter
    app.state.memory = hub
    return app


def _model_for(repo: str, cfg: Settings):
    """Scripted models are per-repo; real providers are repo-agnostic."""
    if cfg.provider == "scripted":
        try:
            return model_for(repo)
        except KeyError:
            return build_model(cfg)
    return build_model(cfg)


def _categories(body: AnalysisRequest) -> tuple[Category, ...]:
    if not body.categories:
        return (
            Category.SECURITY,
            Category.ARCHITECTURE,
            Category.DEPENDENCY,
            Category.DELIVERY,
        )
    return tuple(Category(c) for c in body.categories)


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


app = create_app()
