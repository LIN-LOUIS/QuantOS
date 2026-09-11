"""E2E.2 Knowledge product surface, identity, and artifact reuse tests."""

from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import quantos.knowledge_integration as knowledge_integration
from quantos.knowledge_integration import (
    KnowledgePreparationError, KnowledgePreparationStatus,
)
from quantos.config import (
    MARKET_TIMEZONE, KnowledgeIntegrationSettings, Settings,
)
from quantos.orchestration import ModuleOutcome, collect_readiness, execute_run
from quantos.reporting import (
    canonical_json_bytes, generate_daily_report, render_daily_report,
    report_from_dict, report_to_dict,
)
from quantos.schemas.report import (
    KNOWLEDGE_REPORT_SCHEMA_VERSION, REPORT_SCHEMA_VERSION,
)
from quantos.schemas.run import (
    ArtifactReadiness, ArtifactReference, ModuleName as M, RunContext, RunType,
)
from quantos.schemas.synthesis import KnowledgeBackgroundStatement
from quantos.schemas.time_slice import KNOWLEDGE_TIME_SLICE_SCHEMA_VERSION
from quantos.storage import DailyReportRepository, StorageError, SynthesisRepository
from quantos.storage.run import RunRepository
from quantos.synthesis import LLMGeneration
from quantos.time_slices import (
    assemble_time_slice, render_time_slice, time_slice_from_record,
    time_slice_to_record,
)
from tests.test_knowledge_integration import CUTOFF, EVENT, deployment, document
from tests.test_reporting import artifacts, build_report
from tests.test_synthesis import NOW, valid_payload


TARGET = date(2026, 9, 4)
PRE_OPEN = datetime(2026, 9, 4, 9, tzinfo=MARKET_TIMEZONE)


class KnowledgeFactory:
    def __init__(self, statement="公司从事环保业务。", *, emit_background=True):
        self.calls = 0
        self.statement = statement
        self.emit_background = emit_background

    def __call__(self, value):
        parent = self

        class Client:
            def generate_structured(self, **_kwargs):
                parent.calls += 1
                context = value.knowledge_context
                historical = [
                    f"K{item.retrieval_rank}" for item in context.historical_items
                ]
                retrospective = [
                    f"K{item.retrieval_rank}" for item in context.retrospective_items
                ]
                if value.attribution_evidence:
                    payload = valid_payload(
                        mode=value.mode,
                        evidence_id=value.attribution_evidence[0].evidence_id,
                    )
                else:
                    payload = {
                        "symbol": value.symbol,
                        "mode": value.mode,
                        "evidence_summary": "",
                        "possible_explanations": [],
                        "contradicting_signals": [],
                        "post_event_notes": [],
                        "insufficient_evidence": True,
                        "limitations": ["仅提供背景知识。"],
                        "used_evidence_ids": [],
                    }
                payload["symbol"] = value.symbol
                payload["post_event_notes"] = []
                payload["knowledge_background"] = (
                    [{"statement": parent.statement, "knowledge_refs": historical}]
                    if historical and parent.emit_background else []
                )
                payload["retrospective_knowledge_context"] = (
                    [{"statement": "该材料仅属于事后研究背景。",
                      "knowledge_refs": retrospective}]
                    if retrospective and parent.emit_background else []
                )
                return LLMGeneration(
                    payload, "deepseek", "deepseek-v4-flash", 10, 5, 15,
                )

        return Client()


