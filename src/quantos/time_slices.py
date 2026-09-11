"""Deterministic product assembly after Manifest; no readiness probing or providers."""

from dataclasses import asdict
from datetime import date, datetime, time
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from typing import Sequence

from quantos.config import MARKET_TIMEZONE
from quantos.reporting import (
    RESEARCH_DISCLAIMER, STRICT_DISCLAIMER, canonical_json_bytes,
    render_daily_report, report_from_dict, report_to_dict,
)
from quantos.schemas._validation import require_aware
from quantos.schemas.discovery import WebSearchResult
from quantos.schemas.news import AnnouncementRecord, NewsRecord
from quantos.schemas.report import DailyIntelligenceReport
from quantos.schemas.run import (
    ArtifactReference, ModuleAvailabilityStatus as A, ModuleExecutionStatus as E,
    ModuleName as M, QuantOSRunManifest, RunType,
)
from quantos.schemas.time_slice import (
    EvidenceView, EvidenceWindowResult, IntradayPayload, PostClosePayload,
    KNOWLEDGE_TIME_SLICE_SCHEMA_VERSION, PreOpenPayload, PreviousKnowledgeBrief,
    ProductAvailability as P, ReportType, TEMPORAL_POLICY_VERSION,
    TIME_SLICE_SCHEMA_VERSION, TemporalBucket as B, TimeSliceIntelligenceReport,
    WatchlistEntry,
)

REPORT_TYPES = {RunType.PRE_OPEN: ReportType.PRE_OPEN_BRIEF,
                RunType.INTRADAY: ReportType.INTRADAY_BRIEF,
                RunType.POST_CLOSE: ReportType.POST_CLOSE_REPORT}
INTRADAY_DISCLAIMER = (
    "QuantOS V1 当前未接入 canonical 盘中行情数据，本报告仅展示最近完成交易日背景及截至当前时点"
    "新增的事件证据，不代表当前实时市场表现。"
)
WINDOW_POLICY = "Asia/Shanghai：上一收盘后至目标日零点为隔夜，零点至09:30为盘前，09:30起为开盘后；不推断交易日。"


def classify_temporal_bucket(*, published_at, available_at, previous_market_event_time,
                            previous_close_time, current_open_time, as_of_time,
                            mode, strict_pit, previous_attribution=False):
    """View-only classification; never grants attribution eligibility.

    All records must already be available by the run cutoff, including research.
    Research proxy uses publication for the window only and retains proxy status.
    This is the repository corpus cutoff, not strict knowledge-at-event eligibility.
    With E=event, C=close, M=target midnight, O=open and K=window time:
    prior attribution is an existing view at K<=E; context windows are (E,C],
    (C,M), [M,O), [O,as_of]. E=C is valid for a completed daily event.
    """
    for value in (available_at, previous_market_event_time, previous_close_time, current_open_time, as_of_time):
        require_aware(value, "window timestamp")
    if published_at is not None:
        require_aware(published_at, "published_at")
    if mode not in {"research", "strict_live"}:
        raise ValueError("explicit mode required")
    midnight = datetime.combine(current_open_time.astimezone(MARKET_TIMEZONE).date(), time(), MARKET_TIMEZONE)
    if not previous_market_event_time <= previous_close_time < midnight < current_open_time:
        raise ValueError("invalid market window")
    if available_at > as_of_time or (published_at is not None and published_at > available_at):
        return None
    if mode == "strict_live" and not strict_pit:
        return None
    proxy = not strict_pit
    if proxy and published_at is None:
        return None
    known = published_at if proxy else available_at
    if known <= previous_market_event_time:
        return B.PREVIOUS_ATTRIBUTION if previous_attribution else None
    if previous_market_event_time < known <= previous_close_time:
        return B.POST_EVENT_CONTEXT
    if previous_close_time < known < midnight:
        return B.OVERNIGHT_CONTEXT
    if midnight <= known < current_open_time:
        return B.PRE_OPEN_CONTEXT
    if current_open_time <= known <= as_of_time:
        return B.SINCE_OPEN_CONTEXT
    return None


