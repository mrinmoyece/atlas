"""OpenTelemetry tracing with a no-op fallback.

Every graph node, model call and tool call is a span, so one trace shows
the full causal shape of a multi-agent run - including which specialists
ran in parallel and where the time actually went. Without this, "the run
took 40 seconds" is unactionable.

If the otel extra is absent or no endpoint is configured, `span()` degrades
to a no-op context manager and application code never checks.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

try:
    from opentelemetry import trace as _otel_trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    _HAVE_OTEL = True
except ImportError:  # pragma: no cover
    _HAVE_OTEL = False

_tracer: Any = None


def setup_tracing(*, service_name: str, endpoint: str | None) -> None:
    global _tracer
    if not _HAVE_OTEL or not endpoint:
        return
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    _otel_trace.set_tracer_provider(provider)
    _tracer = _otel_trace.get_tracer("atlas")


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[None]:
    if _tracer is None:
        yield
        return
    with _tracer.start_as_current_span(name) as s:  # pragma: no cover
        for key, value in attributes.items():
            s.set_attribute(
                f"atlas.{key}",
                value if isinstance(value, (str, bool, int, float)) else str(value),
            )
        yield