def _daily_cli():
    spec = importlib.util.spec_from_file_location(
        "daily_knowledge_cli", Path("scripts/generate_daily_report.py"),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def knowledge_report(
    root,
    *,
    historical_content="公司1 601330.SH 环保历史背景",
    include_retrospective=True,
    statement="公司从事环保业务。",
    generated_at=NOW,
    mode="research",
    emit_background=True,
):
    market, anomalies, candidates, names, attribution, _ = artifacts(
        count=1, strict=mode == "strict_live",
    )
    documents = [
        document("company:601330", historical_content, available_at=EVENT),
    ]
    if include_retrospective and mode == "research":
        documents.append(document(
            "company:601330:later",
            "公司1 601330.SH 事后研究资料",
            available_at=EVENT + timedelta(days=1),
        ))
    knowledge, indexes, contexts, settings, _ = deployment(
        root / "knowledge", tuple(documents),
    )
    inputs = _daily_cli()._prepare_synthesis_inputs(
        attribution.bundles[:1],
        attribution.attribution_facts,
        company_names=names,
        mode=mode,
        market_event_time=EVENT,
        research_corpus_cutoff=NOW if mode == "research" else None,
        generated_at=generated_at,
        knowledge_settings=settings,
        knowledge_repository=knowledge,
        index_repository=indexes,
        context_repository=contexts,
    )
    factory = KnowledgeFactory(statement, emit_background=emit_background)
    report = generate_daily_report(
        market_snapshot=market,
        sector_snapshots=(),
        anomalies=anomalies,
        candidates=candidates,
        company_names=names,
        attribution=attribution,
        synthesis_inputs=inputs,
        client_factory=factory,
        synthesis_repository=SynthesisRepository(
            Settings.from_project_root(root / "synthesis"),
        ),
        mode=mode,
        top_n=1,
        generated_at=generated_at,
        provenance={
            "quantos_git_commit": "fixture",
            "synthesis_prompt_version": inputs[0].prompt_version,
            "synthesis_schema_version": inputs[0].schema_version,
        },
    )
    return report, factory, inputs[0]


def manifest_for_daily(root, daily, *, run_type):
    settings = Settings.from_project_root(root)
    json_path, _ = DailyReportRepository(settings).write(daily)
    daily_ref = ArtifactReference(
        hashlib.sha256(json_path.read_bytes()).hexdigest(),
        str(json_path),
        daily.schema_version,
    )
    cutoff = PRE_OPEN if run_type is RunType.PRE_OPEN else NOW + timedelta(hours=1)
    observations = tuple(
        ArtifactReadiness(
            module,
            daily.trade_date,
            max(daily.as_of_time, daily.generated_at),
            daily_ref,
            artifact_as_of_time=daily.as_of_time,
            mode=daily.mode,
            strict_pit=False,
            eligible_evidence_count=(
                sum(
                    item.evidence_stats.eligible_attribution_count
                    for item in daily.candidate_briefs
                )
                if module is M.ATTRIBUTION else None
            ),
        )
        for module in M
        if module not in {M.SECTOR_CONTEXT, M.SYNTHESIS}
    )
    target = TARGET if run_type is RunType.PRE_OPEN else daily.trade_date
    readiness = collect_readiness(
        target_trade_date=target,
        as_of_time=cutoff,
        run_type=run_type,
        mode=daily.mode,
        artifacts=observations,
    )
    context = RunContext(
        target,
        readiness.market_basis_trade_date,
        cutoff,
        run_type,
        daily.mode,
        daily.universe_name,
        1,
        False,
        cutoff,
    )

    def handler(_context, _preceding):
        return ModuleOutcome((daily_ref,), report_id=daily.report_id, report_reused=True)

    manifest = execute_run(
        context,
        readiness,
        handlers={module: handler for module in M},
        clock=lambda: cutoff,
    )
    manifest_path = RunRepository(settings).write(manifest)
    manifest_ref = ArtifactReference(
        context.run_id, str(manifest_path), manifest.schema_version,
    )
    return manifest, manifest_ref, daily_ref


def test_legacy_report_stays_v1_and_omits_knowledge_payload(tmp_path):
    report, _, _ = build_report(tmp_path)
    encoded = report_to_dict(report)
    assert report.schema_version == REPORT_SCHEMA_VERSION
    assert "supplied_context_id" not in encoded["candidate_briefs"][0]["synthesis"]


def test_ready_knowledge_reaches_daily_surface_roundtrip_and_renderer(tmp_path):
    report, factory, synthesis_input = knowledge_report(tmp_path)
    brief = report.candidate_briefs[0].synthesis
    assert factory.calls == 1
    assert report.schema_version == KNOWLEDGE_REPORT_SCHEMA_VERSION
    assert brief.supplied_context_id == synthesis_input.knowledge_context.context_id
    assert brief.supplied_knowledge_refs == ("K1", "K2")
    assert brief.historical_knowledge_background[0].knowledge_refs == ("K1",)
    assert brief.retrospective_research_context[0].knowledge_refs == ("K2",)
    decoded = report_from_dict(json.loads(
        canonical_json_bytes(report_to_dict(report)), parse_float=Decimal,
    ))
    assert canonical_json_bytes(report_to_dict(decoded)) == canonical_json_bytes(
        report_to_dict(report)
    )
    markdown = render_daily_report(decoded)
    assert "Historical Knowledge Background" in markdown
    assert "Retrospective Research Context" in markdown
    assert "Supporting Knowledge" not in markdown


def test_report_identity_binds_context_and_synthesis_output_not_generated_at(tmp_path):
    first, _, first_input = knowledge_report(tmp_path / "first")
    changed_context, _, changed_input = knowledge_report(
        tmp_path / "context", historical_content="公司1 601330.SH 不同历史背景",
    )
    changed_output, _, same_input = knowledge_report(
        tmp_path / "output", statement="公司属于公用事业行业。",
    )
    later, _, later_input = knowledge_report(
        tmp_path / "later", generated_at=NOW + timedelta(hours=1),
    )
    assert first_input.knowledge_context.context_id != changed_input.knowledge_context.context_id
    assert first.report_id != changed_context.report_id
    assert first_input.input_bundle_id == same_input.input_bundle_id
    assert first.report_id != changed_output.report_id
    assert first_input.input_bundle_id == later_input.input_bundle_id
    assert first.report_id == later.report_id


def test_candidate_rank_order_and_duplicates_fail_closed(tmp_path):
    report, _, _ = knowledge_report(tmp_path)
    first = report.candidate_briefs[0]
    second = replace(first, rank=2, symbol="601331.SH", name="公司2")
    summary = {
        **report.candidate_summary,
        "total_candidates": 2,
        "selected_for_report": 2,
        "requested_top_n": 2,
    }
    with pytest.raises(ValueError, match="contiguous rank order"):
        replace(
            report,
            candidate_summary=summary,
            candidate_briefs=(second, first),
        )
    with pytest.raises(ValueError, match="contiguous rank order"):
        replace(
            report,
            candidate_summary=summary,
            candidate_briefs=(first, replace(second, rank=1)),
        )


def test_cache_replay_does_not_change_logical_report_identity(tmp_path):
    generated, first_factory, _ = knowledge_report(tmp_path)
    replayed, replay_factory, _ = knowledge_report(tmp_path)
    assert first_factory.calls == 1 and replay_factory.calls == 0
    assert generated.candidate_briefs[0].synthesis.status == "GENERATED"
    assert replayed.candidate_briefs[0].synthesis.status == "CACHE_HIT"
    assert generated.report_id == replayed.report_id


def test_empty_context_is_v2_and_distinct_from_not_configured(tmp_path):
    report, factory, synthesis_input = knowledge_report(
        tmp_path / "empty",
        historical_content="unrelated text",
        include_retrospective=False,
    )
    brief = report.candidate_briefs[0].synthesis
    assert synthesis_input.knowledge_context.selected_item_count == 0
    assert brief.supplied_context_id == synthesis_input.knowledge_context.context_id
    assert brief.supplied_knowledge_refs == ()
    assert brief.historical_knowledge_background == ()
    assert factory.calls == 1
    assert report.schema_version == KNOWLEDGE_REPORT_SCHEMA_VERSION
    legacy, _, _ = build_report(tmp_path / "legacy")
    assert report.report_id != legacy.report_id


def test_ready_context_with_zero_statements_remains_v2(tmp_path):
    report, factory, synthesis_input = knowledge_report(
        tmp_path, include_retrospective=False, emit_background=False,
    )
    brief = report.candidate_briefs[0].synthesis
    assert synthesis_input.knowledge_context.selected_item_count == 1
    assert factory.calls == 1
    assert report.schema_version == KNOWLEDGE_REPORT_SCHEMA_VERSION
    assert brief.supplied_context_id is not None
    assert brief.supplied_knowledge_refs == ("K1",)
    assert brief.historical_knowledge_background == ()
    with pytest.raises(ValueError, match="cache identity"):
        replace(brief, cache_identity=None)


def test_daily_loader_rejects_schema_downgrade_upgrade_and_unknown_fields(tmp_path):
    report, _, _ = knowledge_report(tmp_path / "knowledge")
    record = json.loads(
        canonical_json_bytes(report_to_dict(report)), parse_float=Decimal,
    )
    with pytest.raises(ValueError, match="synthesis brief fields"):
        report_from_dict({**record, "schema_version": REPORT_SCHEMA_VERSION})

    legacy, _, _ = build_report(tmp_path / "legacy")
    legacy_record = json.loads(
        canonical_json_bytes(report_to_dict(legacy)), parse_float=Decimal,
    )
    with pytest.raises(ValueError, match="synthesis brief fields"):
        report_from_dict({
            **legacy_record,
            "schema_version": KNOWLEDGE_REPORT_SCHEMA_VERSION,
        })
    with pytest.raises(ValueError, match="daily report fields"):
        report_from_dict({**record, "unexpected": "ignored-before-audit"})
    candidate = dict(record["candidate_briefs"][0])
    candidate["unexpected"] = "ignored-before-audit"
    with pytest.raises(ValueError, match="candidate brief fields"):
        report_from_dict({**record, "candidate_briefs": [candidate]})


def test_strict_knowledge_only_background_passes_without_retrospective(tmp_path):
    report, factory, synthesis_input = knowledge_report(
        tmp_path,
        include_retrospective=False,
        mode="strict_live",
    )
    brief = report.candidate_briefs[0].synthesis
    assert synthesis_input.attribution_evidence == ()
    assert factory.calls == 1
    assert brief.historical_knowledge_background
    assert brief.retrospective_research_context == ()
    assert "Retrospective Research Context" not in render_daily_report(report)


def test_strict_empty_context_and_no_evidence_keeps_zero_provider_fast_path(tmp_path):
    report, factory, synthesis_input = knowledge_report(
        tmp_path,
        historical_content="unrelated text",
        include_retrospective=False,
        mode="strict_live",
    )
    assert synthesis_input.knowledge_context.selected_item_count == 0
    assert factory.calls == 0
    assert report.candidate_briefs[0].synthesis.status == "NO_EVIDENCE_FAST_PATH"
    assert report.candidate_briefs[0].synthesis.supplied_context_id is not None


def test_product_rejects_unknown_or_evidence_namespace_knowledge_refs(tmp_path):
    report, _, _ = knowledge_report(tmp_path)
    synthesis = report.candidate_briefs[0].synthesis
    with pytest.raises(ValueError, match="not supplied"):
        replace(
            synthesis,
            historical_knowledge_background=(
                KnowledgeBackgroundStatement("unknown", ("K999",)),
            ),
        )
    with pytest.raises(ValueError, match="Knowledge reference"):
        replace(synthesis, supplied_knowledge_refs=("E1",))


def test_strict_product_rejects_retrospective_surface(tmp_path):
    report, _, _ = knowledge_report(tmp_path)
    with pytest.raises(ValueError, match="retrospective"):
        replace(
            report,
            mode="strict_live",
            data_quality={
                **report.data_quality,
                "evidence": {
                    **report.data_quality["evidence"],
                    "historical_proxy_used": False,
                },
            },
        )


def test_strict_product_keeps_historical_background_without_retrospective(tmp_path):
    report, factory, _ = knowledge_report(
        tmp_path, include_retrospective=False, mode="strict_live",
    )
    synthesis = report.candidate_briefs[0].synthesis
    assert factory.calls == 1
    assert synthesis.historical_knowledge_background
    assert synthesis.retrospective_research_context == ()
    assert "Retrospective Research Context" not in render_daily_report(report)


def test_storage_rejects_tampered_v2_report_id_collision(tmp_path):
    report, _, _ = knowledge_report(tmp_path / "input")
    repository = DailyReportRepository(
        Settings.from_project_root(tmp_path / "output"),
    )
    repository.write(report)
    synthesis = report.candidate_briefs[0].synthesis
    changed = replace(
        synthesis,
        historical_knowledge_background=(
            KnowledgeBackgroundStatement("tampered background", ("K1",)),
        ),
    )
    tampered = replace(
        report,
        candidate_briefs=(
            replace(report.candidate_briefs[0], synthesis=changed),
        ),
    )
    with pytest.raises(StorageError, match="identity"):
        repository.write(tampered)


def test_storage_rejects_tampered_supplied_context_with_old_report_id(tmp_path):
    report, _, _ = knowledge_report(tmp_path / "input")
    repository = DailyReportRepository(
        Settings.from_project_root(tmp_path / "output"),
    )
    changed = replace(
        report.candidate_briefs[0].synthesis,
        supplied_context_id="f" * 64,
    )
    tampered = replace(
        report,
        candidate_briefs=(
            replace(report.candidate_briefs[0], synthesis=changed),
        ),
    )
    with pytest.raises(StorageError, match="identity"):
        repository.write(tampered)


def test_preopen_reuses_prior_daily_knowledge_without_retrieval_or_synthesis(
    tmp_path, monkeypatch,
):
    daily, factory, _ = knowledge_report(tmp_path / "daily")
    calls = factory.calls
    manifest, manifest_ref, daily_ref = manifest_for_daily(
        tmp_path / "run", daily, run_type=RunType.PRE_OPEN,
    )
    monkeypatch.setattr(
        knowledge_integration,
        "retrieve_knowledge",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("PRE_OPEN repeated Knowledge retrieval")
        ),
    )
    monkeypatch.setattr(
        knowledge_integration,
        "assemble_knowledge_context",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("PRE_OPEN repeated context assembly")
        ),
    )
    report = assemble_time_slice(
        manifest,
        manifest_ref=manifest_ref,
        daily_report=daily,
        daily_report_ref=daily_ref,
        generated_at=PRE_OPEN,
    )
    assert factory.calls == calls
    assert report.schema_version == KNOWLEDGE_TIME_SLICE_SCHEMA_VERSION
    assert report.payload.previous_knowledge_briefs[0].supplied_context_id == (
        daily.candidate_briefs[0].synthesis.supplied_context_id
    )
    assert "Historical Knowledge Background" in render_time_slice(report)
    assert "Retrospective Research Context" in render_time_slice(report)
    restored = time_slice_from_record(json.loads(
        canonical_json_bytes(time_slice_to_record(report)), parse_float=Decimal,
    ))
    assert restored == report