def evidence_window(records: Sequence[WebSearchResult | AnnouncementRecord | NewsRecord], *, symbols,
                    previous_market_event_time, previous_close_time, current_open_time,
                    as_of_time, mode, previous_attribution_ids=frozenset(), buckets=None, mentions=()):
    """Existing directly linked records become small views; text is never copied."""
    views = {}
    for record in records:
        if isinstance(record, WebSearchResult):
            identity, linked_symbols = record.result_id, (record.query_symbol,)
        elif isinstance(record, AnnouncementRecord):
            identity, linked_symbols = record.announcement_id, (record.symbol,)
        elif isinstance(record, NewsRecord):
            identity = record.news_id
            linked_symbols = tuple(sorted({x.symbol for x in mentions if x.news_id == identity
                                           and x.available_at <= as_of_time}))
        else:
            raise TypeError("canonical directly linked evidence required")
        linked_symbols = tuple(symbol for symbol in linked_symbols if symbol in symbols)
        if not linked_symbols:
            continue
        if not record.strict_pit and record.timestamp_basis not in {"publisher_reported", "provider_reported"}:
            continue
        bucket = classify_temporal_bucket(
            published_at=record.published_at, available_at=record.available_at,
            previous_market_event_time=previous_market_event_time, previous_close_time=previous_close_time,
            current_open_time=current_open_time, as_of_time=as_of_time, mode=mode,
            strict_pit=record.strict_pit, previous_attribution=identity in previous_attribution_ids,
        )
        if bucket is None or (buckets is not None and bucket not in buckets):
            continue
        for symbol in linked_symbols:
            view = EvidenceView(identity, symbol, record.provider, bucket, record.published_at,
                                record.available_at, record.strict_pit, not record.strict_pit)
            key = (identity, symbol, record.provider)
            if key in views and views[key] != view:
                raise ValueError("conflicting evidence identity")
            views[key] = view
    return summarize_views(tuple(views[key] for key in sorted(views)))


def summarize_views(views):
    providers = sorted({x.provider for x in views})
    return EvidenceWindowResult(tuple(views), {b.value: len({(x.provider, x.evidence_id) for x in views if x.temporal_bucket == b}) for b in B},
        tuple({"provider": provider, "record_count": len({x.evidence_id for x in views if x.provider == provider}),
               "candidate_coverage": len({x.symbol for x in views if x.provider == provider})} for provider in providers))


def availability_from_manifest(manifest):
    plans = {x.module: x for x in manifest.plan}
    results = {x.module: x for x in manifest.execution_results}
    summary = {}
    for module in (M.MARKET_CONTEXT, M.SECTOR_CONTEXT, M.FUND_FLOW, M.EVIDENCE, M.ATTRIBUTION, M.SYNTHESIS):
        plan, result = plans[module], results[module]
        if result.status == E.FAIL:
            status = P.FAILED
        elif plan.availability == A.READY and result.status == E.PASS:
            status = P.READY
        elif plan.availability == A.SKIP_UNSUPPORTED_IN_V1:
            status = P.UNSUPPORTED
        else:
            status = P.UNAVAILABLE
        summary[module.value.lower()] = status
    return summary


