"""Causal On-Call — multi-agent SRE pre-mortem for Dynatrace problems.

The public surface intentionally re-exports only the domain types and the
orchestrator entrypoint. Everything else is an implementation detail.
"""

from __future__ import annotations

from causal_oncall.domain.brief import Brief, Hypothesis
from causal_oncall.domain.evidence import Evidence
from causal_oncall.domain.exceptions import (
    DynatraceUnavailable,
    MemoryStoreUnavailable,
    RateLimited,
    SynthesisFailed,
)
from causal_oncall.domain.incident_record import IncidentRecord, Match
from causal_oncall.domain.problem_signature import ProblemSignature
from causal_oncall.orchestrator import Orchestrator

__all__ = [
    "Brief",
    "DynatraceUnavailable",
    "Evidence",
    "Hypothesis",
    "IncidentRecord",
    "Match",
    "MemoryStoreUnavailable",
    "Orchestrator",
    "ProblemSignature",
    "RateLimited",
    "SynthesisFailed",
]

__version__ = "0.1.0"