def test_empty_daily_context_still_triggers_preopen_v2_and_blocks_downgrade(tmp_path):
    daily, _, synthesis_input = knowledge_report(
        tmp_path / "daily",
        historical_content="unrelated text",
        include_retrospective=False,
    )
    assert synthesis_input.knowledge_context.selected_item_count == 0
    manifest, manifest_ref, daily_ref = manifest_for_daily(
        tmp_path / "run", daily, run_type=RunType.PRE_OPEN,
    )
    report = assemble_time_slice(
        manifest,
        manifest_ref=manifest_ref,
        daily_report=daily,
        daily_report_ref=daily_ref,
        generated_at=PRE_OPEN,
    )
    assert report.schema_version == KNOWLEDGE_TIME_SLICE_SCHEMA_VERSION
    assert report.payload.previous_knowledge_briefs[0].supplied_knowledge_refs == ()
    with pytest.raises(ValueError, match="legacy time-slice"):
        replace(report, schema_version="time-slice-intelligence-v1")


def test_strict_preopen_rejects_research_prior_with_retrospective_context(tmp_path):
    daily, _, _ = knowledge_report(tmp_path / "daily")
    manifest, _manifest_ref, daily_ref = manifest_for_daily(
        tmp_path / "run", daily, run_type=RunType.PRE_OPEN,
    )
    strict_context = replace(manifest.run_context, mode="strict_live")
    strict_manifest = replace(manifest, run_context=strict_context)
    strict_manifest_ref = ArtifactReference(
        strict_context.run_id, "strict-run.json", manifest.schema_version,
    )
    with pytest.raises(ValueError, match="matching source"):
        assemble_time_slice(
            strict_manifest,
            manifest_ref=strict_manifest_ref,
            daily_report=daily,
            daily_report_ref=daily_ref,
            generated_at=PRE_OPEN,
        )


