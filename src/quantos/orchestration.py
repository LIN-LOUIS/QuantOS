"""Deterministic run policy and sequential execution over existing artifacts.

No scheduling, upstream recomputation, provider fallback, retry or LLM planning.
The local adapter reuses persisted Phase 3B reports; earlier cutoffs fail closed.
"""

from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import time
from typing import Callable, Mapping, Sequence

from quantos.config import Settings
from quantos.schemas._validation import require_aware
from quantos.schemas.run import (
    ArtifactReadiness, ArtifactReference, ModuleAvailabilityStatus as A,
    KNOWLEDGE_RUN_SCHEMA_VERSION, KnowledgeOperationalState,
    KnowledgeOperationalStatus as K,
    ModuleExecutionPlan, ModuleExecutionResult, ModuleExecutionStatus as E,
    ModuleName as M, ModulePlanEntry, ORCHESTRATION_POLICY_VERSION,
    QuantOSRunManifest, ReadinessSnapshot, RUN_SCHEMA_VERSION, RunContext,
    RunSummary, RunType,
)

HARD_DEPENDENCIES = {
    M.MARKET_CONTEXT: (), M.SECTOR_CONTEXT: (),
    M.ANOMALY_TRIAGE: (M.MARKET_CONTEXT,), M.FUND_FLOW: (),
    M.EVIDENCE: (M.ANOMALY_TRIAGE,), M.ATTRIBUTION: (M.EVIDENCE,),
    M.SYNTHESIS: (M.ATTRIBUTION,),
    M.DAILY_REPORT: (M.MARKET_CONTEXT, M.ANOMALY_TRIAGE),
}
SOFT_DEPENDENCIES = {
    M.SECTOR_CONTEXT: (M.MARKET_CONTEXT,),
    M.FUND_FLOW: (M.ANOMALY_TRIAGE,),
    M.DAILY_REPORT: (M.SECTOR_CONTEXT, M.FUND_FLOW, M.ATTRIBUTION, M.SYNTHESIS),
}
EXECUTION_ORDER = tuple(M)


def collect_readiness(
    *, target_trade_date: date, as_of_time: datetime, run_type: RunType,
    mode: str, artifacts: Sequence[ArtifactReadiness],
    knowledge_state: KnowledgeOperationalState | None = None,
) -> ReadinessSnapshot:
    """Reduce actual artifact metadata; dates are observed, never calendar - 1."""
    require_aware(as_of_time, "as_of_time")
    if not isinstance(run_type, RunType) or mode not in {"research", "strict_live"}:
        raise ValueError("explicit run_type and mode required")
    def visible(item):
        return (item.available_at <= as_of_time
                and (item.artifact_as_of_time is None or item.artifact_as_of_time <= as_of_time))
    markets = [x for x in artifacts if x.module == M.MARKET_CONTEXT
               and (x.mode is None or x.mode == mode)
               and x.completed_daily and x.trade_date <= target_trade_date and visible(x)]
    target = any(x.trade_date == target_trade_date for x in markets)
    previous = [x.trade_date for x in markets if x.trade_date < target_trade_date]
    if run_type == RunType.POST_CLOSE:
        basis = target_trade_date if target else None
        reason = "READY_PIT_DATA" if target else "TARGET_MARKET_NOT_PIT_READY"
    else:
        basis = max(previous, default=None)
        reason = "COMPLETED_DAILY_BACKGROUND_ONLY" if basis else "NO_COMPLETED_DAILY_BASIS"
    candidates = [x for x in artifacts if basis is not None and x.trade_date == basis
                  and (x.mode is None or x.mode == mode)]
    ready = []
    for item in candidates:
        if not visible(item):
            continue
        if item.module == M.MARKET_CONTEXT and not item.completed_daily:
            continue
        if item.module in {M.EVIDENCE, M.ATTRIBUTION, M.SYNTHESIS, M.DAILY_REPORT} and item.mode != mode:
            continue
        if mode == "strict_live" and item.module in {M.EVIDENCE, M.ATTRIBUTION, M.SYNTHESIS}:
            if not item.strict_pit:
                continue
            # Empty strict channels are valid artifacts, even if materialized later.
            if item.module == M.ATTRIBUTION and item.eligible_evidence_count != 0:
                if item.market_event_time is None or item.available_at > item.market_event_time:
                    continue
        ready.append(item)
    ready.sort(key=lambda x: (EXECUTION_ORDER.index(x.module), x.available_at, x.reference.path))
    modules = {x.module for x in ready}
    future = {x.module for x in candidates if not visible(x)}
    if basis is None and any(x.module == M.MARKET_CONTEXT and x.trade_date == target_trade_date
                             and not visible(x) for x in artifacts):
        future.add(M.MARKET_CONTEXT)
    fund_next = min((x.available_at for x in candidates if x.module == M.FUND_FLOW
                     and x.available_at > as_of_time), default=None)
    attribution = [x for x in ready if x.module == M.ATTRIBUTION]
    eligible = attribution[-1].eligible_evidence_count if attribution else None
    return ReadinessSnapshot(
        target_trade_date, as_of_time, run_type, mode, target, bool(previous), basis,
        M.SECTOR_CONTEXT in modules, M.ANOMALY_TRIAGE in modules,
        M.ANOMALY_TRIAGE in modules, M.FUND_FLOW in modules, fund_next,
        M.EVIDENCE in modules, mode == "research" and bool(attribution),
        mode == "strict_live" and bool(attribution), M.SYNTHESIS in modules,
        eligible, tuple(ready), tuple(x for x in EXECUTION_ORDER if x in future), reason,
        knowledge_state=knowledge_state,
    )


