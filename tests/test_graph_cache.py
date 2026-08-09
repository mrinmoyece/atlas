"""The compiled-graph cache.

LangGraph runs `inspect.getsource` over every node during compile, which
tokenises and ASTs the source. Profiled at 9.9ms against a 38.4ms run —
26% of every run spent rebuilding an identical graph. Reusing it took a run
from 36.2ms to 17.7ms, a 51% saving.

A cache is a correctness hazard dressed as an optimisation, so most of what
follows is about when it must NOT return a hit.
"""

from __future__ import annotations

import time

import pytest

from atlas.config import Settings
from atlas.domain.types import Category
from atlas.evals.scenarios import model_for
from atlas.graph.build import _graph_cache, build_graph, build_graph_cached
from atlas.memory.hub import MemoryHub


@pytest.fixture(autouse=True)
def clean_cache():
    _graph_cache.clear()
    yield
    _graph_cache.clear()


class TestReuse:
    def test_the_same_configuration_returns_the_same_graph(self):
        model = model_for("legacy-billing")
        assert build_graph_cached(model=model) is build_graph_cached(model=model)

    def test_a_hit_is_orders_of_magnitude_faster(self):
        """The entire justification. If a hit is not dramatically cheaper
        than a compile, the cache is complexity for nothing."""
        model = model_for("legacy-billing")
        build_graph_cached(model=model)

        start = time.perf_counter()
        build_graph_cached(model=model)
        cached_ms = (time.perf_counter() - start) * 1000

        start = time.perf_counter()
        build_graph(model=model)
        compile_ms = (time.perf_counter() - start) * 1000

        assert cached_ms < compile_ms / 10, f"hit {cached_ms:.3f}ms vs compile {compile_ms:.1f}ms"

    def test_hits_and_misses_are_counted(self):
        model = model_for("legacy-billing")
        build_graph_cached(model=model)
        build_graph_cached(model=model)
        assert _graph_cache.misses == 1
        assert _graph_cache.hits == 1


class TestCorrectness:
    def test_a_different_model_does_not_share_a_graph(self):
        """The nodes close over the model. Sharing across models would run
        one repository's analysis against another's scripted fixture."""
        a, b = model_for("legacy-billing"), model_for("modern-payments")
        assert build_graph_cached(model=a) is not build_graph_cached(model=b)

    def test_a_different_pattern_does_not_share_a_graph(self):
        model = model_for("legacy-billing")
        assert build_graph_cached(model=model, pattern_name="react") is not build_graph_cached(
            model=model, pattern_name="reflexion"
        )

    def test_a_different_category_set_does_not_share_a_graph(self):
        model = model_for("legacy-billing")
        assert build_graph_cached(
            model=model, categories=(Category.SECURITY,)
        ) is not build_graph_cached(model=model, categories=(Category.SECURITY, Category.DELIVERY))

    def test_a_different_settings_object_does_not_share_a_graph(self):
        model = model_for("legacy-billing")
        assert build_graph_cached(
            model=model, settings=Settings(provider="scripted", max_cost_usd=1.0)
        ) is not build_graph_cached(
            model=model, settings=Settings(provider="scripted", max_cost_usd=9.0)
        )

    def test_a_different_memory_hub_does_not_share_a_graph(self):
        model = model_for("legacy-billing")
        assert build_graph_cached(
            model=model, memory=MemoryHub(enabled=True)
        ) is not build_graph_cached(model=model, memory=MemoryHub(enabled=False))

    def test_a_caller_supplied_checkpointer_bypasses_the_cache(self):
        """A checkpointer carries run state. Two runs sharing one would let
        a resumed run see another's checkpoints - so that path pays the
        10ms rather than risk it."""
        from langgraph.checkpoint.memory import InMemorySaver

        model = model_for("legacy-billing")
        saver = InMemorySaver()
        first = build_graph_cached(model=model, checkpointer=saver)
        second = build_graph_cached(model=model, checkpointer=saver)
        assert first is not second
        assert _graph_cache.hits == 0


class TestBounds:
    def test_the_cache_is_bounded(self):
        """Unbounded, a caller constructing a model per request turns the
        cache into a leak - and it holds strong references to every model
        it has ever seen."""
        models = [model_for("legacy-billing") for _ in range(_graph_cache.MAX_ENTRIES + 4)]
        for m in models:
            build_graph_cached(model=m)
        assert len(_graph_cache._entries) <= _graph_cache.MAX_ENTRIES

    def test_key_objects_are_kept_alive_by_the_cache(self):
        """`id()` is unique only among LIVE objects. A cache keyed on a
        collected object's id can hand back a graph closed over a
        completely different model - so entries hold a strong reference to
        their key objects, deliberately."""
        model = model_for("legacy-billing")
        build_graph_cached(model=model)
        entry = next(iter(_graph_cache._entries.values()))
        assert model in entry[1], "the cache must retain its key objects"
