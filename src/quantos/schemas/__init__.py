"""Stable business schemas shared across Phase 1A modules."""

from .time_slice import (
    EvidenceView, EvidenceWindowResult, PreviousKnowledgeBrief, ReportType,
    TemporalBucket, TimeSliceIntelligenceReport,
)

from .run import (
    KnowledgeOperationalState, KnowledgeOperationalStatus,
    RunContext, RunType, ModuleName, ModuleAvailabilityStatus,
    ModuleExecutionStatus, ModulePlanEntry, ModuleExecutionResult,
    ModuleExecutionPlan, ReadinessSnapshot, QuantOSRunManifest,
)

from .schedule import (
    ScheduleDecision, ScheduleDecisionStatus, ScheduleEvaluation, SchedulePolicy,
    ScheduleSlot, ScheduledRunIntent,
)

from .scheduler_runtime import (
    ClaimResult, ClaimStatus, MissedRunAction, MissedRunPolicy, MissedRunRule,
    RetryPolicy, RuntimeEligibility, RuntimeEligibilityStatus, ScheduleInstance,
    SchedulerAttempt, SchedulerAttemptStatus, SchedulerExecutionOutcome,
    SchedulerExecutionResult, SchedulerInstanceRecord, SchedulerInstanceStatus,
    SchedulerInvocationStatus, SchedulerRuntimePolicy, SchedulerRuntimeResult,
)

from .scheduler_ops import (
    LocalTradingCalendarArtifact, SchedulerOnceOverallStatus,
    SchedulerOnceResult, SchedulerOpsEvent, SchedulerOpsEventType,
    SchedulerSlotResult,
)

from .knowledge import (
    KnowledgeCorpusManifest, KnowledgeDocument, KnowledgeDocumentInput,
    KnowledgeProvenance, KnowledgeQueryMode, KnowledgeResearchView,
    KnowledgeSourceType, KnowledgeTemporalClass,
    build_knowledge_corpus_manifest, canonicalize_knowledge_document,
)
from .knowledge_ingestion import (
    KnowledgeIngestionErrorCode, KnowledgeIngestionOutcome,
    KnowledgeIngestionRequest, KnowledgeIngestionResult, KnowledgeMediaType,
    KnowledgeParseWarning, KnowledgeSourceArtifact, ParsedKnowledgeDocument,
)
from .knowledge_retrieval import (
    KnowledgeChunk, KnowledgeChunkManifest, KnowledgeLexicalIndex,
    KnowledgeLexicalIndexEntry, KnowledgeRetrievalErrorCode,
    KnowledgeRetrievalHit, KnowledgeRetrievalRequest, KnowledgeRetrievalResult,
)
from .knowledge_context import (
    KnowledgeContextBundle, KnowledgeContextItem, KnowledgeContextLane,
    KnowledgeContextIntegrityError, KnowledgeContextPolicy,
    KnowledgeContextStopReason, validate_knowledge_context_bundle,
)

from .anomalies import AnomalyStatus, SectorAnomaly, StockAnomaly
from .candidates import AnomalyCandidate
from .discovery import (
    CandidateCoverage,
    CanonicalDiscoveredEvidence,
    DiscoveryMention,
    EvidenceCoverageReport,
    EvidenceDiscoveryRequest,
    MultiSourceCandidateEvidenceBundle,
    ProviderCoverage,
    ProviderDiscoveryResult,
    ProviderBenchmarkReport,
    EvidenceQualityFlags,
    ProviderQualityMetrics,
    CrossProviderDedupAudit,
    AttributionEvidenceFact,
    AttributionEvidenceBundle,
    WebSearchResult,
)
from .jobs import JobContext, JobStatus, JobType
from .market import MarketBar
from .moneyflow import (
    CandidateFundFlowEvidence,
    FundFlowEvidence,
    MarketFundFlow,
    MoneyFlowRecord,
    SectorFundFlow,
)
from .news import (
    AnnouncementRecord,
    CandidateEvidenceBundle,
    CandidateNewsEvidence,
    NewsMention,
    NewsRecord,
)
from .securities import SecurityMaster
from .bootstrap import (
    BootstrapManifest, ProviderAvailability, ProviderContract, ProviderFailureCode,
    ProviderHealthRecord, SecurityIdentityRecord, SecurityMasterSnapshot,
    canonical_security_id,
)
from .sectors import (
    SectorMembership,
    SectorMembershipSnapshot,
    SectorRank,
    SectorSnapshot,
)
from .snapshots import MarketSnapshot
from .synthesis import (
    EvidenceSynthesis, KnowledgeBackgroundStatement, PossibleExplanation,
    SynthesisCandidate, SynthesisEvidence, SynthesisFundFlowFacts, SynthesisInput,
    SynthesisMarketFacts,
)
from .report import (
    CandidateBrief, DailyIntelligenceReport, EvidenceStats, SynthesisBrief,
)