def build_run_plan(context: RunContext, readiness: ReadinessSnapshot) -> ModuleExecutionPlan:
    """Pure fixed-policy planning; never calls handlers, clocks, IO or models."""
    if (context.target_trade_date, context.as_of_time, context.run_type, context.mode,
        context.market_basis_trade_date) != (
        readiness.target_trade_date, readiness.as_of_time, readiness.run_type,
        readiness.mode, readiness.market_basis_trade_date,
    ):
        raise ValueError("context and readiness must describe the same run")
    known = {
        M.MARKET_CONTEXT: context.market_basis_trade_date is not None,
        M.SECTOR_CONTEXT: readiness.sector_basis_available,
        M.ANOMALY_TRIAGE: readiness.anomaly_basis_available and readiness.triage_basis_available,
        M.FUND_FLOW: readiness.fund_flow_basis_available,
        M.EVIDENCE: readiness.evidence_available,
        M.ATTRIBUTION: (readiness.attribution_research_available if context.mode == "research"
                        else readiness.attribution_strict_available),
    }
    entries = []
    states = {}
    for module in EXECUTION_ORDER:
        status, reason = A.READY, "READY_PIT_DATA"
        if context.run_type != RunType.POST_CLOSE and module in {
            M.EVIDENCE, M.ATTRIBUTION, M.SYNTHESIS, M.DAILY_REPORT,
        }:
            status = A.SKIP_UNSUPPORTED_IN_V1
            reason = ("PRE_OPEN_EVENT_ATTRIBUTION_UNSUPPORTED" if context.run_type == RunType.PRE_OPEN
                      else "CURRENT_INTRADAY_DATA_UNSUPPORTED")
        elif any(states[dep] != A.READY for dep in HARD_DEPENDENCIES[module]):
            status, reason = A.BLOCKED_DEPENDENCY, "DEPENDENCY_NOT_READY"
        elif module == M.SYNTHESIS:
            if readiness.eligible_evidence_count == 0:
                reason = "NO_ELIGIBLE_EVIDENCE"
            elif readiness.synthesis_cache_available:
                reason = "VALIDATED_CACHE_AVAILABLE"
            elif not context.llm_allowed:
                status, reason = A.SKIP_POLICY_DISABLED, "LLM_DISABLED"
            elif readiness.eligible_evidence_count is None:
                status, reason = A.SKIP_DATA_MISSING, "NO_PIT_VALID_ARTIFACT"
            else:
                reason = "ELIGIBLE_EVIDENCE_GENERATION_ALLOWED"
        elif module != M.DAILY_REPORT and not known[module]:
            if module in readiness.future_modules:
                status, reason = A.SKIP_NOT_AVAILABLE_YET, "DATA_AVAILABLE_AFTER_AS_OF"
            else:
                status, reason = A.SKIP_DATA_MISSING, "NO_PIT_VALID_ARTIFACT"
        elif context.run_type != RunType.POST_CLOSE:
            reason = "COMPLETED_DAILY_BACKGROUND_ONLY"
        states[module] = status
        entries.append(ModulePlanEntry(module, status, context.market_basis_trade_date,
                                       reason, HARD_DEPENDENCIES[module],
                                       SOFT_DEPENDENCIES.get(module, ()), status == A.READY))
    return ModuleExecutionPlan(tuple(entries))


