"""TDD spec for Curator (W3-S3).

The Curator's public surface is exactly one method (``synthesize``) and
one report type (``CuratorReport``); these tests pin every observable
behavior the strategist locked in the W3-S3 brief.

All Gemini calls go through a ``FakeGeminiClient``; all Mongo reads go
through ``FakeMongoCollection`` populated from the 10 seed fixtures.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from causal_oncall.curator import (
    Curator,
    CuratorConfig,
    CuratorReport,
    _build_argparser,
    _parse_since,
    _service_from_record,
    _slug,
    main,
)
from causal_oncall.domain.brief import Brief
from causal_oncall.domain.incident_record import IncidentRecord
from causal_oncall.domain.problem_signature import ProblemSignature
from causal_oncall.memory_store import MemoryStore
from tests.conftest import (
    FakeMemoryStore,
    make_brief,
    make_memory_store_config,
    make_signature,
)
from tests.fakes import FakeEmbedder, FakeGeminiClient, FakeMongoCollection

# ---------------------------------------------------------------------- #
# Helpers
# ---------------------------------------------------------------------- #


def _resolved(
    incident_id: str,
    root_cause: str,
    *,
    service: str = "payment-service",
    title: str = "Response time degradation on payment-service",
    severity: str = "PERFORMANCE",
    fix: str = "Bounced the pod and waited for the pool to drain.",
    resolved_at: datetime | None = None,
) -> IncidentRecord:
    sig = ProblemSignature(
        problem_id=incident_id,
        title=title,
        severity=severity,
        affected_entity_ids=(service,),
        affected_entity_types=("SERVICE",),
        opened_at=datetime(2026, 5, 1, tzinfo=UTC),
        fingerprint=f"fp-{incident_id}",
    )
    return IncidentRecord(
        incident_id=incident_id,
        signature=sig,
        brief=make_brief(),
        opened_at=sig.opened_at,
        resolved_at=resolved_at or datetime(2026, 5, 1, 1, tzinfo=UTC),
        confirmed_root_cause_key=root_cause,
        confirmed_fix=fix,
    )


def _curator(
    tmp_path: Path,
    *,
    records: list[IncidentRecord] | None = None,
    active_keys: set[str] | None = None,
    gemini: FakeGeminiClient | None = None,
    config: CuratorConfig | None = None,
) -> tuple[Curator, FakeMemoryStore, FakeGeminiClient]:
    memory = FakeMemoryStore(
        resolved_records=records or [],
        active_few_shot_keys=active_keys or set(),
    )
    gemini = gemini or FakeGeminiClient()
    config = config or CuratorConfig(min_cluster_size=2, few_shot_directory=tmp_path)
    if config.few_shot_directory is None:
        config = CuratorConfig(
            lookback_days=config.lookback_days,
            min_cluster_size=config.min_cluster_size,
            max_examples_per_pattern=config.max_examples_per_pattern,
            gemini_model_id=config.gemini_model_id,
            few_shot_directory=tmp_path,
        )
    return Curator(memory=memory, config=config, gemini_client=gemini), memory, gemini


# ---------------------------------------------------------------------- #
# Curator.synthesize — the one public method
# ---------------------------------------------------------------------- #


def test_synthesize_emits_one_yaml_per_eligible_cluster(tmp_path):
    """A cluster of 3 incidents on payment-service/db_pool_exhaustion -> 1 YAML."""
    records = [
        _resolved("inc-1", "db_pool_exhaustion"),
        _resolved("inc-2", "db_pool_exhaustion"),
        _resolved("inc-3", "db_pool_exhaustion"),
        _resolved("inc-4", "noisy_neighbor", service="api-gateway"),  # singleton, below floor
    ]
    curator, _, _ = _curator(tmp_path, records=records)
    report = curator.synthesize(datetime(2026, 4, 1, tzinfo=UTC))

    assert isinstance(report, CuratorReport)
    assert report.clusters_examined == 1
    assert report.patterns_extracted == 1
    assert len(report.files_written) == 1
    written = report.files_written[0]
    assert written.parent == tmp_path
    assert written.suffix == ".yaml"


def test_synthesize_writes_yaml_with_expected_schema(tmp_path):
    """The written YAML must carry the locked schema fields the next deploy reads."""
    records = [
        _resolved("inc-1", "db_pool_exhaustion"),
        _resolved("inc-2", "db_pool_exhaustion"),
    ]
    curator, _, gemini = _curator(
        tmp_path,
        records=records,
        gemini=FakeGeminiClient(
            responses=[
                {
                    "pattern_summary": "Recurring HikariCP pool saturation post-deploy.",
                    "recommended_action": "Increase pool size and bounce pods.",
                    "diagnostic_dql": "fetch logs | filter contains(content, 'HikariPool')",
                }
            ]
        ),
    )
    curator.synthesize(datetime(2026, 4, 1, tzinfo=UTC))

    yaml_path = next(tmp_path.glob("*.yaml"))
    payload = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == Brief.SCHEMA_VERSION
    assert payload["failure_mode"] == "db_pool_exhaustion"
    assert payload["service"] == "payment-service"
    assert payload["cluster_size"] == 2
    assert "HikariCP" in payload["pattern_summary"]
    assert payload["recommended_action"].startswith("Increase")
    assert payload["diagnostic_dql"].startswith("fetch logs")
    assert len(payload["examples"]) == 2
    assert payload["examples"][0]["incident_id"] in {"inc-1", "inc-2"}
    # Gemini was called exactly once (one cluster).
    assert len(gemini.calls) == 1


def test_synthesize_clusters_separately_by_service_and_failure_mode(tmp_path):
    """Same failure_mode on two different services produces two patterns."""
    records = [
        _resolved("a", "db_pool_exhaustion", service="payment-service"),
        _resolved("b", "db_pool_exhaustion", service="payment-service"),
        _resolved("c", "db_pool_exhaustion", service="checkout-service"),
        _resolved("d", "db_pool_exhaustion", service="checkout-service"),
    ]
    curator, _, _ = _curator(tmp_path, records=records)
    report = curator.synthesize(datetime(2026, 4, 1, tzinfo=UTC))
    assert report.patterns_extracted == 2
    services_emitted = {yaml.safe_load(p.read_text())["service"] for p in tmp_path.glob("*.yaml")}
    assert services_emitted == {"payment-service", "checkout-service"}


def test_synthesize_is_idempotent_on_repeat_runs(tmp_path):
    """Same memory state + same since -> second run writes zero files."""
    records = [
        _resolved("inc-1", "db_pool_exhaustion"),
        _resolved("inc-2", "db_pool_exhaustion"),
    ]
    curator, _, gemini = _curator(tmp_path, records=records)
    first = curator.synthesize(datetime(2026, 4, 1, tzinfo=UTC))
    second = curator.synthesize(datetime(2026, 4, 1, tzinfo=UTC))

    assert first.patterns_extracted == 1
    assert second.patterns_extracted == 0
    # Second run skipped before Gemini was consulted again.
    assert len(gemini.calls) == 1


def test_synthesize_skips_clusters_already_in_active_few_shot_keys(tmp_path):
    """Memory's active keys override the directory check (cron isolation)."""
    records = [
        _resolved("inc-1", "db_pool_exhaustion"),
        _resolved("inc-2", "db_pool_exhaustion"),
    ]
    # Pre-compute the filename stem the Curator will produce so we can
    # seed it as already-active.
    expected_stem = Curator._filename_stem("payment-service", "db_pool_exhaustion", records)
    curator, _, gemini = _curator(tmp_path, records=records, active_keys={expected_stem})

    report = curator.synthesize(datetime(2026, 4, 1, tzinfo=UTC))
    assert report.patterns_extracted == 0
    assert report.clusters_examined == 1  # still examined, just not written
    assert gemini.calls == []


