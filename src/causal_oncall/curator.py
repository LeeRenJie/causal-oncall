"""Curator — weekly batch synthesis of higher-order patterns from Mongo.

The Curator is the slowest-moving piece of the learning loop. Once a
week it sweeps every resolved incident in the configured lookback
window, clusters them by ``(service, failure_mode)``, asks Gemini 2.5
Pro to synthesize a few-shot pattern from each cluster, and writes one
YAML file per pattern into the in-package few-shot bank that ships with
the next deploy.

Public surface — locked to two types per the PLAN W3-S3 brief:

* :class:`Curator` — the agent itself; one public method.
* :class:`CuratorReport` — what one run produced; suitable for the
  Slack weekly-digest message.

Everything else (clustering, prompt construction, Gemini call, YAML
emission, cost estimation, idempotent SHA-keyed file naming) is hidden
plumbing. The Curator never runs in the webhook request path; it ships
as a standalone CLI driven from cron (``python -m causal_oncall.curator
--since 7d``).

CLI usage::

    python -m causal_oncall.curator --since 7d
    python -m causal_oncall.curator --since 24h
    python -m causal_oncall.curator --since 30d --few-shot-dir ./tmp_patterns

The ``--since`` flag accepts ``Nd`` (days), ``Nh`` (hours), and ``Nm``
(minutes); the lookback is computed against ``datetime.now(UTC)``.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

import yaml

from causal_oncall.domain.brief import Brief
from causal_oncall.domain.incident_record import IncidentRecord
from causal_oncall.memory_store import MemoryStore, MemoryStoreConfig

# Gemini 2.5 Pro pricing on Vertex AI (verified UNIQUE_IDEA §"Unit economics"):
# $2 / 1M input tokens; $12 / 1M output tokens. The Curator's per-cluster
# cost is exposed in CuratorReport.total_cost_usd for the COST-LOG row.
_GEMINI_PRO_INPUT_PER_TOKEN_USD = 2.0 / 1_000_000
_GEMINI_PRO_OUTPUT_PER_TOKEN_USD = 12.0 / 1_000_000


class GeminiSynthesisClient(Protocol):
    """Narrow protocol the Curator depends on for pattern synthesis.

    The production wiring instantiates a :class:`_VertexGeminiClient` (lazy
    Vertex AI import). Tests inject ``FakeGeminiClient`` (tests/fakes/).
    """

    def synthesize_pattern(self, prompt: str) -> dict[str, Any]: ...

    def token_counts(self) -> tuple[int, int]: ...


@dataclass(frozen=True, slots=True)
class CuratorConfig:
    """Tunables for one weekly batch run.

    Attributes:
        lookback_days: Default lookback when the caller doesn't specify
            an explicit ``since`` timestamp. CLI overrides via ``--since``.
        min_cluster_size: Minimum incident count per ``(service, failure_mode)``
            cluster before a pattern is synthesized. Singletons are noise.
        max_examples_per_pattern: Cap on incident exemplars included in
            each YAML's ``examples`` list. Keeps the few-shot prompt short.
        gemini_model_id: Vertex AI model used for synthesis. Locked to a
            Pro tier per PLAN W3-S3 (high-stakes cluster summarization).
        few_shot_directory: Where YAML patterns are written. ``None`` means
            "use the MemoryStore's configured directory" (the in-package
            ``_few_shot/`` dir by default).
    """

    lookback_days: int = 7
    min_cluster_size: int = 2
    max_examples_per_pattern: int = 5
    gemini_model_id: str = "gemini-2.5-pro"
    few_shot_directory: Path | None = None


@dataclass(frozen=True, slots=True)
class CuratorReport:
    """Summary of one Curator run.

    Attributes:
        run_at: UTC timestamp the batch completed.
        clusters_examined: How many ``(service, failure_mode)`` clusters
            cleared ``min_cluster_size`` and were considered for synthesis.
        patterns_extracted: How many patterns were synthesized into new
            YAML files this run (skipped duplicates do not count).
        files_written: Absolute paths of YAML files written this run, in
            deterministic order (sorted by stem).
        total_cost_usd: Estimated Vertex AI Gemini Pro spend for this
            batch based on per-call token counts × published per-token
            rates. Surfaces in the weekly Slack digest + COST-LOG.
    """

    run_at: datetime
    clusters_examined: int
    patterns_extracted: int
    files_written: tuple[Path, ...] = field(default_factory=tuple)
    total_cost_usd: float = 0.0


class Curator:
    """Off-line pattern miner. Runs weekly, never in the request path."""

    def __init__(
        self,
        *,
        memory: MemoryStore,
        config: CuratorConfig | None = None,
        gemini_client: GeminiSynthesisClient | None = None,
    ) -> None:
        self._memory = memory
        self._config = config or CuratorConfig()
        # Gemini client is dependency-injectable so unit tests stay
        # hermetic. Production wiring constructs the real Vertex client
        # lazily on the first ``synthesize`` call.
        self._gemini: GeminiSynthesisClient = gemini_client or _LazyVertexGeminiClient(
            model_id=self._config.gemini_model_id
        )

    # ------------------------------------------------------------------ #
    # Public surface — one method. Adding a second requires a PLAN
    # amendment per the deep-module rule.
    # ------------------------------------------------------------------ #

    def synthesize(self, since: datetime) -> CuratorReport:
        """Cluster resolved incidents since ``since`` and emit few-shot YAMLs.

        Idempotent: re-running with the same ``since`` over the same
        Mongo state produces zero new files because each pattern's
        filename embeds a SHA-8 of its sorted input incident ids.

        The Curator is the only writer into the few-shot directory, so
        skipping when a file with the target name already exists is the
        whole dedup story.
        """
        records = self._memory.list_resolved_since(since)
        clusters = self._cluster_records(records)
        eligible = {
            key: items
            for key, items in clusters.items()
            if len(items) >= self._config.min_cluster_size
        }

        active_keys = self._memory.list_active_few_shot_keys()
        directory = self._resolve_few_shot_dir()
        directory.mkdir(parents=True, exist_ok=True)

        written: list[Path] = []
        total_input_tokens = 0
        total_output_tokens = 0

        for service, failure_mode in sorted(eligible.keys()):
            cluster = eligible[(service, failure_mode)]
            filename_stem = self._filename_stem(service, failure_mode, cluster)
            if filename_stem in active_keys:
                continue
            target = directory / f"{filename_stem}.yaml"
            if target.exists():
                # Mirror of ``active_keys`` for the case where the
                # MemoryStore's few-shot dir is configured separately
                # from where we write (tests + cron isolation).
                continue

            prompt = self._build_prompt(service, failure_mode, cluster)
            response = self._gemini.synthesize_pattern(prompt)
            in_toks, out_toks = self._gemini.token_counts()
            total_input_tokens += in_toks
            total_output_tokens += out_toks

            payload = self._render_yaml_payload(
                service=service,
                failure_mode=failure_mode,
                cluster=cluster,
                synthesis=response,
            )
            target.write_text(yaml.safe_dump(payload, sort_keys=False, width=80), encoding="utf-8")
            written.append(target)

        cost = (
            total_input_tokens * _GEMINI_PRO_INPUT_PER_TOKEN_USD
            + total_output_tokens * _GEMINI_PRO_OUTPUT_PER_TOKEN_USD
        )
        return CuratorReport(
            run_at=datetime.now(UTC),
            clusters_examined=len(eligible),
            patterns_extracted=len(written),
            files_written=tuple(sorted(written)),
            total_cost_usd=cost,
        )

    # ------------------------------------------------------------------ #
    # Internals — clustering, prompt, YAML emission, naming.
    # ------------------------------------------------------------------ #

    def _resolve_few_shot_dir(self) -> Path:
        if self._config.few_shot_directory is not None:
            return self._config.few_shot_directory
        # Fall through to MemoryStore's configured location so both sides
        # of the read/write contract point at the same place.
        return self._memory._few_shot_dir()

    @staticmethod
    def _cluster_records(
        records: Iterable[IncidentRecord],
    ) -> dict[tuple[str, str], list[IncidentRecord]]:
        """Group records by ``(service, failure_mode)``.

        Service is derived from the signature's first affected entity id
        (deterministic) with a token-based fallback to the signature's
        title. Failure mode is the confirmed root cause key.
        """
        clusters: dict[tuple[str, str], list[IncidentRecord]] = defaultdict(list)
        for record in records:
            if not record.confirmed_root_cause_key:
                continue
            service = _service_from_record(record)
            key = (service, record.confirmed_root_cause_key)
            clusters[key].append(record)
        return dict(clusters)

    @staticmethod
    def _filename_stem(service: str, failure_mode: str, cluster: Iterable[IncidentRecord]) -> str:
        """Deterministic ``service_failure_sha8`` stem for the pattern file.

        Embedding the SHA-8 of the sorted incident-id list keeps the
        Curator idempotent: re-running with the same cluster produces
        the same filename, so the existence check skips. A fresh
        incident lifts the SHA and produces a new file alongside.
        """
        ids = sorted(record.incident_id for record in cluster)
        digest = hashlib.sha256("|".join(ids).encode("utf-8")).hexdigest()[:8]
        return f"{_slug(service)}_{_slug(failure_mode)}_{digest}"

    def _build_prompt(
        self,
        service: str,
        failure_mode: str,
        cluster: list[IncidentRecord],
    ) -> str:
        """Render the Gemini Pro prompt for one cluster.

        Sends only the structured facts the model needs (titles, fixes,
        severities) — never the full Brief markdown. Pro is asked for a
        JSON-shaped dict so the result drops straight into the YAML.
        """
        lines = [
            "You are the Curator agent for an SRE pre-mortem system.",
            "Synthesize a single recurring incident pattern from the cluster below.",
            f"service: {service}",
            f"failure_mode: {failure_mode}",
            f"cluster_size: {len(cluster)}",
            "examples:",
        ]
        for record in cluster[: self._config.max_examples_per_pattern]:
            lines.append(
                f"- id={record.incident_id}; title={record.signature.title!r}; "
                f"fix={record.confirmed_fix!r}"
            )
        lines.extend(
            [
                "Respond ONLY with JSON of shape:",
                '{"pattern_summary": "...", "recommended_action": "...", '
                '"diagnostic_dql": "..."}',
            ]
        )
        return "\n".join(lines)

    def _render_yaml_payload(
        self,
        *,
        service: str,
        failure_mode: str,
        cluster: list[IncidentRecord],
        synthesis: dict[str, Any],
    ) -> dict[str, Any]:
        """Build the dict that becomes the YAML file content.

        Keeps fields the next deploy's specialist needs to consume as a
        few-shot example: pattern metadata, the synthesized prose, and
        a small slice of historical exemplars for grounding.
        """
        examples = [
            {
                "incident_id": record.incident_id,
                "title": record.signature.title,
                "severity": record.signature.severity,
                "fix": record.confirmed_fix,
            }
            for record in cluster[: self._config.max_examples_per_pattern]
        ]
        return {
            "schema_version": Brief.SCHEMA_VERSION,
            "service": service,
            "failure_mode": failure_mode,
            "cluster_size": len(cluster),
            "synthesized_at": datetime.now(UTC).isoformat(),
            "pattern_summary": str(synthesis.get("pattern_summary", "")),
            "recommended_action": str(synthesis.get("recommended_action", "")),
            "diagnostic_dql": str(synthesis.get("diagnostic_dql", "")),
            "examples": examples,
        }


# ---------------------------------------------------------------------- #
# Module-private helpers
# ---------------------------------------------------------------------- #


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(text: str) -> str:
    """Lowercase + collapse non-alphanumerics to single underscore.

    Used for both service + failure-mode segments of the pattern filename
    so they're cross-platform-safe (no slashes, no spaces, no colons).
    """
    cleaned = _SLUG_RE.sub("_", text.lower()).strip("_")
    return cleaned or "unknown"


def _service_from_record(record: IncidentRecord) -> str:
    """Pull a service identifier out of an IncidentRecord deterministically.

    Prefers the first non-empty ``affected_entity_ids`` token (the
    Dynatrace entity id); falls back to the first word of the signature
    title (covers historical seeds that lacked structured entities).
    """
    for entity_id in record.signature.affected_entity_ids:
        if entity_id:
            return entity_id
    title = record.signature.title.strip()
    if title:
        return title.split()[0]
    return "unknown"


_SINCE_RE = re.compile(r"^\s*(\d+)\s*([dhm])\s*$", re.IGNORECASE)


def _parse_since(token: str) -> datetime:
    """Parse ``"7d"`` / ``"24h"`` / ``"30m"`` into a UTC ``datetime``.

    Computed as ``datetime.now(UTC) - timedelta(...)``; raises
    ``argparse.ArgumentTypeError`` on unparseable input so the CLI
    surfaces a clean error.
    """
    match = _SINCE_RE.match(token)
    if not match:
        raise argparse.ArgumentTypeError(f"--since must look like 7d / 24h / 30m, got {token!r}")
    count, unit = int(match.group(1)), match.group(2).lower()
    if unit == "d":
        delta = timedelta(days=count)
    elif unit == "h":
        delta = timedelta(hours=count)
    else:
        delta = timedelta(minutes=count)
    return datetime.now(UTC) - delta


def _build_argparser() -> argparse.ArgumentParser:
    """Return the Curator's CLI parser.

    Kept as a separate function so the unit suite can exercise the
    parsing logic without invoking ``main``.
    """
    parser = argparse.ArgumentParser(
        prog="python -m causal_oncall.curator",
        description=(
            "Weekly batch synthesis of higher-order incident patterns. "
            "Reads resolved incidents from Mongo, clusters by service + "
            "failure mode, and emits few-shot YAML patterns the next "
            "deploy's specialists consume."
        ),
    )
    parser.add_argument(
        "--since",
        type=_parse_since,
        default=None,
        help="Lookback window (e.g. 7d, 24h, 30m). Defaults to CuratorConfig.lookback_days.",
    )
    parser.add_argument(
        "--few-shot-dir",
        type=Path,
        default=None,
        help="Override the directory where pattern YAMLs are written.",
    )
    return parser


def main(
    argv: list[str] | None = None,
    *,
    memory: MemoryStore | None = None,
    gemini_client: GeminiSynthesisClient | None = None,
) -> int:
    """CLI entry point. Returns a Unix exit code.

    The ``memory`` and ``gemini_client`` kwargs are seams for unit tests
    so the CLI flow runs end-to-end without Vertex AI creds. Production
    runs (``python -m causal_oncall.curator --since 7d``) leave both
    ``None`` and the function builds real clients from environment vars.
    """
    args = _build_argparser().parse_args(argv)
    config = CuratorConfig(few_shot_directory=args.few_shot_dir)
    since = args.since or (datetime.now(UTC) - timedelta(days=config.lookback_days))

    memory = memory if memory is not None else _build_memory_store_from_env(config)
    curator = Curator(memory=memory, config=config, gemini_client=gemini_client)
    report = curator.synthesize(since)

    sys.stdout.write(
        f"Curator: examined {report.clusters_examined} cluster(s), "
        f"wrote {report.patterns_extracted} pattern file(s), "
        f"est. cost ${report.total_cost_usd:.4f}\n"
    )
    for path in report.files_written:
        sys.stdout.write(f"  - {path}\n")
    return 0


def _build_memory_store_from_env(  # pragma: no cover  # env-driven boot
    config: CuratorConfig,
) -> MemoryStore:
    """Production wiring: read Mongo + Vertex env vars and build a real store.

    Mirrors ``app.py``'s ``_build_production_wiring`` for the MemoryStore
    subset only; the Curator never needs Dynatrace, Slack, or Phoenix.
    """
    cfg = MemoryStoreConfig(
        mongodb_uri=os.environ["MONGODB_URI"],
        database=os.environ.get("MONGODB_DATABASE", "causal_oncall"),
        collection=os.environ.get("MONGODB_INCIDENTS_COLLECTION", "incidents"),
        vector_index_name=os.environ.get("MONGODB_VECTOR_INDEX_NAME", "incident_vec_idx"),
        embedding_model_id=os.environ.get("EMBEDDING_MODEL_ID", "text-embedding-005"),
        embedding_dimensions=int(os.environ.get("EMBEDDING_DIMENSIONS", "768")),
        match_threshold=float(os.environ.get("MEMORY_MATCH_THRESHOLD", "0.85")),
        few_shot_directory=config.few_shot_directory,
    )
    return MemoryStore(cfg)


class _LazyVertexGeminiClient:  # pragma: no cover  # vertex-backed; contract-only
    """Production Gemini Pro client; constructed but not connected until first call.

    Real Vertex AI calls are exercised by the cassette + manual scripts;
    the unit suite never touches this class.
    """

    def __init__(self, *, model_id: str) -> None:
        self._model_id = model_id
        self._last_input_tokens = 0
        self._last_output_tokens = 0

    def synthesize_pattern(self, prompt: str) -> dict[str, Any]:
        from google import genai

        client = genai.Client(
            vertexai=True,
            project=os.environ.get("GOOGLE_CLOUD_PROJECT", ""),
            location=os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1"),
        )
        response = client.models.generate_content(model=self._model_id, contents=prompt)
        usage = getattr(response, "usage_metadata", None)
        if usage is not None:
            self._last_input_tokens = int(getattr(usage, "prompt_token_count", 0) or 0)
            self._last_output_tokens = int(getattr(usage, "candidates_token_count", 0) or 0)
        import json as _json

        return _json.loads(response.text)

    def token_counts(self) -> tuple[int, int]:
        return (self._last_input_tokens, self._last_output_tokens)


if (
    __name__ == "__main__"
):  # pragma: no cover  # exercised via tests/unit/test_curator.py::test_main
    raise SystemExit(main())