@dataclass(frozen=True, slots=True)
class ModuleOutcome:
    """Handler result: only references/accounting; never payloads or model prose."""
    artifact_refs: tuple[ArtifactReference, ...] = ()
    real_llm_requests: int = 0
    report_id: str | None = None
    report_reused: bool = False


class ModuleUnavailable(RuntimeError):
    """A local module cannot execute; messages are never retained."""


def execute_run(
    context: RunContext, readiness: ReadinessSnapshot, *,
    handlers: Mapping[M, Callable[[RunContext, tuple[ModuleExecutionResult, ...]], ModuleOutcome]],
    plan_only: bool = False, clock: Callable[[], datetime] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    git_commit: str | None = None,
) -> QuantOSRunManifest:
    """Sequential hard-dependency propagation, no retry and no nested workers.

    Handlers receive the immutable run policy plus preceding outcomes. Provider
    adapters must enforce context.llm_allowed at the invocation boundary; the
    supplied local adapter has no network capability at all.
    """
    now = clock or (lambda: datetime.now(timezone.utc))
    begun = monotonic()
    plan = build_run_plan(context, readiness)
    effective = []
    results = []
    request_count = 0
    report_id = None
    report_reused = False
    for entry in plan.entries:
        blocked = any(next(x for x in results if x.module == dep).status != E.PASS
                      for dep in entry.dependencies) if not plan_only else False
        if blocked and entry.will_execute:
            entry = replace(entry, availability=A.BLOCKED_DEPENDENCY,
                            reason_code="DEPENDENCY_EXECUTION_FAILED", will_execute=False)
        effective.append(entry)
        if plan_only or not entry.will_execute:
            results.append(ModuleExecutionResult(entry.module, E.SKIP,
                           "PLAN_ONLY" if plan_only else entry.reason_code, None, None, None))
            continue
        started = now()
        timer = monotonic()
        status, code, refs = E.PASS, "EXISTING_MODULE_EXECUTED", ()
        safe_error = None
        try:
            handler = handlers.get(entry.module)
            if handler is None:
                raise ModuleUnavailable()
            outcome = handler(context, tuple(results))
            if not isinstance(outcome, ModuleOutcome):
                raise ValueError("invalid module outcome")
            if type(outcome.real_llm_requests) is not int or outcome.real_llm_requests < 0:
                raise ValueError("invalid request count")
            request_count += outcome.real_llm_requests
            if not context.llm_allowed and outcome.real_llm_requests:
                raise ValueError("LLM policy violation")
            refs = outcome.artifact_refs
            if entry.module == M.DAILY_REPORT:
                if not outcome.report_id or not refs:
                    raise ValueError("report outcome must identify an artifact")
                report_id, report_reused = outcome.report_id, outcome.report_reused
                code = "EXISTING_REPORT_REUSED" if report_reused else "REPORT_CREATED"
        except Exception as exc:
            status, refs = E.FAIL, ()
            safe_error = "MODULE_UNAVAILABLE" if isinstance(exc, ModuleUnavailable) else "MODULE_EXECUTION_FAILED"
            code = safe_error
        finished = now()
        results.append(ModuleExecutionResult(entry.module, status, code, started, finished,
                       max(0, round((monotonic() - timer) * 1000)), refs, safe_error))
    summary = RunSummary(len(plan.entries), sum(x.status != E.SKIP for x in results),
                         sum(x.status == E.PASS for x in results), sum(x.status == E.SKIP for x in results),
                         sum(x.status == E.FAIL for x in results), request_count,
                         report_id is not None and not report_reused, report_id,
                         max(0, round((monotonic() - begun) * 1000)), report_reused)
    knowledge_state = readiness.knowledge_state
    required_product_module = (
        M.DAILY_REPORT if context.run_type is RunType.POST_CLOSE
        else M.ANOMALY_TRIAGE
    )
    if (
        not plan_only
        and knowledge_state is not None
        and not knowledge_state.is_hard_failure
        and knowledge_state.product_ref is not None
        and not any(
            result.module is required_product_module
            and result.status == E.PASS
            and knowledge_state.product_ref in result.artifact_refs
            for result in results
        )
    ):
        knowledge_state = KnowledgeOperationalState(
            K.FAILED,
            "PRODUCT_VALIDATION_FAILED",
            knowledge_state.product_ref,
            None,
            0,
            0,
            0,
        )
    manifest_schema = (
        KNOWLEDGE_RUN_SCHEMA_VERSION
        if knowledge_state is not None
        else RUN_SCHEMA_VERSION
    )
    return QuantOSRunManifest(manifest_schema, context, tuple(effective), tuple(results), summary,
                             tuple(sorted((("orchestration_policy_version", ORCHESTRATION_POLICY_VERSION),
                              ("quantos_git_commit", git_commit),
                              ("market_basis_reason", readiness.basis_reason_code),
                              ("CURRENT_INTRADAY_MARKET_DATA", readiness.current_intraday_market_data),
                              ("execution_mode", "PLAN_ONLY" if plan_only else "EXECUTE")))),
                             knowledge_state)