def test_synthesize_skips_records_without_confirmed_root_cause(tmp_path):
    """Open incidents (no root cause) must not enter any cluster."""
    records = [
        _resolved("inc-1", "db_pool_exhaustion"),
        _resolved("inc-2", "db_pool_exhaustion"),
        IncidentRecord(
            incident_id="open-1",
            signature=make_signature(),
            brief=make_brief(),
            opened_at=datetime(2026, 5, 1, tzinfo=UTC),
            resolved_at=None,
            confirmed_root_cause_key=None,
        ),
    ]
    curator, _, _ = _curator(tmp_path, records=records)
    report = curator.synthesize(datetime(2026, 4, 1, tzinfo=UTC))
    # The open record is dropped before clustering; only the real cluster survives.
    assert report.patterns_extracted == 1


def test_synthesize_returns_empty_report_on_empty_window(tmp_path):
    """No resolved records -> zero clusters, zero files, zero cost."""
    curator, _, gemini = _curator(tmp_path, records=[])
    report = curator.synthesize(datetime(2026, 4, 1, tzinfo=UTC))
    assert report.clusters_examined == 0
    assert report.patterns_extracted == 0
    assert report.files_written == ()
    assert report.total_cost_usd == 0.0
    assert gemini.calls == []


def test_synthesize_skips_singleton_clusters(tmp_path):
    """Even with min_cluster_size=2, a lone incident produces no pattern."""
    records = [_resolved("inc-1", "exotic_failure")]
    curator, _, _ = _curator(tmp_path, records=records)
    report = curator.synthesize(datetime(2026, 4, 1, tzinfo=UTC))
    assert report.clusters_examined == 0
    assert report.patterns_extracted == 0


