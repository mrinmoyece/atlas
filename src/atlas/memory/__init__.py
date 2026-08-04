"""Four-tier memory with a measurement harness.

The headline claim of this package is not "Atlas has memory" - every agent
demo claims that. It is: **Atlas measures whether memory helps**, via an
A/B harness that runs the same golden set with memory on and off and
reports the delta in F1, cost and steps. See `atlas.memory.measure` and
docs/MEMORY.md.
"""

from atlas.memory.embeddings import HashingEmbedder, cosine
from atlas.memory.hub import MemoryHub, RecallResult
from atlas.memory.tiers import (
    EpisodicMemory,
    MemoryRecord,
    ProceduralMemory,
    SemanticMemory,
    StrategyStats,
)

__all__ = [
    "HashingEmbedder",
    "cosine",
    "MemoryHub",
    "RecallResult",
    "EpisodicMemory",
    "SemanticMemory",
    "ProceduralMemory",
    "MemoryRecord",
    "StrategyStats",
]