def test_preopen_rejects_daily_object_that_does_not_match_manifest_artifact(tmp_path):
    daily, _, _ = knowledge_report(tmp_path / "daily")
    manifest, manifest_ref, daily_ref = manifest_for_daily(
        tmp_path / "run", daily, run_type=RunType.PRE_OPEN,
    )
    changed_synthesis = replace(
        daily.candidate_briefs[0].synthesis,
        historical_knowledge_background=(
            KnowledgeBackgroundStatement("tampered background", ("K1",)),
        ),
    )
    tampered = replace(
        daily,
        candidate_briefs=(
            replace(daily.candidate_briefs[0], synthesis=changed_synthesis),
        ),
    )
    with pytest.raises(ValueError, match="artifact reference"):
        assemble_time_slice(
            manifest,
            manifest_ref=manifest_ref,
            daily_report=tampered,
            daily_report_ref=daily_ref,
            generated_at=PRE_OPEN,
        )


def test_postclose_only_references_daily_and_does_not_repeat_knowledge(
    tmp_path, monkeypatch,
):
    daily, factory, _ = knowledge_report(tmp_path / "daily")
    calls = factory.calls
    manifest, manifest_ref, daily_ref = manifest_for_daily(
        tmp_path / "run", daily, run_type=RunType.POST_CLOSE,
    )
    monkeypatch.setattr(
        knowledge_integration,
        "retrieve_knowledge",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("POST_CLOSE repeated Knowledge retrieval")
        ),
    )
    monkeypatch.setattr(
        knowledge_integration,
        "assemble_knowledge_context",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("POST_CLOSE repeated context assembly")
        ),
    )
    report = assemble_time_slice(
        manifest,
        manifest_ref=manifest_ref,
        daily_report=daily,
        daily_report_ref=daily_ref,
        generated_at=NOW + timedelta(hours=1),
    )
    assert factory.calls == calls
    assert report.payload.daily_intelligence_report_ref == daily_ref
    assert not hasattr(report.payload, "previous_knowledge_briefs")


