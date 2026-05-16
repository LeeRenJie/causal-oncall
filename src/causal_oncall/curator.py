"""Curator — weekly pattern synthesis over the incident memory.

Hides: the batch query that pulls resolved incidents from the last N
days, the cluster-by-confirmed-root-cause analysis, the few-shot
prompt-fragment generation, and the safe-write back into the
specialists' few-shot pool.

This is the slowest-moving piece of the learning loop and the first
thing on the cut list per ENGINEERING-PRINCIPLES §1 if we slip.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from causal_oncall.memory_store import MemoryStore


@dataclass(frozen=True, slots=True)
class CuratorConfig:
    """Tunables for the weekly batch."""

    lookback_days: int = 30
    min_cluster_size: int = 3
    max_few_shot_examples_per_specialist: int = 6


@dataclass(frozen=True, slots=True)
class CurationReport:
    """Summary of one curator run; suitable for the Slack weekly digest.

    Attributes:
        run_at: When the batch ran (UTC).
        incidents_scanned: How many resolved records were considered.
        clusters_promoted: Number of new few-shot patterns written back.
        top_recurring_pattern: Single English sentence naming the biggest
            recurring incident shape this week.
    """

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
        """Cluster recently-resolved incidents and update specialist few-shots.

        Returns a CurationReport for the weekly digest. Safe to invoke
        idempotently; re-running on the same window yields the same
        promotions and does not re-promote already-known clusters.
        """
        raise NotImplementedError(
            "Pull resolved incidents over lookback_days, cluster by "
            "confirmed_root_cause_key, promote clusters >= min_cluster_size to "
            "specialist few-shots, and return a CurationReport."
        )