def assemble_time_slice(manifest: QuantOSRunManifest, *, manifest_ref: ArtifactReference,
                        daily_report: DailyIntelligenceReport | None = None,
                        daily_report_ref: ArtifactReference | None = None,
                        records=(), mentions=(), generated_at: datetime):
    """Consume completed run facts, preserving previous ranks and canonical numerics."""
    if dict(manifest.provenance).get("execution_mode") == "PLAN_ONLY":
        raise ValueError("plan-only cannot generate a product")
    context = manifest.run_context
    if manifest_ref.artifact_id != context.run_id:
        raise ValueError("manifest reference mismatch")
    availability = availability_from_manifest(manifest)
    states = {x.module: x.status for x in manifest.execution_results}
    if daily_report is not None:
        source_module = M.DAILY_REPORT if context.run_type == RunType.POST_CLOSE else M.ANOMALY_TRIAGE
        source_refs = tuple(ref for result in manifest.execution_results
                            if result.module == source_module and result.status == E.PASS for ref in result.artifact_refs)
        if daily_report_ref is None or daily_report_ref not in source_refs:
            raise ValueError("source report must be referenced by the executed manifest")
        if (daily_report.trade_date != context.market_basis_trade_date or daily_report.mode != context.mode
            or daily_report.universe_name != context.universe_name
            or max(daily_report.as_of_time, daily_report.generated_at) > context.as_of_time):
            raise ValueError("report is not a PIT-valid matching source")
        if states[M.MARKET_CONTEXT] != E.PASS or states[M.ANOMALY_TRIAGE] != E.PASS:
            raise ValueError("source background requires successful manifest modules")
        if daily_report.candidate_summary["requested_top_n"] < context.top_n:
            raise ValueError("source ranking does not cover requested top_n")
    if context.run_type == RunType.POST_CLOSE:
        if daily_report is not None:
            if states[M.DAILY_REPORT] != E.PASS or manifest.summary.report_id != daily_report.report_id:
                raise ValueError("post-close source must be the manifest report")
            if daily_report_ref is None:
                raise ValueError("post-close requires a daily report reference")
            if availability["fund_flow"] != P.READY and daily_report.data_quality["fund_flow"]["covered_candidates"]:
                raise ValueError("daily report fund-flow disagrees with manifest")
            _validate_daily_artifact_reference(daily_report, daily_report_ref)
        payload = PostClosePayload(daily_report_ref if daily_report else None, availability, manifest_ref)
        views = ()
    else:
        if daily_report is not None:
            _validate_daily_artifact_reference(daily_report, daily_report_ref)
        selected = tuple(x for x in daily_report.candidate_briefs if x.rank <= context.top_n) if daily_report else ()
        opened = datetime.combine(context.target_trade_date, time(9, 30), MARKET_TIMEZONE)
        if context.run_type == RunType.PRE_OPEN and context.as_of_time > opened:
            raise ValueError("pre-open product cutoff cannot follow opening")
        if context.run_type == RunType.INTRADAY and context.as_of_time < opened:
            raise ValueError("intraday product cutoff cannot precede opening")
        if context.market_basis_trade_date is not None:
            closed = datetime.combine(context.market_basis_trade_date, time(15), MARKET_TIMEZONE)
            window = evidence_window(records, symbols={x.symbol for x in selected},
                previous_market_event_time=closed, previous_close_time=closed, current_open_time=opened,
                as_of_time=context.as_of_time, mode=context.mode, mentions=mentions,
                buckets=({B.OVERNIGHT_CONTEXT, B.PRE_OPEN_CONTEXT} if context.run_type == RunType.PRE_OPEN
                         else {B.OVERNIGHT_CONTEXT, B.PRE_OPEN_CONTEXT, B.SINCE_OPEN_CONTEXT}))
        else:
            window = summarize_views(())
        watchlist = []
        for candidate in selected:
            own = tuple(x for x in window.views if x.symbol == candidate.symbol)
            overnight = sum(x.temporal_bucket == B.OVERNIGHT_CONTEXT for x in own)
            preopen = sum(x.temporal_bucket == B.PRE_OPEN_CONTEXT for x in own)
            # Existing canonical signal labels, not recalculated anomaly thresholds.
            reasons = [f"PREVIOUS_{signal.upper()}_ANOMALY" for signal in ("price", "volume", "amount")
                       if any(signal in label.split("_") for label in candidate.signal_types)]
            reasons += (["OVERNIGHT_EVENT_PRESENT"] if overnight else []) + (["PRE_OPEN_EVENT_PRESENT"] if preopen else [])
            watchlist.append(WatchlistEntry(candidate.rank, candidate.symbol, candidate.name, candidate.tier,
                candidate.signal_types, candidate.market_facts, overnight, preopen, tuple(reasons)))
        views = window.views
        if context.run_type == RunType.PRE_OPEN:
            knowledge_briefs = tuple(
                PreviousKnowledgeBrief(
                    candidate.rank,
                    candidate.symbol,
                    candidate.synthesis.input_bundle_id,
                    candidate.synthesis.cache_identity,
                    candidate.synthesis.historical_knowledge_background,
                    candidate.synthesis.retrospective_research_context,
                    candidate.synthesis.supplied_context_id,
                    candidate.synthesis.supplied_knowledge_refs,
                )
                for candidate in selected
                if candidate.synthesis.supplied_context_id is not None
            )
            payload = PreOpenPayload(daily_report.market_overview if daily_report else None,
                daily_report.anomaly_overview if daily_report else None, tuple(watchlist),
                summarize_views(tuple(x for x in views if x.temporal_bucket != B.SINCE_OPEN_CONTEXT)),
                availability, knowledge_briefs)
        else:
            since = summarize_views(tuple(x for x in views if x.temporal_bucket == B.SINCE_OPEN_CONTEXT))
            updates = tuple({"symbol": x.symbol,
                "new_event_count_since_open": sum(v.symbol == x.symbol for v in since.views),
                "new_provider_records": list(summarize_views(tuple(v for v in since.views if v.symbol == x.symbol)).providers),
                "latest_evidence_available_at": max((v.available_at for v in since.views if v.symbol == x.symbol), default=None)}
                for x in selected)
            payload = IntradayPayload({"canonical_intraday_supported": False, "status": "UNSUPPORTED_IN_V1"},
                {"market_overview": daily_report.market_overview, "anomaly_overview": daily_report.anomaly_overview}
                if daily_report else None, tuple(watchlist), since, updates, availability)
    proxy = any(x.historical_proxy_used for x in views)
    if daily_report is not None and context.run_type == RunType.POST_CLOSE:
        proxy = daily_report.data_quality["evidence"]["historical_proxy_used"]
    time_slice_schema_version = (
        KNOWLEDGE_TIME_SLICE_SCHEMA_VERSION
        if isinstance(payload, PreOpenPayload) and payload.previous_knowledge_briefs
        else TIME_SLICE_SCHEMA_VERSION
    )
    return TimeSliceIntelligenceReport(time_slice_schema_version, REPORT_TYPES[context.run_type],
        context.target_trade_date, context.market_basis_trade_date, context.as_of_time, generated_at,
        context.mode, context.universe_name, manifest_ref, payload,
        {"historical_proxy_used": proxy, "strict_pit_only": context.mode == "strict_live",
         "background_available": daily_report is not None,
         "source_scope": "LOCAL_CANONICAL_ARTIFACTS", "window_policy": WINDOW_POLICY,
         "run_execution_summary": {"passed_modules": manifest.summary.passed_modules,
             "skipped_modules": manifest.summary.skipped_modules, "failed_modules": manifest.summary.failed_modules},
         "limitations": ["观察名单不构成预测或投资建议。", "模块可用性来自 Run Manifest；事件窗口视图不改变 attribution 模块状态。"]},
        {"time_slice_schema_version": time_slice_schema_version, "temporal_policy_version": TEMPORAL_POLICY_VERSION,
         "run_schema_version": manifest.schema_version,
         "source_daily_report_id": daily_report.report_id if daily_report else None,
         "orchestration_policy_version": dict(manifest.provenance).get("orchestration_policy_version")})