def test_synthesize_caps_examples_per_pattern_at_config_max(tmp_path):
    """max_examples_per_pattern caps the YAML's examples list length."""
    records = [_resolved(f"inc-{i}", "db_pool_exhaustion") for i in range(10)]
    curator, _, _ = _curator(
        tmp_path,
        records=records,
        config=CuratorConfig(
            min_cluster_size=2,
            max_examples_per_pattern=3,
            few_shot_directory=tmp_path,
        ),
    )
    curator.synthesize(datetime(2026, 4, 1, tzinfo=UTC))
    payload = yaml.safe_load(next(tmp_path.glob("*.yaml")).read_text())
    assert len(payload["examples"]) == 3


def test_synthesize_cost_estimate_uses_published_pro_rates(tmp_path):
    """One cluster, 1200 input tokens + 400 output tokens at Gemini Pro rates."""
    records = [
        _resolved("inc-1", "db_pool_exhaustion"),
        _resolved("inc-2", "db_pool_exhaustion"),
    ]
    gemini = FakeGeminiClient(prompt_input_tokens=1200, prompt_output_tokens=400)
    curator, _, _ = _curator(tmp_path, records=records, gemini=gemini)
    report = curator.synthesize(datetime(2026, 4, 1, tzinfo=UTC))
    # 1200 * $2/M + 400 * $12/M = $0.0024 + $0.0048 = $0.0072
    assert report.total_cost_usd == pytest.approx(1200 * 2e-6 + 400 * 12e-6)


def test_synthesize_passes_since_through_to_memory(tmp_path):
    """The Curator must forward ``since`` to ``memory.list_resolved_since`` verbatim."""
    curator, memory, _ = _curator(tmp_path, records=[])
    target = datetime(2026, 3, 17, 11, 0, tzinfo=UTC)
    curator.synthesize(target)
    assert memory.list_resolved_since_calls == [target]


def test_synthesize_prompt_carries_service_and_failure_mode(tmp_path):
    """The prompt sent to Gemini must contain the cluster facts the model needs."""
    records = [
        _resolved("inc-1", "deploy_regression", service="checkout-service"),
        _resolved("inc-2", "deploy_regression", service="checkout-service"),
    ]
    curator, _, gemini = _curator(tmp_path, records=records)
    curator.synthesize(datetime(2026, 4, 1, tzinfo=UTC))
    prompt = gemini.calls[0]
    assert "checkout-service" in prompt
    assert "deploy_regression" in prompt
    assert "cluster_size: 2" in prompt


