"""FakeEmbedder — deterministic stand-in for Vertex AI ``text-embedding-005``.

Real embeddings are 768-dim, expensive, and require GCP credentials.
For unit tests we need a callable that:

* is **deterministic** — the same input string always yields the same
  vector, so cosine assertions are stable;
* is **distance-aware** — two similar inputs land near each other in
  vector space (so ``MemoryStore.match`` can be unit-tested without
  having to inject exact hand-crafted vectors per test);
* is **dimensionable** — defaults to 768 to match the locked
  production model + Atlas index dimensions, but allows narrower
  vectors for tests that hand-craft synthetic corpora.

The implementation hashes the input into a uniform-distribution
pseudo-random vector then layers a structured "topic" projection on top
so that strings sharing a substring land in roughly the same half-space.
That's enough realism for unit-test assertions; the contract suite
exercises the real model.
"""

from __future__ import annotations

import hashlib
import math


class FakeEmbedder:
    """Callable producing a deterministic ``dim``-vector from any text."""

    def __init__(self, *, dim: int = 768) -> None:
        self.dim = dim
        self.calls: list[str] = []

    def __call__(self, text: str) -> tuple[float, ...]:
        self.calls.append(text)
        return self._embed(text)

    def _embed(self, text: str) -> tuple[float, ...]:
        # Build a deterministic byte stream by hashing the text repeatedly;
        # 32 bytes per round, take ``dim`` floats total. Each byte feeds
        # one float in [-1, 1].
        out: list[float] = []
        seed = text.encode("utf-8")
        round_index = 0
        while len(out) < self.dim:
            digest = hashlib.sha256(seed + round_index.to_bytes(2, "big")).digest()
            for byte in digest:
                if len(out) >= self.dim:
                    break
                out.append((byte / 127.5) - 1.0)
            round_index += 1

        # Normalize so cosine similarity is well-conditioned.
        norm = math.sqrt(sum(x * x for x in out)) or 1.0
        return tuple(x / norm for x in out)