def _validate_daily_artifact_reference(daily_report, daily_report_ref):
    canonical_daily = canonical_json_bytes(report_to_dict(daily_report)) + b"\n"
    if (
        daily_report_ref is None
        or daily_report_ref.version != daily_report.schema_version
        or daily_report_ref.artifact_id != hashlib.sha256(canonical_daily).hexdigest()
    ):
        raise ValueError("source report artifact reference mismatch")


def time_slice_to_record(report):
    value = asdict(report)
    if report.schema_version == TIME_SLICE_SCHEMA_VERSION and isinstance(
        report.payload, PreOpenPayload
    ):
        value["payload"].pop("previous_knowledge_briefs", None)
    return {**value, "report_id": report.report_id}


def time_slice_from_record(record):
    value = dict(record)
    identity = value.pop("report_id")
    value["report_type"] = ReportType(value["report_type"])
    for field in ("target_trade_date", "market_basis_trade_date"):
        value[field] = date.fromisoformat(value[field]) if value[field] else None
    for field in ("as_of_time", "generated_at"):
        value[field] = datetime.fromisoformat(value[field])
    value["run_context_ref"] = ArtifactReference(**value["run_context_ref"])
    payload = dict(value["payload"])
    payload["availability_summary"] = {k: P(v) for k, v in payload["availability_summary"].items()}
    if value["report_type"] == ReportType.POST_CLOSE_REPORT:
        ref = payload["daily_intelligence_report_ref"]
        payload["daily_intelligence_report_ref"] = ArtifactReference(**ref) if ref else None
        payload["run_manifest_ref"] = ArtifactReference(**payload["run_manifest_ref"])
        value["payload"] = PostClosePayload(**payload)
    else:
        payload["watchlist"] = tuple(WatchlistEntry(**{**x, "previous_signal_types": tuple(x["previous_signal_types"]),
                                                     "watch_reason_codes": tuple(x["watch_reason_codes"])}) for x in payload["watchlist"])
        window_key = "overnight_evidence_overview" if value["report_type"] == ReportType.PRE_OPEN_BRIEF else "evidence_since_open"
        window = payload[window_key]
        views = tuple(EvidenceView(**{**x, "temporal_bucket": B(x["temporal_bucket"]),
            "published_at": datetime.fromisoformat(x["published_at"]) if x["published_at"] else None,
            "available_at": datetime.fromisoformat(x["available_at"])}) for x in window["views"])
        payload[window_key] = EvidenceWindowResult(views, window["bucket_counts"], tuple(window["providers"]))
        if value["report_type"] == ReportType.PRE_OPEN_BRIEF:
            payload["previous_knowledge_briefs"] = tuple(
                _previous_knowledge_brief_from_record(item)
                for item in payload.get("previous_knowledge_briefs", ())
            )
        if value["report_type"] == ReportType.INTRADAY_BRIEF:
            payload["candidate_event_updates"] = tuple({**x, "latest_evidence_available_at":
                datetime.fromisoformat(x["latest_evidence_available_at"]) if x["latest_evidence_available_at"] else None}
                for x in payload["candidate_event_updates"])
        value["payload"] = PreOpenPayload(**payload) if value["report_type"] == ReportType.PRE_OPEN_BRIEF else IntradayPayload(**payload)
    report = TimeSliceIntelligenceReport(**value)
    if report.report_id != identity:
        raise ValueError("time-slice identity mismatch")
    return report


