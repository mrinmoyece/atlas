"""Configuration (12-factor). Safe offline defaults everywhere."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # `extra="ignore"` for the ENVIRONMENT (an unrelated ATLAS_-prefixed
    # variable must not crash startup) but `forbid` would be wrong there and
    # `ignore` was wrong for explicit kwargs: `Settings(repo_root=...)` in a
    # test was silently dropped, so the test believed it had pointed the app
    # at a temp repository and had not. Validated construction is checked in
    # `__init__` instead, where the caller is code rather than an operator.
    model_config = SettingsConfigDict(env_prefix="ATLAS_", env_file=".env", extra="ignore")

    def __init__(self, **values: object) -> None:
        unknown = set(values) - set(type(self).model_fields)
        if unknown:
            raise TypeError(
                f"unknown Settings field(s): {sorted(unknown)}. "
                "Environment variables are still ignored when unrecognised; "
                "an explicit keyword that goes nowhere is always a bug."
            )
        super().__init__(**values)  # type: ignore[arg-type]

    # "scripted" (deterministic, offline, default) or "anthropic"
    provider: str = "scripted"
    model: str = "claude-sonnet-4-5"
    anthropic_api_key: str | None = None

    # Context engineering
    context_token_budget: int = 12_000
    compaction_keep_recent: int = 6

    # Run governance - enforced by the runtime, not by prompts
    max_steps_per_specialist: int = 12
    max_cost_usd: float = 5.0
    specialist_timeout_s: float = 60.0

    # Per-principal API limits. These existed only as `RateLimiter()`
    # defaults, unreachable from config, while `create_app` raised an error
    # telling operators to "raise ATLAS_DAILY_SPEND_USD" - a variable that
    # did not exist anywhere but in that error string. An error message that
    # names a knob nobody can turn is worse than no message.
    #
    # `daily_spend_usd` must be >= `max_cost_usd` or every run is rejected
    # before it starts; `_validate_budget_invariant` enforces that at
    # startup, which is why the default here is 5x the per-run ceiling.
    daily_spend_usd: float = 25.0
    requests_per_minute: float = 60.0
    rate_limit_burst: float | None = None
    rate_limit_max_buckets: int = 10_000
    rate_limit_bucket_ttl_s: float = 3_600.0

    # Audit
    audit_sink: Path | None = None
    audit_memory_max_entries: int = 10_000

    # Memory
    memory_enabled: bool = True
    memory_top_k: int = 3
    memory_decay_half_life_runs: float = 50.0

    # Observability
    otel_endpoint: str | None = None
    service_name: str = "atlas"


@lru_cache
def get_settings() -> Settings:
    return Settings()