def test_postclose_identity_binds_daily_bytes_but_not_artifact_path(tmp_path):
    daily, _, _ = knowledge_report(tmp_path / "daily")
    manifest, manifest_ref, daily_ref = manifest_for_daily(
        tmp_path / "run", daily, run_type=RunType.POST_CLOSE,
    )
    report = assemble_time_slice(
        manifest,
        manifest_ref=manifest_ref,
        daily_report=daily,
        daily_report_ref=daily_ref,
        generated_at=NOW + timedelta(hours=1),
    )
    moved_payload = replace(
        report.payload,
        daily_intelligence_report_ref=replace(
            daily_ref, path="/deployment-independent/location.json",
        ),
    )
    assert replace(report, payload=moved_payload).report_id == report.report_id


def test_timeslice_loader_rejects_unknown_previous_knowledge_fields(tmp_path):
    daily, _, _ = knowledge_report(tmp_path / "daily")
    manifest, manifest_ref, daily_ref = manifest_for_daily(
        tmp_path / "run", daily, run_type=RunType.PRE_OPEN,
    )
    report = assemble_time_slice(
        manifest,
        manifest_ref=manifest_ref,
        daily_report=daily,
        daily_report_ref=daily_ref,
        generated_at=PRE_OPEN,
    )
    record = json.loads(
        canonical_json_bytes(time_slice_to_record(report)), parse_float=Decimal,
    )
    record["payload"]["previous_knowledge_briefs"][0]["unexpected"] = True
    with pytest.raises(ValueError, match="fields disagree"):
        time_slice_from_record(record)