def test_synthesize_creates_target_directory_if_missing(tmp_path):
    """``few_shot_directory`` need not pre-exist; the Curator mkdirs."""
    target = tmp_path / "nested" / "patterns"
    config = CuratorConfig(min_cluster_size=2, few_shot_directory=target)
    memory = FakeMemoryStore(
        resolved_records=[
            _resolved("inc-1", "db_pool_exhaustion"),
            _resolved("inc-2", "db_pool_exhaustion"),
        ]
    )
    curator = Curator(memory=memory, config=config, gemini_client=FakeGeminiClient())
    curator.synthesize(datetime(2026, 4, 1, tzinfo=UTC))
    assert target.exists()
    assert list(target.glob("*.yaml"))


def test_synthesize_filename_includes_sha8_of_sorted_incident_ids(tmp_path):
    """The stem encodes a deterministic SHA-8 over the cluster's sorted ids."""
    import hashlib

    records = [
        _resolved("z-incident", "db_pool_exhaustion"),
        _resolved("a-incident", "db_pool_exhaustion"),
    ]
    expected_digest = hashlib.sha256(b"a-incident|z-incident").hexdigest()[:8]
    curator, _, _ = _curator(tmp_path, records=records)
    report = curator.synthesize(datetime(2026, 4, 1, tzinfo=UTC))
    assert expected_digest in report.files_written[0].stem


def test_synthesize_files_written_are_sorted(tmp_path):
    """CuratorReport.files_written must be sorted for deterministic logging."""
    records = [
        _resolved("a1", "db_pool_exhaustion", service="payment-service"),
        _resolved("a2", "db_pool_exhaustion", service="payment-service"),
        _resolved("b1", "deploy_regression", service="checkout-service"),
        _resolved("b2", "deploy_regression", service="checkout-service"),
    ]
    curator, _, _ = _curator(tmp_path, records=records)
    report = curator.synthesize(datetime(2026, 4, 1, tzinfo=UTC))
    paths = list(report.files_written)
    assert paths == sorted(paths)


def test_synthesize_skips_when_target_file_exists_even_without_active_key(tmp_path):
    """File-existence check is the second leg of dedup, distinct from active_keys."""
    records = [
        _resolved("inc-1", "db_pool_exhaustion"),
        _resolved("inc-2", "db_pool_exhaustion"),
    ]
    expected_stem = Curator._filename_stem("payment-service", "db_pool_exhaustion", records)
    # Pre-create the target file without recording it as active.
    (tmp_path / f"{expected_stem}.yaml").write_text("pre-existing\n", encoding="utf-8")

    curator, _, gemini = _curator(tmp_path, records=records)
    report = curator.synthesize(datetime(2026, 4, 1, tzinfo=UTC))
    assert report.patterns_extracted == 0
    assert gemini.calls == []
    # The pre-existing file is untouched.
    assert (tmp_path / f"{expected_stem}.yaml").read_text() == "pre-existing\n"


# ---------------------------------------------------------------------- #
# Module-private helpers
# ---------------------------------------------------------------------- #


def test_slug_lowercases_and_collapses_separators():
    assert _slug("Payment Service v2") == "payment_service_v2"
    assert _slug("DB Pool / Exhaustion!") == "db_pool_exhaustion"


def test_slug_returns_unknown_on_empty_input():
    assert _slug("") == "unknown"
    assert _slug("!!!") == "unknown"


def test_service_from_record_prefers_entity_id():
    rec = _resolved("inc-1", "db_pool_exhaustion", service="SERVICE-ABC")
    assert _service_from_record(rec) == "SERVICE-ABC"


