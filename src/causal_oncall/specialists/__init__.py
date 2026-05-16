"""Specialist sub-agents.

Five specialists, all conforming to the same ``Specialist`` contract:
``investigate(ProblemSignature) -> Evidence``. The orchestrator dispatches
them sequentially (Dynatrace's 50 req/min rate limit makes parallel
fan-out unsafe in practice) and aggregates their evidence for the
synthesizer.
"""

from causal_oncall.specialists.anomaly_window import AnomalyWindowSpecialist
from causal_oncall.specialists.base import Specialist
from causal_oncall.specialists.deploy_correlation import DeployCorrelationSpecialist
from causal_oncall.specialists.topology import TopologySpecialist
from causal_oncall.specialists.triage import TriageSpecialist
from causal_oncall.specialists.vuln_sec import VulnSecSpecialist

__all__ = [
    "AnomalyWindowSpecialist",
    "DeployCorrelationSpecialist",
    "Specialist",
    "TopologySpecialist",
    "TriageSpecialist",
    "VulnSecSpecialist",
]