def load_local_artifacts(settings: Settings, *, target_trade_date: date,
                         as_of_time: datetime, mode: str, top_n: int,
                         universe_name: str,
                         daily_report_paths: Sequence[Path] | None = None,
                         ) -> tuple[ArtifactReadiness, ...]:
    """Read completed daily metadata and validated persisted Phase 3B artifacts.

    Daily bars establish background basis only. Target-day readiness requires an
    existing report's canonical MarketSnapshot section. No algorithm is rerun.
    Report materialization time is a conservative availability lower bound.
    """
    from quantos.reporting import report_from_dict
    import duckdb

    require_aware(as_of_time, "as_of_time")
    observations = []
    for path in sorted(settings.normalized_market_dir.glob("trade_date=*/market=*/*.parquet")):
        # Metadata only: no prices, raw payloads or future financial values.
        with duckdb.connect(":memory:") as db:
            rows = db.execute("SELECT DISTINCT timestamp, available_at, frequency, schema_version FROM read_parquet(?)",
                              [str(path)]).fetchall()
        for timestamp, available, frequency, version in rows:
            local = timestamp.astimezone(settings.market_timezone)
            if frequency != "1d" or local.date() >= target_trade_date or timestamp > as_of_time:
                continue
            # Canonical daily bars carry the exchange close timestamp, not midnight.
            if (local.hour, local.minute) < (15, 0):
                continue
            observations.append(ArtifactReadiness(M.MARKET_CONTEXT, local.date(), available,
                                ArtifactReference("completed-daily-background", str(path.resolve()), version),
                                completed_daily=True))
    report_paths = (
        sorted(settings.report_dir.glob("trade_date=*/*.json"))
        if daily_report_paths is None
        else tuple(sorted(Path(path) for path in daily_report_paths))
    )
    for path in report_paths:
        raw = path.read_bytes()
        data = json.loads(raw, parse_float=Decimal)
        if data["mode"] != mode or data["universe_name"] != universe_name:
            continue
        if data["candidate_summary"]["requested_top_n"] != top_n:
            continue
        report = report_from_dict(data)
        if report.trade_date > target_trade_date:
            continue
        reference = ArtifactReference(hashlib.sha256(raw).hexdigest(), str(path.resolve()), report.schema_version)
        observations.extend(_daily_report_artifacts(report, reference, mode=mode))
    return tuple(observations)