def test_service_from_record_falls_back_to_title_first_token():
    """When no entity ids, derive service from the signature title."""
    sig = ProblemSignature(
        problem_id="inc-1",
        title="payment-service is degraded",
        severity="PERFORMANCE",
        affected_entity_ids=(),  # no entity id available
        affected_entity_types=(),
        opened_at=datetime(2026, 5, 1, tzinfo=UTC),
        fingerprint="fp-inc-1",
    )
    rec = IncidentRecord(
        incident_id="inc-1",
        signature=sig,
        brief=make_brief(),
        opened_at=sig.opened_at,
        resolved_at=datetime(2026, 5, 1, 1, tzinfo=UTC),
        confirmed_root_cause_key="db_pool_exhaustion",
        confirmed_fix="...",
    )
    assert _service_from_record(rec) == "payment-service"


def test_service_from_record_returns_unknown_on_blank_title_and_no_entities():
    sig = ProblemSignature(
        problem_id="inc-1",
        title="",
        severity="PERFORMANCE",
        affected_entity_ids=(),
        affected_entity_types=(),
        opened_at=datetime(2026, 5, 1, tzinfo=UTC),
        fingerprint="fp-inc-1",
    )
    rec = IncidentRecord(
        incident_id="inc-1",
        signature=sig,
        brief=make_brief(),
        opened_at=sig.opened_at,
        resolved_at=datetime(2026, 5, 1, 1, tzinfo=UTC),
        confirmed_root_cause_key="db_pool_exhaustion",
        confirmed_fix="...",
    )
    assert _service_from_record(rec) == "unknown"


def test_service_from_record_skips_empty_entity_id_string():
    """An empty-string entity id (defensive) is skipped so title-fallback fires."""
    sig = ProblemSignature(
        problem_id="inc-1",
        title="payment-service down",
        severity="PERFORMANCE",
        affected_entity_ids=("",),
        affected_entity_types=("SERVICE",),
        opened_at=datetime(2026, 5, 1, tzinfo=UTC),
        fingerprint="fp-inc-1",
    )
    rec = IncidentRecord(
        incident_id="inc-1",
        signature=sig,
        brief=make_brief(),
        opened_at=sig.opened_at,
        resolved_at=datetime(2026, 5, 1, 1, tzinfo=UTC),
        confirmed_root_cause_key="db_pool_exhaustion",
        confirmed_fix="...",
    )
    assert _service_from_record(rec) == "payment-service"


# ---------------------------------------------------------------------- #
# --since parsing
# ---------------------------------------------------------------------- #


def test_parse_since_days():
    now = datetime.now(UTC)
    parsed = _parse_since("7d")
    delta = now - parsed
    assert timedelta(days=6, hours=23) <= delta <= timedelta(days=7, minutes=1)


def test_parse_since_hours():
    now = datetime.now(UTC)
    parsed = _parse_since("24h")
    delta = now - parsed
    assert timedelta(hours=23, minutes=59) <= delta <= timedelta(hours=24, minutes=1)


def test_parse_since_minutes():
    now = datetime.now(UTC)
    parsed = _parse_since("30m")
    delta = now - parsed
    assert timedelta(minutes=29) <= delta <= timedelta(minutes=31)


def test_parse_since_is_case_insensitive():
    parsed = _parse_since("7D")
    assert parsed.tzinfo is UTC


def test_parse_since_rejects_garbage():
    import argparse

    with pytest.raises(argparse.ArgumentTypeError, match="--since"):
        _parse_since("yesterday")


def test_build_argparser_defaults_to_none_since():
    parser = _build_argparser()
    ns = parser.parse_args([])
    assert ns.since is None
    assert ns.few_shot_dir is None


def test_build_argparser_accepts_since_and_dir(tmp_path):
    parser = _build_argparser()
    ns = parser.parse_args(["--since", "1d", "--few-shot-dir", str(tmp_path)])
    assert ns.since is not None
    assert ns.few_shot_dir == tmp_path


# ---------------------------------------------------------------------- #
# CLI entry point — main()
# ---------------------------------------------------------------------- #