def _previous_knowledge_brief_from_record(value):
    from quantos.schemas.synthesis import KnowledgeBackgroundStatement

    expected = {
        "rank", "symbol", "synthesis_input_bundle_id",
        "synthesis_cache_identity", "historical_knowledge_background",
        "retrospective_research_context", "supplied_context_id",
        "supplied_knowledge_refs",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError("previous Knowledge brief fields disagree with schema version")

    def statements(name):
        result = []
        for item in value[name]:
            if not isinstance(item, dict) or set(item) != {"statement", "knowledge_refs"}:
                raise ValueError("previous Knowledge statement fields are invalid")
            result.append(KnowledgeBackgroundStatement(
                statement=item["statement"],
                knowledge_refs=tuple(item["knowledge_refs"]),
            ))
        return tuple(result)

    return PreviousKnowledgeBrief(
        rank=value["rank"],
        symbol=value["symbol"],
        synthesis_input_bundle_id=value["synthesis_input_bundle_id"],
        synthesis_cache_identity=value["synthesis_cache_identity"],
        historical_knowledge_background=statements("historical_knowledge_background"),
        retrospective_research_context=statements("retrospective_research_context"),
        supplied_context_id=value["supplied_context_id"],
        supplied_knowledge_refs=tuple(value["supplied_knowledge_refs"]),
    )


def _display(value):
    # Canonical values only. No model prose, financial interpretations or invented facts.
    if value is None:
        return "unavailable"
    return "```json\n" + canonical_json_bytes(value).decode() + "\n```"


def render_time_slice(report, *, daily_report_loader=None):
    info = (f"目标交易日：{report.target_trade_date}；市场背景日期：{report.market_basis_trade_date or 'unavailable'}；"
            f"as_of_time：{report.as_of_time.isoformat()}；mode：{report.mode}；"
            f"report_type：{report.report_type.value}；run_id：{report.run_context_ref.artifact_id}")
    disclaimer = RESEARCH_DISCLAIMER if report.mode == "research" else STRICT_DISCLAIMER
    payload = report.payload
    if isinstance(payload, PostClosePayload):
        if payload.daily_intelligence_report_ref is None:
            core = "# QuantOS Daily Intelligence\n\nDailyIntelligenceReport unavailable：Run Manifest 未提供可用报告。\n"
        else:
            if daily_report_loader is None:
                raise ValueError("post-close rendering requires referenced daily report")
            daily = daily_report_loader(payload.daily_intelligence_report_ref)
            if (daily.report_id != report.provenance["source_daily_report_id"] or daily.mode != report.mode
                or daily.trade_date != report.market_basis_trade_date
                or max(daily.as_of_time, daily.generated_at) > report.as_of_time):
                raise ValueError("post-close referenced report mismatch")
            core = render_daily_report(daily)
        return core + "\n## 时间切片元数据\n\n" + info + "\n\n" + disclaimer + "\n\n" + _display(payload) + "\n"
    intraday = isinstance(payload, IntradayPayload)
    title = "QuantOS Intraday Brief" if intraday else "QuantOS Pre-Open Brief"
    lines = [f"# {title}", ""]
    if intraday:
        lines += [INTRADAY_DISCLAIMER, ""]
    sections = [("报告信息", info)]
    if intraday:
        sections += [("实时能力说明", INTRADAY_DISCLAIMER),
                     ("最近完成交易日背景", _display(payload.previous_market_context))]
    else:
        sections += [("数据时点", WINDOW_POLICY), ("昨日市场概览", _display(payload.previous_market_overview)),
                     ("昨日异常扫描", _display(payload.previous_anomaly_overview))]
        if payload.previous_knowledge_briefs:
            sections.append(("Historical Knowledge Background", _display(tuple({
                "rank": item.rank,
                "symbol": item.symbol,
                "synthesis_input_bundle_id": item.synthesis_input_bundle_id,
                "supplied_context_id": item.supplied_context_id,
                "supplied_knowledge_refs": item.supplied_knowledge_refs,
                "statements": item.historical_knowledge_background,
            } for item in payload.previous_knowledge_briefs))))
            retrospective = tuple({
                    "rank": item.rank,
                    "symbol": item.symbol,
                    "statements": item.retrospective_research_context,
                } for item in payload.previous_knowledge_briefs
                    if item.retrospective_research_context)
            if retrospective:
                sections.append(("Retrospective Research Context", _display(retrospective)))
    sections += [("今日观察名单", _display(payload.watchlist)),
        ("开盘后新增事件" if intraday else "隔夜与盘前新增事件",
         _display({"window": payload.evidence_since_open, "candidate_updates": payload.candidate_event_updates}) if intraday
         else _display(payload.overnight_evidence_overview)),
        ("当前数据可用性" if intraday else "当前不可用数据", _display(payload.availability_summary)),
        ("数据质量与限制", disclaimer + "\n\n" + _display(report.data_quality))]
    if not intraday:
        sections += [("方法与版本", _display(report.provenance))]
    for name, body in sections:
        lines += [f"## {name}", "", body, ""]
    return "\n".join(lines)


def load_daily_reference(reference):
    """Read only an explicitly supplied local canonical artifact; verify identity."""
    raw = Path(reference.path).read_bytes()
    if hashlib.sha256(raw).hexdigest() != reference.artifact_id:
        raise ValueError("daily report artifact changed")
    # Frozen Phase 3B renderer reads standard JSON floats.
    return report_from_dict(json.loads(raw))
