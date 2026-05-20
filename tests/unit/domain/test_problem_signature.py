"""TDD spec for ProblemSignature.

Behaviors driven here:
  * normalization is deterministic + idempotent on the canonical payload,
  * the embedding-text format is stable across runs,
  * affected-entity ids and types are deduped + ordered so two slightly-
    different orderings of the same problem yield identical fingerprints.
"""

from __future__ import annotations

from datetime import UTC, datetime

from causal_oncall.domain.problem_signature import ProblemSignature

_VALID_PAYLOAD = {
    "problemId": "-1234567890123456789_v2",
    "title": "Response time degradation",
    "severityLevel": "PERFORMANCE",
    "startTime": "2026-05-17T09:30:00Z",
    "affectedEntities": [
        {"entityId": {"id": "SERVICE-ABC"}, "type": "SERVICE"},
        {"entityId": {"id": "SERVICE-DEF"}, "type": "SERVICE"},
    ],
}


def test_normalization_is_idempotent_on_canonical_payload():
    a = ProblemSignature.from_dynatrace_payload(_VALID_PAYLOAD)
    b = ProblemSignature.from_dynatrace_payload(_VALID_PAYLOAD)
    assert a == b
    assert a.fingerprint == b.fingerprint
    assert a.fingerprint != ""


def test_affected_entities_are_sorted_deterministically():
    shuffled = dict(_VALID_PAYLOAD)
    shuffled["affectedEntities"] = list(reversed(_VALID_PAYLOAD["affectedEntities"]))

    a = ProblemSignature.from_dynatrace_payload(_VALID_PAYLOAD)
    b = ProblemSignature.from_dynatrace_payload(shuffled)
    assert a.affected_entity_ids == b.affected_entity_ids
    assert a.fingerprint == b.fingerprint


def test_entity_types_are_deduped():
    dup = dict(_VALID_PAYLOAD)
    dup["affectedEntities"] = [
        {"entityId": {"id": "SERVICE-ABC"}, "type": "SERVICE"},
        {"entityId": {"id": "SERVICE-DEF"}, "type": "SERVICE"},
        {"entityId": {"id": "PG-1"}, "type": "PROCESS_GROUP"},
    ]
    sig = ProblemSignature.from_dynatrace_payload(dup)
    assert sig.affected_entity_types == ("PROCESS_GROUP", "SERVICE")


def test_to_embedding_text_includes_title_severity_and_entity_types():
    sig = ProblemSignature.from_dynatrace_payload(_VALID_PAYLOAD)
    text = sig.to_embedding_text()
    assert "Response time degradation" in text
    assert "PERFORMANCE" in text
    assert "SERVICE" in text


def test_opened_at_is_parsed_as_utc():
    sig = ProblemSignature.from_dynatrace_payload(_VALID_PAYLOAD)
    assert sig.opened_at == datetime(2026, 5, 17, 9, 30, tzinfo=UTC)


def test_fingerprint_changes_when_severity_changes():
    a = ProblemSignature.from_dynatrace_payload(_VALID_PAYLOAD)
    other = dict(_VALID_PAYLOAD)
    other["severityLevel"] = "ERROR"
    b = ProblemSignature.from_dynatrace_payload(other)
    assert a.fingerprint != b.fingerprint


def test_signature_is_hashable_so_it_can_key_caches():
    sig = ProblemSignature.from_dynatrace_payload(_VALID_PAYLOAD)
    {sig: "ok"}  # noqa: B018 — exercise hashability
