"""Dependency-free deterministic embeddings.

Why not sentence-transformers or an embedding API? Three reasons, in order
of importance:

  1. **Determinism.** Evals must be reproducible. A hosted embedding model
     can change under you and silently move your recall numbers.
  2. **Offline CI.** No model download, no key, no network in tests.
  3. **Honesty.** The retrieval *interface* is what matters architecturally;
     the vectoriser behind it is swappable. Pretending a 384-dim MiniLM is
     "production-grade semantic memory" would be no more true than this.

The implementation is the hashing trick (feature hashing) over word
unigrams + bigrams, L2-normalised, with sublinear term-frequency damping.
That gives genuine vector-space behaviour - cosine similarity, dense
vectors, dimensionality reduction - with zero dependencies. It captures
lexical overlap well and paraphrase poorly; swapping in real embeddings
behind `Embedder` is a one-file change, documented in LIMITATIONS.md.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence
from typing import Protocol

_WORD = re.compile(r"[a-z0-9_]+")
_STOP = frozenset(
    "a an and are as at be by for from has have in is it of on or that the "
    "this to was with you your we our".split()
)

DEFAULT_DIM = 256


class Embedder(Protocol):
    dim: int

    def embed(self, text: str) -> list[float]: ...


def _tokens(text: str) -> list[str]:
    words = [w for w in _WORD.findall(text.lower()) if w not in _STOP and len(w) > 1]
    # bigrams capture a little word order, which pure bag-of-words loses
    bigrams = [f"{a}_{b}" for a, b in zip(words, words[1:], strict=False)]
    return words + bigrams


def _bucket(token: str, dim: int) -> tuple[int, float]:
    """Hash a token to (index, sign). The sign trick reduces collision bias:
    colliding features cancel on average instead of always adding."""
    digest = hashlib.blake2b(token.encode(), digest_size=8).digest()
    value = int.from_bytes(digest, "big")
    return value % dim, 1.0 if (value >> 63) & 1 else -1.0


class HashingEmbedder:
    """Feature-hashing vectoriser with sublinear TF and L2 normalisation."""

    def __init__(self, dim: int = DEFAULT_DIM) -> None:
        self.dim = dim

    def embed(self, text: str) -> list[float]:
        counts: dict[str, int] = {}
        for token in _tokens(text):
            counts[token] = counts.get(token, 0) + 1
        vec = [0.0] * self.dim
        for token, count in counts.items():
            idx, sign = _bucket(token, self.dim)
            # 1 + log(tf) damping: the 10th mention of "sql" should not
            # outweigh the presence of a rarer, more discriminating term
            vec[idx] += sign * (1.0 + math.log(count))
        norm = math.sqrt(sum(v * v for v in vec))
        if norm == 0:
            return vec
        return [v / norm for v in vec]


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Both vectors are already L2-normalised, so this is a dot product.
    Kept as a named function because the call sites read better and because
    swapping in un-normalised embeddings later shouldn't break callers."""
    return sum(x * y for x, y in zip(a, b, strict=False))