def _daily_report_artifacts(report, reference, *, mode):
    """Derive readiness from one already-validated in-memory Daily product."""
    observations = []
    visible_at = max(report.as_of_time, report.generated_at)

    def add(module, **extra):
        observations.append(ArtifactReadiness(
            module, report.trade_date, visible_at, reference,
            artifact_as_of_time=report.as_of_time, mode=mode, **extra,
        ))

    add(M.MARKET_CONTEXT)
    add(M.ANOMALY_TRIAGE)
    if (
        report.sector_overview["sector_count"] > 0
        and report.sector_overview.get("historical_membership_mode") == "strict_pit"
    ):
        add(M.SECTOR_CONTEXT)
    if report.data_quality["fund_flow"]["covered_candidates"] > 0:
        add(M.FUND_FLOW)
    # A canonical empty evidence result is distinct from no artifact at all.
    add(M.EVIDENCE, strict_pit=mode == "strict_live")
    eligible = sum(
        item.evidence_stats.eligible_attribution_count
        for item in report.candidate_briefs
    )
    # Report alone cannot prove per-record strict availability for nonempty attribution.
    if mode == "research" or eligible == 0:
        add(
            M.ATTRIBUTION, strict_pit=mode == "strict_live",
            eligible_evidence_count=eligible,
        )
    # Report prose alone cannot prove exact synthesis-input cache identity.
    add(M.DAILY_REPORT)
    return tuple(observations)


