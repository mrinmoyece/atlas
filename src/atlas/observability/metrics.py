"""Prometheus metrics, dependency-free.

Cardinality discipline (the rule that keeps a metrics backend alive):
labels are bounded sets only - category, pattern, outcome. Repository name
is **never** a label, because a due-diligence platform analyses unbounded
repositories and that would create unbounded time series. Per-repo detail
belongs in traces and logs, not in metrics.
"""

from __future__ import annotations

import threading
from collections import defaultdict

# Latency buckets in ms, chosen around agent reality: sub-second is a cache
# or a failure, most specialist runs land in seconds, anything over a minute
# is pathological and should be visible as its own bucket.
_BUCKETS = (100, 500, 1_000, 5_000, 15_000, 60_000)

_LOCK = threading.Lock()
_COUNTERS: dict[tuple[str, tuple[tuple[str, str], ...]], float] = defaultdict(float)
# Histograms are stored as bucket counters + sum + count, NOT as raw
# observations. Retaining every sample would grow without bound in a
# long-running process and make each scrape O(n log n) - the opposite of
# what a metrics backend needs, and an odd failure for a module whose first
# rule is cardinality discipline.
_HISTOGRAMS: dict[tuple[str, tuple[tuple[str, str], ...]], dict[str, float]] = defaultdict(
    lambda: {"sum": 0.0, "count": 0.0, **{str(b): 0.0 for b in _BUCKETS}}
)
_HELP: dict[str, str] = {}


def _inc(name: str, help_text: str, value: float = 1.0, **labels: str) -> None:
    key = (name, tuple(sorted(labels.items())))
    with _LOCK:
        _HELP.setdefault(name, help_text)
        _COUNTERS[key] += value


def _observe(name: str, help_text: str, value: float, **labels: str) -> None:
    key = (name, tuple(sorted(labels.items())))
    with _LOCK:
        _HELP.setdefault(name, help_text)
        entry = _HISTOGRAMS[key]
        entry["sum"] += value
        entry["count"] += 1
        for bucket in _BUCKETS:
            if value <= bucket:
                entry[str(bucket)] += 1


def record_specialist(
    *,
    category: str,
    pattern: str,
    findings: int,
    tokens: int,
    cost_usd: float,
    duration_ms: int,
    ok: bool,
) -> None:
    labels = {"category": category, "pattern": pattern}
    _inc(
        "atlas_specialist_runs_total",
        "Specialist agent runs by outcome",
        outcome="ok" if ok else "error",
        **labels,
    )
    _inc("atlas_findings_total", "Findings produced", value=findings, **labels)
    _inc("atlas_tokens_total", "Tokens consumed", value=tokens, **labels)
    _inc("atlas_cost_usd_total", "Model spend in USD", value=cost_usd, **labels)
    _observe(
        "atlas_specialist_duration_ms",
        "Specialist wall-clock duration",
        float(duration_ms),
        **labels,
    )


def record_run(
    *, repo: str, findings: int, cost_usd: float, duration_ms: int, failed_specialists: int
) -> None:
    # NOTE: `repo` is accepted for symmetry with logs/traces but deliberately
    # NOT used as a label - see the cardinality note at the top.
    _inc("atlas_runs_total", "Due-diligence runs")
    _inc("atlas_run_cost_usd_total", "Spend across runs", value=cost_usd)
    _inc("atlas_run_findings_total", "Findings across runs", value=findings)
    if failed_specialists:
        _inc(
            "atlas_run_partial_total",
            "Runs completing with at least one failed specialist",
        )
    _observe("atlas_run_duration_ms", "End-to-end run duration", float(duration_ms))


def record_auth(*, outcome: str) -> None:
    _inc("atlas_auth_total", "API authentication attempts", outcome=outcome)


def record_rate_limit(*, principal_kind: str) -> None:
    _inc("atlas_rate_limited_total", "Requests rejected by rate limiting", principal=principal_kind)


def render() -> str:
    """Prometheus text exposition format."""
    lines: list[str] = []
    with _LOCK:
        counters = sorted(_COUNTERS.items())
        histograms = sorted(_HISTOGRAMS.items())
        helps = dict(_HELP)

    seen: set[str] = set()
    for (name, labels), value in counters:
        if name not in seen:
            seen.add(name)
            lines.append(f"# HELP {name} {helps.get(name, '')}")
            lines.append(f"# TYPE {name} counter")
        lines.append(f"{name}{_fmt_labels(labels)} {_fmt(value)}")

    for (name, labels), entry in histograms:
        if name not in seen:
            seen.add(name)
            lines.append(f"# HELP {name} {helps.get(name, '')}")
            lines.append(f"# TYPE {name} histogram")
        for bucket in _BUCKETS:
            lines.append(
                f"{name}_bucket{_fmt_labels(labels, le=str(bucket))} "
                f"{_fmt(entry.get(str(bucket), 0.0))}"
            )
        lines.append(f"{name}_bucket{_fmt_labels(labels, le='+Inf')} {_fmt(entry['count'])}")
        lines.append(f"{name}_sum{_fmt_labels(labels)} {_fmt(entry['sum'])}")
        lines.append(f"{name}_count{_fmt_labels(labels)} {_fmt(entry['count'])}")

    return "\n".join(lines) + "\n"


def reset() -> None:
    with _LOCK:
        _COUNTERS.clear()
        _HISTOGRAMS.clear()
        _HELP.clear()


def _fmt_labels(labels: tuple[tuple[str, str], ...], **extra: str) -> str:
    items = list(labels) + sorted(extra.items())
    if not items:
        return ""
    body = ",".join(f'{k}="{_escape(str(v))}"' for k, v in items)
    return "{" + body + "}"


def _escape(v: str) -> str:
    return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _fmt(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else f"{v:.8f}".rstrip("0").rstrip(".")