def test_main_runs_end_to_end_against_real_memory_store(tmp_path, capsys):
    """The CLI builds a Curator against a real MemoryStore and prints a summary."""
    collection = FakeMongoCollection()
    embedder = FakeEmbedder(dim=8)
    cfg = make_memory_store_config(dim=8, few_shot_directory=tmp_path)
    store = MemoryStore(cfg, embedder=embedder, collection=collection)

    # Seed two resolved incidents on payment-service/db_pool_exhaustion.
    base = datetime(2026, 5, 10, tzinfo=UTC)
    for i in range(2):
        collection.insert_one(
            {
                "incident_id": f"inc-{i}",
                "problem_signature_hash": f"fp-{i}",
                "brief_hash": f"bh-{i}",
                "embedding": [0.1] * 8,
                "opened_at": base,
                "resolved_at": base + timedelta(hours=1),
                "confirmed_root_cause_key": "db_pool_exhaustion",
                "confirmed_fix": f"Fix #{i}",
                "signature": {
                    "problem_id": f"inc-{i}",
                    "title": "Response time degradation on payment-service",
                    "severity": "PERFORMANCE",
                    "affected_entity_ids": ["payment-service"],
                    "affected_entity_types": ["SERVICE"],
                    "opened_at": base,
                    "fingerprint": f"fp-{i}",
                },
                "brief_markdown": "# Prior brief\n",
            }
        )

    exit_code = main(
        ["--since", "30d", "--few-shot-dir", str(tmp_path)],
        memory=store,
        gemini_client=FakeGeminiClient(),
    )

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "examined 1 cluster(s)" in captured.out
    assert "wrote 1 pattern file(s)" in captured.out
    assert any(tmp_path.glob("*.yaml"))


def test_main_defaults_since_to_config_lookback_when_not_passed(tmp_path, capsys):
    """No --since -> lookback_days * 1d window."""
    collection = FakeMongoCollection()
    embedder = FakeEmbedder(dim=8)
    cfg = make_memory_store_config(dim=8, few_shot_directory=tmp_path)
    store = MemoryStore(cfg, embedder=embedder, collection=collection)
    exit_code = main(
        ["--few-shot-dir", str(tmp_path)],
        memory=store,
        gemini_client=FakeGeminiClient(),
    )
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "examined 0 cluster(s)" in captured.out


# ---------------------------------------------------------------------- #
# Integration with real MemoryStore (via fakes) — the production read path
# ---------------------------------------------------------------------- #


def test_curator_against_real_memory_store_round_trip(tmp_path):
    """Wire a real MemoryStore (against fakes) and verify the full path."""
    collection = FakeMongoCollection()
    embedder = FakeEmbedder(dim=8)
    cfg = make_memory_store_config(dim=8, few_shot_directory=tmp_path)
    store = MemoryStore(cfg, embedder=embedder, collection=collection)

    base = datetime(2026, 5, 10, tzinfo=UTC)
    # Two resolved incidents on the same (service, failure_mode)
    for i in range(2):
        collection.insert_one(
            {
                "incident_id": f"real-{i}",
                "problem_signature_hash": f"fp-real-{i}",
                "brief_hash": f"bh-real-{i}",
                "embedding": [0.1] * 8,
                "opened_at": base,
                "resolved_at": base + timedelta(hours=1),
                "confirmed_root_cause_key": "deploy_regression",
                "confirmed_fix": "Rolled back to vN-1",
                "signature": {
                    "problem_id": f"real-{i}",
                    "title": "Failure rate increase on checkout-service",
                    "severity": "ERROR",
                    "affected_entity_ids": ["checkout-service"],
                    "affected_entity_types": ["SERVICE"],
                    "opened_at": base,
                    "fingerprint": f"fp-real-{i}",
                },
                "brief_markdown": "# Prior\n",
            }
        )
    # Also seed an open one that must be skipped.
    collection.insert_one(
        {
            "incident_id": "open-1",
            "problem_signature_hash": "fp-open",
            "brief_hash": "bh-open",
            "embedding": [0.1] * 8,
            "opened_at": base,
            "resolved_at": None,
            "confirmed_root_cause_key": None,
            "confirmed_fix": "",
            "signature": {
                "problem_id": "open-1",
                "title": "Still ongoing",
                "severity": "ERROR",
                "affected_entity_ids": ["checkout-service"],
                "affected_entity_types": ["SERVICE"],
                "opened_at": base,
                "fingerprint": "fp-open",
            },
            "brief_markdown": "# Pending\n",
        }
    )

    curator = Curator(
        memory=store,
        config=CuratorConfig(min_cluster_size=2, few_shot_directory=tmp_path),
        gemini_client=FakeGeminiClient(),
    )
    report = curator.synthesize(base - timedelta(days=1))
    assert report.patterns_extracted == 1