def test_hard_knowledge_failure_precedes_provider_and_product_publication(tmp_path):
    _market, _anomalies, _candidates, names, attribution, _ = artifacts(count=1)
    cli = _daily_cli()
    settings = KnowledgeIntegrationSettings(
        enabled=True, lexical_index_version="0" * 64,
    )
    with pytest.raises(knowledge_integration.KnowledgePreparationError):
        cli._prepare_synthesis_inputs(
            attribution.bundles[:1],
            attribution.attribution_facts,
            company_names=names,
            mode="research",
            market_event_time=EVENT,
            research_corpus_cutoff=CUTOFF,
            generated_at=CUTOFF,
            knowledge_settings=settings,
            knowledge_repository=knowledge_integration.KnowledgeRepository(
                tmp_path / "missing",
            ),
        )
    assert not (tmp_path / "data" / "derived" / "daily_reports").exists()


def test_multi_candidate_knowledge_failure_prevents_partial_daily_publication(
    tmp_path, monkeypatch,
):
    _market, _anomalies, _candidates, names, attribution, _ = artifacts(count=2)
    _ready_report, ready_factory, ready_input = knowledge_report(tmp_path / "ready")
    provider_calls = ready_factory.calls
    cli = _daily_cli()
    calls = 0

    def prepare(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return SimpleNamespace(synthesis_input=ready_input)
        raise KnowledgePreparationError(
            KnowledgePreparationStatus.CORRUPT,
            "KNOWLEDGE_ARTIFACT_CORRUPT",
        )

    monkeypatch.setattr(cli, "prepare_candidate_synthesis_input", prepare)
    with pytest.raises(KnowledgePreparationError) as raised:
        cli._prepare_synthesis_inputs(
            attribution.bundles[:2],
            attribution.attribution_facts,
            company_names=names,
            mode="research",
            market_event_time=EVENT,
            research_corpus_cutoff=CUTOFF,
            generated_at=CUTOFF,
            knowledge_settings=KnowledgeIntegrationSettings(
                enabled=True, lexical_index_version="0" * 64,
            ),
            knowledge_repository=object(),
            index_repository=object(),
            context_repository=object(),
        )
    assert raised.value.status is KnowledgePreparationStatus.CORRUPT
    assert calls == 2
    assert ready_factory.calls == provider_calls
    assert not (tmp_path / "data" / "derived" / "daily_reports").exists()