__all__ = [
    "ReportType", "TimeSliceIntelligenceReport", "EvidenceView", "EvidenceWindowResult",
    "PreviousKnowledgeBrief", "TemporalBucket",
    "KnowledgeOperationalState", "KnowledgeOperationalStatus",
    "RunContext", "RunType", "ModuleName", "ModuleAvailabilityStatus",
    "ModuleExecutionStatus", "ModulePlanEntry", "ModuleExecutionResult",
    "ModuleExecutionPlan", "ReadinessSnapshot", "QuantOSRunManifest",
    "ScheduleDecision", "ScheduleDecisionStatus", "ScheduleEvaluation",
    "SchedulePolicy", "ScheduleSlot", "ScheduledRunIntent",
    "ClaimResult", "ClaimStatus", "MissedRunAction", "MissedRunPolicy",
    "MissedRunRule", "RetryPolicy", "RuntimeEligibility",
    "RuntimeEligibilityStatus", "ScheduleInstance", "SchedulerAttempt",
    "SchedulerAttemptStatus", "SchedulerExecutionOutcome",
    "SchedulerExecutionResult", "SchedulerInstanceRecord",
    "SchedulerInstanceStatus", "SchedulerInvocationStatus",
    "SchedulerRuntimePolicy", "SchedulerRuntimeResult",
    "LocalTradingCalendarArtifact", "SchedulerOnceOverallStatus",
    "SchedulerOnceResult", "SchedulerOpsEvent", "SchedulerOpsEventType",
    "SchedulerSlotResult",
    "KnowledgeCorpusManifest", "KnowledgeDocument", "KnowledgeDocumentInput",
    "KnowledgeProvenance", "KnowledgeQueryMode", "KnowledgeResearchView",
    "KnowledgeSourceType", "KnowledgeTemporalClass",
    "build_knowledge_corpus_manifest", "canonicalize_knowledge_document",
    "KnowledgeIngestionErrorCode", "KnowledgeIngestionOutcome",
    "KnowledgeIngestionRequest", "KnowledgeIngestionResult", "KnowledgeMediaType",
    "KnowledgeParseWarning", "KnowledgeSourceArtifact", "ParsedKnowledgeDocument",
    "KnowledgeChunk", "KnowledgeChunkManifest", "KnowledgeLexicalIndex",
    "KnowledgeLexicalIndexEntry", "KnowledgeRetrievalErrorCode",
    "KnowledgeRetrievalHit", "KnowledgeRetrievalRequest", "KnowledgeRetrievalResult",
    "KnowledgeContextBundle", "KnowledgeContextItem", "KnowledgeContextLane",
    "KnowledgeContextIntegrityError", "KnowledgeContextPolicy",
    "KnowledgeContextStopReason", "validate_knowledge_context_bundle",
    "AnomalyStatus",
    "AnomalyCandidate",
    "AnnouncementRecord",
    "CandidateEvidenceBundle",
    "CandidateNewsEvidence",
    "CandidateCoverage",
    "CanonicalDiscoveredEvidence",
    "DiscoveryMention",
    "EvidenceCoverageReport",
    "EvidenceDiscoveryRequest",
    "JobContext",
    "JobStatus",
    "JobType",
    "MarketBar",
    "MarketFundFlow",
    "MarketSnapshot",
    "MoneyFlowRecord",
    "MultiSourceCandidateEvidenceBundle",
    "NewsMention",
    "NewsRecord",
    "ProviderCoverage",
    "ProviderDiscoveryResult",
    "ProviderBenchmarkReport",
    "EvidenceQualityFlags",
    "ProviderQualityMetrics",
    "CrossProviderDedupAudit",
    "AttributionEvidenceFact",
    "AttributionEvidenceBundle",
    "SecurityMaster",
    "BootstrapManifest", "ProviderAvailability", "ProviderContract",
    "ProviderFailureCode", "ProviderHealthRecord", "SecurityIdentityRecord",
    "SecurityMasterSnapshot", "canonical_security_id",
    "SectorMembership",
    "SectorMembershipSnapshot",
    "SectorAnomaly",
    "SectorRank",
    "SectorSnapshot",
    "StockAnomaly",
    "FundFlowEvidence",
    "CandidateFundFlowEvidence",
    "SectorFundFlow",
    "WebSearchResult",
    "EvidenceSynthesis",
    "KnowledgeBackgroundStatement",
    "PossibleExplanation",
    "SynthesisCandidate",
    "SynthesisEvidence",
    "SynthesisFundFlowFacts",
    "SynthesisInput",
    "SynthesisMarketFacts",
    "CandidateBrief",
    "DailyIntelligenceReport",
    "EvidenceStats",
    "SynthesisBrief",
]