# ---------------------------------------------------------------------- #
# Curator constructor surface
# ---------------------------------------------------------------------- #


def test_curator_defaults_config_when_omitted(tmp_path):
    """The Curator(memory=...) constructor matches app.py's wiring."""
    memory = FakeMemoryStore()
    curator = Curator(memory=memory)
    assert curator._config.lookback_days == 7
    assert curator._config.min_cluster_size == 2


def test_curator_default_gemini_client_runs_through_the_adk_runtime(tmp_path):
    """No gemini_client passed -> the ADK-runtime-backed synthesis client is wired.

    Compliance: production pattern synthesis goes through an ADK LlmAgent +
    Runner (``AdkPatternSynthesisClient``), not a direct google.genai call.
    """
    from causal_oncall.adk_runtime import AdkPatternSynthesisClient

    memory = FakeMemoryStore()
    curator = Curator(memory=memory)
    assert isinstance(curator._gemini, AdkPatternSynthesisClient)


def test_synthesize_falls_back_to_memory_store_few_shot_dir(tmp_path):
    """When CuratorConfig.few_shot_directory is None, defer to MemoryStore."""
    collection = FakeMongoCollection()
    embedder = FakeEmbedder(dim=8)
    cfg = make_memory_store_config(dim=8, few_shot_directory=tmp_path)
    store = MemoryStore(cfg, embedder=embedder, collection=collection)

    base = datetime(2026, 5, 10, tzinfo=UTC)
    for i in range(2):
        collection.insert_one(
            {
                "incident_id": f"fb-{i}",
                "problem_signature_hash": f"fp-fb-{i}",
                "brief_hash": f"bh-fb-{i}",
                "embedding": [0.1] * 8,
                "opened_at": base,
                "resolved_at": base + timedelta(hours=1),
                "confirmed_root_cause_key": "db_pool_exhaustion",
                "confirmed_fix": "...",
                "signature": {
                    "problem_id": f"fb-{i}",
                    "title": "Response time degradation on payment-service",
                    "severity": "PERFORMANCE",
                    "affected_entity_ids": ["payment-service"],
                    "affected_entity_types": ["SERVICE"],
                    "opened_at": base,
                    "fingerprint": f"fp-fb-{i}",
                },
                "brief_markdown": "# Prior\n",
            }
        )

    # Note: CuratorConfig.few_shot_directory is None on purpose.
    curator = Curator(
        memory=store,
        config=CuratorConfig(min_cluster_size=2, few_shot_directory=None),
        gemini_client=FakeGeminiClient(),
    )
    report = curator.synthesize(base - timedelta(days=1))
    assert report.patterns_extracted == 1
    # File landed in the MemoryStore's configured directory (tmp_path),
    # not the in-package default.
    assert report.files_written[0].parent == tmp_path


# ---------------------------------------------------------------------- #
# Schema bump on seed JSON (W3-S2 postmortem flag #3)
# ---------------------------------------------------------------------- #


def test_seed_json_carries_schema_version_two():
    """The W3-S2 postmortem flagged the seed JSON for a schema_version bump."""
    seed_path = (
        Path(__file__).resolve().parent.parent
        / "fixtures"
        / "memory_seeds"
        / "seed_10_resolved.json"
    )
    raw = json.loads(seed_path.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    assert raw["schema_version"] == 2
    assert isinstance(raw["records"], list)
    assert len(raw["records"]) == 10
    # Each record carries a 'service' so the Curator can cluster by it.
    for record in raw["records"]:
        assert "service" in record
