"""Model factory: one place that decides which chat model the platform uses.

Everything downstream (agents, patterns, graph) depends only on
`BaseChatModel`, so swapping providers is a config change, never a code
change. The default is the deterministic scripted model, which is why
`pytest`, the benchmarks and the demo all run with no API key present.
"""

from __future__ import annotations

from typing import Any

from langchain_core.language_models import BaseChatModel

from atlas.config import Settings, get_settings
from atlas.llm.scripted import ScriptedChatModel


def build_model(settings: Settings | None = None, **overrides: Any) -> BaseChatModel:
    s = settings or get_settings()
    provider = overrides.pop("provider", None) or s.provider

    if provider == "scripted":
        # Callers that want real behaviour inject their own routes; an
        # empty-route model still answers (terminal response) rather than
        # raising, so accidental use degrades gracefully.
        return ScriptedChatModel(routes=overrides.pop("routes", {}) or {})

    if provider == "anthropic":
        try:
            from langchain_anthropic import ChatAnthropic
        except ImportError as e:  # pragma: no cover
            raise RuntimeError(
                "Anthropic support requires: pip install 'atlas-dd[anthropic]'"
            ) from e
        return ChatAnthropic(
            model=s.model,
            api_key=s.anthropic_api_key,
            max_tokens=overrides.pop("max_tokens", 4096),
            temperature=overrides.pop("temperature", 0.0),
            **overrides,
        )

    raise ValueError(f"unknown provider {provider!r} (expected 'scripted' or 'anthropic')")