def load_local_operational_artifacts(
    settings: Settings,
    *,
    target_trade_date: date,
    as_of_time: datetime,
    run_type: RunType,
    mode: str,
    top_n: int,
    universe_name: str,
    daily_report_ref: ArtifactReference | None = None,
    market_basis_trade_date: date | None = None,
) -> tuple[tuple[ArtifactReadiness, ...], KnowledgeOperationalState]:
    """Load one exact Daily product and derive its observed Knowledge state.

    Discovery is metadata-driven and fail-closed: select the completed basis date, then
    require exactly one matching product generation. No path/mtime/latest-file
    rule participates in product selection.
    """
    from quantos.reporting import render_daily_report, report_from_dict

    require_aware(as_of_time, "as_of_time")
    if not isinstance(run_type, RunType) or mode not in {"research", "strict_live"}:
        raise ValueError("explicit run_type and mode required")
    if market_basis_trade_date is not None and (
        type(market_basis_trade_date) is not date
        or run_type is RunType.PRE_OPEN
        and market_basis_trade_date >= target_trade_date
        or run_type is RunType.POST_CLOSE
        and market_basis_trade_date != target_trade_date
    ):
        raise ValueError("invalid expected market basis trade date")

    def failure(status, reason, reference=None):
        state = KnowledgeOperationalState(status, reason, reference, None, 0, 0, 0)
        return (), state

    if run_type is RunType.INTRADAY:
        return failure(K.UNAVAILABLE, "PRODUCT_UNAVAILABLE")

    if daily_report_ref is not None:
        path = Path(daily_report_ref.path)
        if not path.is_file():
            return failure(K.UNAVAILABLE, "PRODUCT_UNAVAILABLE", daily_report_ref)
        candidates = (path,)
    else:
        dated = []
        for path in settings.report_dir.glob("trade_date=*/*.json"):
            try:
                trade_date = date.fromisoformat(path.parent.name.removeprefix("trade_date="))
            except ValueError:
                continue
            eligible = trade_date == (
                target_trade_date
                if run_type is RunType.POST_CLOSE
                else market_basis_trade_date
            ) if market_basis_trade_date is not None else (
                trade_date == target_trade_date
                if run_type is RunType.POST_CLOSE
                else trade_date < target_trade_date
            )
            if eligible:
                dated.append((trade_date, path))
        if not dated:
            return failure(K.UNAVAILABLE, "PRODUCT_UNAVAILABLE")
        basis = max(item[0] for item in dated)
        candidates = tuple(sorted(path for trade_date, path in dated if trade_date == basis))

    loaded = []
    for path in candidates:
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return failure(K.UNAVAILABLE, "PRODUCT_UNAVAILABLE", daily_report_ref)
        except OSError:
            return failure(K.FAILED, "INTERNAL_FAILURE", daily_report_ref)
        if daily_report_ref is not None and hashlib.sha256(raw).hexdigest() != (
            daily_report_ref.artifact_id
        ):
            return failure(
                K.CORRUPT, "PRODUCT_ARTIFACT_HASH_MISMATCH", daily_report_ref,
            )
        try:
            payload = json.loads(raw, parse_float=Decimal)
            report = report_from_dict(payload)
        except ValueError as error:
            reason = (
                "PRODUCT_IDENTITY_MISMATCH"
                if "identity" in str(error).lower()
                else "PRODUCT_CORRUPT"
            )
            return failure(K.CORRUPT, reason, daily_report_ref)
        except (KeyError, TypeError, UnicodeDecodeError):
            return failure(K.CORRUPT, "PRODUCT_CORRUPT", daily_report_ref)
        except Exception:
            return failure(K.FAILED, "INTERNAL_FAILURE", daily_report_ref)
        if report.universe_name != universe_name:
            if daily_report_ref is not None:
                return failure(
                    K.FAILED_INTEGRITY, "PRODUCT_UNIVERSE_MISMATCH",
                    daily_report_ref,
                )
            continue
        if report.candidate_summary["requested_top_n"] != top_n:
            if daily_report_ref is not None:
                return failure(
                    K.FAILED_INTEGRITY, "PRODUCT_TOP_N_MISMATCH",
                    daily_report_ref,
                )
            continue
        loaded.append((path, raw, report))

    if not loaded:
        return failure(K.UNAVAILABLE, "PRODUCT_UNAVAILABLE", daily_report_ref)
    if len(loaded) != 1:
        return failure(K.FAILED_INTEGRITY, "PRODUCT_SELECTION_AMBIGUOUS")
    path, raw, report = loaded[0]
    reference = daily_report_ref or ArtifactReference(
        hashlib.sha256(raw).hexdigest(), str(path.resolve()), report.schema_version,
    )
    if reference.version != report.schema_version:
        return failure(K.CORRUPT, "PRODUCT_SCHEMA_MISMATCH", reference)
    if report.mode != mode:
        return failure(K.FAILED_INTEGRITY, "PRODUCT_MODE_MISMATCH", reference)
    expected_date = (
        report.trade_date == target_trade_date
        if run_type is RunType.POST_CLOSE
        else (
            report.trade_date == market_basis_trade_date
            if market_basis_trade_date is not None
            else report.trade_date < target_trade_date
        )
    )
    if not expected_date:
        return failure(K.STALE, "PRODUCT_DATE_MISMATCH", reference)
    if max(report.as_of_time, report.generated_at) > as_of_time:
        return failure(K.STALE, "PRODUCT_NOT_PIT_VISIBLE", reference)
    markdown = path.with_suffix(".md")
    try:
        markdown_bytes = markdown.read_bytes()
    except FileNotFoundError:
        return failure(K.CORRUPT, "PRODUCT_CORRUPT", reference)
    except OSError:
        return failure(K.FAILED, "INTERNAL_FAILURE", reference)
    try:
        expected_markdown = render_daily_report(report).encode()
    except Exception:
        return failure(K.FAILED, "INTERNAL_FAILURE", reference)
    if markdown_bytes != expected_markdown:
        return failure(K.CORRUPT, "PRODUCT_CORRUPT", reference)

    try:
        state = _knowledge_operational_state(report, reference)
        daily_artifacts = _daily_report_artifacts(report, reference, mode=mode)
    except (KeyError, TypeError, ValueError):
        return failure(K.CORRUPT, "PRODUCT_CORRUPT", reference)
    except Exception:
        return failure(K.FAILED, "INTERNAL_FAILURE", reference)
    try:
        background_artifacts = load_local_artifacts(
            settings,
            target_trade_date=target_trade_date,
            as_of_time=as_of_time,
            mode=mode,
            top_n=top_n,
            universe_name=universe_name,
            daily_report_paths=(),
        )
    except Exception:
        return failure(K.FAILED, "INTERNAL_FAILURE", reference)
    return (*background_artifacts, *daily_artifacts), state


