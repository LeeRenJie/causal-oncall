"""Curator — weekly pattern synthesis over the incident memory.

Hides: the batch query that pulls resolved incidents from the last N
days, the cluster-by-confirmed-root-cause analysis, the few-shot
prompt-fragment generation, and the safe-write back into the
specialists' few-shot pool.

This is the slowest-moving piece of the learning loop and the first
thing on the cut list per ENGINEERING-PRINCIPLES §1 if we slip.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime

from causal_oncall.memory_store import MemoryStore


@dataclass(frozen=True, slots=True)
class CuratorConfig:
    """Tunables for the weekly batch."""

    lookback_days: int = 30
    min_cluster_size: int = 3
    max_few_shot_examples_per_specialist: int = 6


@dataclass(frozen=True, slots=True)
class CurationReport:
    """Summary of one curator run; suitable for the Slack weekly digest."""

    run_at: datetime
    incidents_scanned: int
    clusters_promoted: int
    top_recurring_pattern: str


class Curator:
    """Off-line pattern miner. Runs weekly, never in the request path."""

    def __init__(self, *, memory: MemoryStore, config: CuratorConfig | None = None) -> None:
        self._memory = memory
        self._config = config or CuratorConfig()

    def run_weekly_batch(self) -> CurationReport:
        """Cluster recently-resolved incidents and update specialist few-shots."""
        records = list(
            self._memory.list_resolved_since(self._config.lookback_days)  # type: ignore[attr-defined]
        )
        counts: Counter[str] = Counter()
        for record in records:
            key = record.confirmed_root_cause_key
            if key:
                counts[key] += 1

        already_promoted: set[str] = set()
        existing = getattr(self._memory, "already_promoted_keys", None)
        if callable(existing):
            already_promoted = set(existing())

        promoted = 0
        top_key = ""
        top_count = 0
        for key, count in counts.most_common():
            if count > top_count:
                top_key, top_count = key, count
            if count < self._config.min_cluster_size:
                continue
            if key in already_promoted:
                continue
            self._memory.promote_few_shot(key)  # type: ignore[attr-defined]
            promoted += 1

        return CurationReport(
            run_at=datetime.now(UTC),
            incidents_scanned=len(records),
            clusters_promoted=promoted,
            top_recurring_pattern=(
                f"{top_key} recurred {top_count} time(s)"
                if top_count
                else "no recurring pattern this window"
            ),
        )
