"""PIT-safe offline historical replay and evaluation contracts."""

from .contracts import (
    HistoricalDatasetManifest, IdentityTemporalSemantics, ReplayCampaignManifest, ReplayClock,
    ReplayFailureRecord, ReplayPoint, ReplayResult, ReplaySummary, ReplayUniverse,
    ReplayMode,
)
from .identity import HistoricalIdentityAuthority
from .policy import (
    HistoricalEvidenceEvaluation, ReplayEvidence, evaluate_historical_evidence,
    knowledge_is_visible, report_is_visible,
)
__all__ = [
    "HistoricalDatasetManifest", "HistoricalEvidenceEvaluation", "HistoricalIdentityAuthority",
    "IdentityTemporalSemantics", "ReplayCampaignManifest",
    "ReplayCampaignService", "ReplayClock", "ReplayEngine", "ReplayEvidence",
    "ReplayFailureRecord", "ReplayMode", "ReplayPoint", "ReplayResult", "ReplaySummary",
    "ReplaySupplementalOutcome", "ReplayUniverse", "evaluate_historical_evidence",
    "knowledge_is_visible", "report_is_visible",
]


def __getattr__(name: str):
    if name in {
        "ReplayCampaignService", "ReplayEngine", "ReplaySupplementalOutcome",
    }:
        from .service import (
            ReplayCampaignService, ReplayEngine, ReplaySupplementalOutcome,
        )
        return {
            "ReplayCampaignService": ReplayCampaignService,
            "ReplayEngine": ReplayEngine,
            "ReplaySupplementalOutcome": ReplaySupplementalOutcome,
        }[name]
    raise AttributeError(name)
