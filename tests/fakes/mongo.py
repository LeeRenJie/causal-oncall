"""FakeMongoCollection — in-process stand-in for Atlas's pymongo surface.

Implements *only* the four operations ``MemoryStore`` calls:

* ``update_one(filter, update, upsert=True)`` — supports ``$set`` and
  ``$setOnInsert`` operator dicts; the upsert key is the literal
  ``filter`` dict.
* ``aggregate(pipeline)`` — special-cases the ``$vectorSearch`` stage
  by computing cosine similarity in Python over the stored docs,
  filtering on ``$vectorSearch.filter``, sorting descending by score,
  then applying the ``$addFields`` stage to surface ``score``.
* ``count_documents(filter)`` — equality matching only.
* ``find_one(filter)`` — equality matching only.

Anything Atlas-specific outside this surface (transactions, change
streams, sharded reads) is intentionally not modelled; if the
production code grows a new boundary call, *this fake grows too* and
the new behavior gets its own contract test.

A ``FakeMongoClient`` is provided so test wiring can mirror the
``client[db][coll]`` indexing pattern the real pymongo MongoClient uses;
its only job is to hand out ``FakeMongoCollection`` instances on first
indexing.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from typing import Any


def _cosine(a: Iterable[float], b: Iterable[float]) -> float:
    a_list = list(a)
    b_list = list(b)
    dot = sum(x * y for x, y in zip(a_list, b_list, strict=False))
    na = math.sqrt(sum(x * x for x in a_list))
    nb = math.sqrt(sum(y * y for y in b_list))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _matches_filter(doc: dict, filter_doc: dict) -> bool:
    """Subset of Mongo filter language: equality + ``$exists`` + ``$ne`` + ``$gte``."""
    for key, expected in filter_doc.items():
        actual = doc.get(key)
        if isinstance(expected, dict):
            for op, op_arg in expected.items():
                if op == "$exists":
                    present = key in doc and doc[key] is not None
                    if bool(op_arg) != present:
                        return False
                elif op == "$ne":
                    if actual == op_arg:
                        return False
                elif op == "$gte":
                    if actual is None or actual < op_arg:
                        return False
                else:  # pragma: no cover  # only the operators MemoryStore uses are supported
                    raise NotImplementedError(f"FakeMongo operator {op!r} not supported")
        else:
            if actual != expected:
                return False
    return True


class FakeMongoCollection:
    """Behavioural double for the operations ``MemoryStore`` calls."""

    def __init__(self) -> None:
        # Storage is keyed by an auto-incrementing internal id so
        # multiple docs with identical content still round-trip.
        self._docs: list[dict] = []
        # Tracks what was passed to the most recent ``aggregate`` call so
        # tests can pin the wire-shape contract (index name, queryVector
        # length, numCandidates, filter).
        self.last_aggregate_pipeline: list[dict] | None = None
        # Same for ``update_one`` — useful for asserting the dedup
        # filter contains both signature_hash and brief_hash.
        self.last_update_one: tuple[dict, dict, bool] | None = None

    # ---- write surface --------------------------------------------------

    def insert_one(self, doc: dict) -> None:
        """Test setup helper: shove a row in without going through update_one."""
        self._docs.append(dict(doc))

    def update_one(self, filter_doc: dict, update: dict, *, upsert: bool = False) -> None:
        self.last_update_one = (dict(filter_doc), dict(update), upsert)
        set_fields = update.get("$set", {})
        set_on_insert_fields = update.get("$setOnInsert", {})

        for existing in self._docs:
            if _matches_filter(existing, filter_doc):
                for k, v in set_fields.items():
                    existing[k] = v
                return

        if not upsert:
            return

        # Upsert path: seed from the filter (so the dedup keys are
        # always present on the new doc) then layer $set + $setOnInsert.
        new_doc: dict[str, Any] = {}
        for k, v in filter_doc.items():
            if not isinstance(v, dict):
                new_doc[k] = v
        new_doc.update(set_fields)
        new_doc.update(set_on_insert_fields)
        self._docs.append(new_doc)

    # ---- read surface ---------------------------------------------------

    def find_one(self, filter_doc: dict | None = None) -> dict | None:
        for doc in self._docs:
            if filter_doc is None or _matches_filter(doc, filter_doc):
                return doc
        return None

    def find(
        self,
        filter_doc: dict | None = None,
        *,
        sort: list[tuple[str, int]] | None = None,
    ) -> list[dict]:
        """Return all documents matching ``filter_doc`` with optional sort.

        Supports the same filter sub-language as :meth:`find_one` plus
        ``$gte`` for the W3-S3 ``list_resolved_since`` query. ``sort`` is
        a list of ``(field, direction)`` tuples where direction is 1 for
        ascending and -1 for descending.
        """
        matches = [
            doc for doc in self._docs if filter_doc is None or _matches_filter(doc, filter_doc)
        ]
        if sort:
            for field, direction in reversed(sort):
                matches.sort(
                    key=lambda d, f=field: (d.get(f) is None, d.get(f)),
                    reverse=(direction < 0),
                )
        return matches

    def count_documents(self, filter_doc: dict) -> int:
        return sum(1 for d in self._docs if _matches_filter(d, filter_doc))

    def aggregate(self, pipeline: list[dict]) -> list[dict]:
        """Emulate the ``$vectorSearch`` + ``$addFields`` pipeline shape.

        Anything outside that two-stage shape is unsupported by design —
        the production code only issues this pipeline. Other pipelines
        would mean the production code grew a new path that needs its
        own contract test.
        """
        self.last_aggregate_pipeline = [dict(stage) for stage in pipeline]
        if not pipeline or "$vectorSearch" not in pipeline[0]:
            raise NotImplementedError("FakeMongo only emulates $vectorSearch pipelines")

        vs = pipeline[0]["$vectorSearch"]
        path = vs.get("path", "embedding")
        query_vec = list(vs.get("queryVector", []))
        limit = int(vs.get("limit", 10))
        vs_filter = vs.get("filter", {}) or {}

        scored: list[tuple[float, dict]] = []
        for doc in self._docs:
            if not _matches_filter(doc, vs_filter):
                continue
            emb = doc.get(path)
            if not emb:
                continue
            score = _cosine(query_vec, emb)
            scored.append((score, doc))

        scored.sort(key=lambda pair: pair[0], reverse=True)
        scored = scored[:limit]

        results: list[dict] = []
        for score, doc in scored:
            out = dict(doc)
            # $addFields stage in the production pipeline writes ``score``
            # from ``vectorSearchScore`` meta. Tests don't need to walk
            # the rest of the pipeline; we just project the field.
            out["score"] = score
            results.append(out)
        return results


class FakeMongoClient:
    """Mirrors ``client[db][coll]`` indexing without the network."""

    def __init__(self) -> None:
        self._dbs: dict[str, dict[str, FakeMongoCollection]] = {}

    def __getitem__(self, db_name: str) -> _FakeDatabase:
        return _FakeDatabase(self._dbs.setdefault(db_name, {}))


class _FakeDatabase:
    """Indexable container of collections, returned by ``FakeMongoClient[db]``."""

    def __init__(self, collections: dict[str, FakeMongoCollection]) -> None:
        self._collections = collections

    def __getitem__(self, coll_name: str) -> FakeMongoCollection:
        return self._collections.setdefault(coll_name, FakeMongoCollection())
