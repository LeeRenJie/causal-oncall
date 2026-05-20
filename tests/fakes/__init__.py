"""Test-only fakes for boundary modules (Mongo Atlas, Vertex AI embeddings).

These are deep-fake doubles — they mimic the *behavior* of the real
boundary at the seam MemoryStore depends on, not the entire pymongo /
Vertex AI surface. New helpers added here must stay narrow to the
contract the production code uses; growing them into a generic Mongo
emulator would be off-charter (use ``mongomock`` for that case).
"""

from __future__ import annotations

from tests.fakes.gemini import FakeGeminiClient
from tests.fakes.mongo import FakeMongoClient, FakeMongoCollection
from tests.fakes.phoenix import FakePhoenixClient
from tests.fakes.vertex_embedder import FakeEmbedder

__all__ = [
    "FakeEmbedder",
    "FakeGeminiClient",
    "FakeMongoClient",
    "FakeMongoCollection",
    "FakePhoenixClient",
]