def _knowledge_operational_state(report, reference):
    from quantos.schemas.report import (
        KNOWLEDGE_REPORT_SCHEMA_VERSION, REPORT_SCHEMA_VERSION,
    )

    count = len(report.candidate_briefs)
    if report.schema_version == REPORT_SCHEMA_VERSION:
        return KnowledgeOperationalState(
            K.NOT_CONFIGURED,
            "KNOWLEDGE_NOT_CONFIGURED",
            reference,
            report.report_id,
            count,
            0,
            0,
        )
    if report.schema_version != KNOWLEDGE_REPORT_SCHEMA_VERSION:
        raise ValueError("unsupported Daily product schema")
    ready = sum(bool(item.synthesis.supplied_knowledge_refs) for item in report.candidate_briefs)
    empty = count - ready
    return KnowledgeOperationalState(
        K.READY if ready else K.EMPTY,
        "KNOWLEDGE_READY" if ready else "KNOWLEDGE_EMPTY",
        reference,
        report.report_id,
        count,
        ready,
        empty,
    )


def local_artifact_handlers(readiness: ReadinessSnapshot):
    """Network-free execution: verify/read existing module results by reference.

    The report handler reuses a PIT-visible Phase 3B JSON/Markdown pair. It never
    reruns skipped synthesis, changes its cutoff, or writes new business facts.
    """
    from quantos.reporting import report_from_dict, render_daily_report

    by_module = {module: tuple(x for x in readiness.ready_artifacts if x.module == module) for module in M}
    def handler_for(module):
        def execute(context, preceding):
            if (context.target_trade_date, context.as_of_time, context.run_type, context.mode,
                context.market_basis_trade_date) != (
                readiness.target_trade_date, readiness.as_of_time, readiness.run_type, readiness.mode,
                readiness.market_basis_trade_date,
            ):
                raise ModuleUnavailable()
            choices = by_module[module]
            if module == M.SYNTHESIS and readiness.eligible_evidence_count == 0:
                choices = by_module[M.DAILY_REPORT]
            if not choices:
                raise ModuleUnavailable()
            artifact = choices[-1]
            path = Path(artifact.reference.path)
            if path.suffix != ".json":
                if not path.is_file():
                    raise ModuleUnavailable()
                return ModuleOutcome((artifact.reference,))
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != artifact.reference.artifact_id:
                raise ModuleUnavailable()
            # Use Phase 3B's persisted-JSON reader convention for exact Markdown
            # verification; this adapter does not change numeric serialization.
            report = report_from_dict(json.loads(raw))
            if (
                readiness.knowledge_state is not None
                and artifact.reference == readiness.knowledge_state.product_ref
            ):
                markdown = path.with_suffix(".md")
                if (
                    not markdown.is_file()
                    or markdown.read_text() != render_daily_report(report)
                ):
                    raise ModuleUnavailable()
            if (report.mode != context.mode or report.trade_date != context.market_basis_trade_date
                or max(report.as_of_time, report.generated_at) > context.as_of_time):
                raise ModuleUnavailable()
            if module == M.SYNTHESIS:
                if not all(x.synthesis.status == "NO_EVIDENCE_FAST_PATH" for x in report.candidate_briefs):
                    raise ModuleUnavailable()
            if module == M.DAILY_REPORT:
                md = path.with_suffix(".md")
                if not md.is_file() or md.read_text() != render_daily_report(report):
                    raise ModuleUnavailable()
                return ModuleOutcome((artifact.reference, ArtifactReference(report.report_id, str(md), report.schema_version)),
                                     report_id=report.report_id, report_reused=True)
            return ModuleOutcome((artifact.reference,))
        return execute
    return {module: handler_for(module) for module in M}
