"""E2E.3 local operational Knowledge status and manifest smokes."""

from dataclasses import replace
from datetime import timedelta
import hashlib
import json
from pathlib import Path

import pytest

from quantos.config import Settings
from quantos.orchestration import (
    collect_readiness, execute_run, load_local_operational_artifacts,
    local_artifact_handlers,
)
from quantos.reporting import canonical_json_bytes, report_to_dict
from quantos.schemas.run import (
    ArtifactReference, KNOWLEDGE_RUN_SCHEMA_VERSION,
    KNOWLEDGE_OPERATIONAL_STATE_SCHEMA_VERSION, KnowledgeOperationalState,
    KnowledgeOperationalStatus as K, ModuleExecutionStatus as E,
    RUN_SCHEMA_VERSION, RunContext, RunType,
)
from quantos.storage import DailyReportRepository
from quantos.storage.knowledge import KnowledgeRepository
from quantos.storage.knowledge_context import KnowledgeContextRepository
from quantos.storage.market import StorageError
from quantos.storage.run import RunRepository, manifest_from_record, manifest_record
from tests.test_e2e_product_knowledge import knowledge_report
from tests.test_reporting import build_report


UNIVERSE = "shse_szse_a_share"


def _publish(root, report):
    settings = Settings.from_project_root(root)
    json_path, _ = DailyReportRepository(settings).write(report)
    return settings, ArtifactReference(
        hashlib.sha256(json_path.read_bytes()).hexdigest(),
        str(json_path.resolve()),
        report.schema_version,
    )


def _operate(settings, *, report, reference, run_type, expected_mode=None,
             target_trade_date=None, as_of_time=None):
    mode = expected_mode or report.mode
    target = target_trade_date or (
        report.trade_date + timedelta(days=1)
        if run_type is RunType.PRE_OPEN else report.trade_date
    )
    cutoff = as_of_time or max(report.as_of_time, report.generated_at) + timedelta(days=2)
    artifacts, state = load_local_operational_artifacts(
        settings,
        target_trade_date=target,
        as_of_time=cutoff,
        run_type=run_type,
        mode=mode,
        top_n=report.candidate_summary["requested_top_n"],
        universe_name=report.universe_name,
        daily_report_ref=reference,
        market_basis_trade_date=(
            report.trade_date if run_type is RunType.PRE_OPEN else None
        ),
    )
    readiness = collect_readiness(
        target_trade_date=target,
        as_of_time=cutoff,
        run_type=run_type,
        mode=mode,
        artifacts=artifacts,
        knowledge_state=state,
    )
    context = RunContext(
        target, readiness.market_basis_trade_date, cutoff, run_type, mode,
        report.universe_name, report.candidate_summary["requested_top_n"],
        False, cutoff,
    )
    manifest = execute_run(
        context, readiness, handlers=local_artifact_handlers(readiness),
        clock=lambda: cutoff,
    )
    return readiness, manifest


def test_legacy_daily_maps_to_not_configured_and_manifest_v2_roundtrips(tmp_path):
    report, _, _ = build_report(tmp_path / "source")
    settings, reference = _publish(tmp_path / "run", report)
    readiness, manifest = _operate(
        settings, report=report, reference=reference, run_type=RunType.POST_CLOSE,
    )
    assert readiness.knowledge_state.status is K.NOT_CONFIGURED
    assert manifest.schema_version == KNOWLEDGE_RUN_SCHEMA_VERSION
    assert manifest.knowledge_state == readiness.knowledge_state
    assert manifest.is_success
    repository = RunRepository(settings)
    assert repository.read(repository.write(manifest)) == manifest


def test_empty_and_ready_daily_have_distinct_operational_states(tmp_path):
    empty, _, empty_input = knowledge_report(
        tmp_path / "empty-source", historical_content="unrelated text",
        include_retrospective=False,
    )
    empty_settings, empty_ref = _publish(tmp_path / "empty-run", empty)
    _, empty_manifest = _operate(
        empty_settings, report=empty, reference=empty_ref,
        run_type=RunType.POST_CLOSE,
    )
    ready, _, ready_input = knowledge_report(
        tmp_path / "ready-source", include_retrospective=False,
        emit_background=False,
    )
    ready_settings, ready_ref = _publish(tmp_path / "ready-run", ready)
    _, ready_manifest = _operate(
        ready_settings, report=ready, reference=ready_ref,
        run_type=RunType.POST_CLOSE,
    )
    assert empty_input.knowledge_context.selected_item_count == 0
    assert empty_manifest.knowledge_state.status is K.EMPTY
    assert empty_manifest.knowledge_state.empty_candidate_count == 1
    assert ready_input.knowledge_context.selected_item_count == 1
    assert ready.candidate_briefs[0].synthesis.historical_knowledge_background == ()
    assert ready_manifest.knowledge_state.status is K.READY
    assert ready_manifest.knowledge_state.ready_candidate_count == 1
    assert empty_manifest.is_success and ready_manifest.is_success


def test_mixed_ready_and_empty_candidates_aggregate_to_ready(tmp_path):
    from quantos.reporting import _knowledge_report_identity_value

    ready, _, _ = knowledge_report(
        tmp_path / "ready", include_retrospective=False, emit_background=False,
    )
    empty, _, _ = knowledge_report(
        tmp_path / "empty", historical_content="unrelated text",
        include_retrospective=False,
    )
    second = replace(
        empty.candidate_briefs[0], rank=2, symbol="601331.SH", name="公司2",
    )
    summary = {
        **ready.candidate_summary,
        "total_candidates": 2,
        "selected_for_report": 2,
        "requested_top_n": 2,
    }
    mixed = replace(
        ready, report_id="0" * 64, candidate_summary=summary,
        candidate_briefs=(ready.candidate_briefs[0], second),
    )
    identity = _knowledge_report_identity_value(
        trade_date=mixed.trade_date,
        as_of_time=mixed.as_of_time,
        mode=mixed.mode,
        universe_name=mixed.universe_name,
        market_overview=mixed.market_overview,
        sector_overview=mixed.sector_overview,
        anomaly_overview=mixed.anomaly_overview,
        candidate_summary=mixed.candidate_summary,
        candidate_briefs=mixed.candidate_briefs,
        evidence_overview=mixed.evidence_overview,
        data_quality=mixed.data_quality,
        provenance=mixed.provenance,
    )
    mixed = replace(
        mixed,
        report_id=hashlib.sha256(canonical_json_bytes(identity)).hexdigest(),
    )
    settings, reference = _publish(tmp_path / "run", mixed)
    _, manifest = _operate(
        settings, report=mixed, reference=reference, run_type=RunType.POST_CLOSE,
    )
    assert manifest.knowledge_state.status is K.READY
    assert manifest.knowledge_state.candidate_count == 2
    assert manifest.knowledge_state.ready_candidate_count == 1
    assert manifest.knowledge_state.empty_candidate_count == 1


def test_missing_product_is_unavailable_not_not_configured(tmp_path):
    report, _, _ = build_report(tmp_path / "source")
    settings = Settings.from_project_root(tmp_path / "run")
    missing = ArtifactReference("0" * 64, str(tmp_path / "missing.json"), report.schema_version)
    _, manifest = _operate(
        settings, report=report, reference=missing, run_type=RunType.POST_CLOSE,
    )
    assert manifest.knowledge_state.status is K.UNAVAILABLE
    assert manifest.knowledge_state.reason_code == "PRODUCT_UNAVAILABLE"
    assert not manifest.is_success
    assert manifest_from_record(manifest_record(manifest)) == manifest


@pytest.mark.parametrize("kind", ["hash", "identity"])
def test_corrupt_product_hash_or_logical_identity_fails_closed(tmp_path, kind):
    report, _, _ = knowledge_report(tmp_path / "source", include_retrospective=False)
    settings, reference = _publish(tmp_path / "run", report)
    path = reference.path
    if kind == "hash":
        with open(path, "ab") as stream:
            stream.write(b" ")
    else:
        record = report_to_dict(report)
        limitations = record["candidate_briefs"][0]["synthesis"]["limitations"]
        record["candidate_briefs"][0]["synthesis"]["limitations"] = (
            *limitations, "tampered",
        )
        raw = canonical_json_bytes(record) + b"\n"
        with open(path, "wb") as stream:
            stream.write(raw)
        reference = replace(reference, artifact_id=hashlib.sha256(raw).hexdigest())
    _, manifest = _operate(
        settings, report=report, reference=reference, run_type=RunType.POST_CLOSE,
    )
    assert manifest.knowledge_state.status is K.CORRUPT
    assert not manifest.is_success
    assert manifest_from_record(manifest_record(manifest)) == manifest


def test_mode_and_date_mismatch_fail_closed(tmp_path):
    report, _, _ = knowledge_report(tmp_path / "source", include_retrospective=False)
    settings, reference = _publish(tmp_path / "run", report)
    _, mode_manifest = _operate(
        settings, report=report, reference=reference, run_type=RunType.POST_CLOSE,
        expected_mode="strict_live",
    )
    _, date_manifest = _operate(
        settings, report=report, reference=reference, run_type=RunType.POST_CLOSE,
        target_trade_date=report.trade_date + timedelta(days=1),
    )
    assert mode_manifest.knowledge_state.status is K.FAILED_INTEGRITY
    assert mode_manifest.knowledge_state.reason_code == "PRODUCT_MODE_MISMATCH"
    assert date_manifest.knowledge_state.status is K.STALE
    assert date_manifest.knowledge_state.reason_code == "PRODUCT_DATE_MISMATCH"
    assert not mode_manifest.is_success and not date_manifest.is_success
    assert manifest_from_record(manifest_record(mode_manifest)) == mode_manifest
    assert manifest_from_record(manifest_record(date_manifest)) == date_manifest


def test_explicit_pre_open_reference_must_match_expected_basis(tmp_path):
    report, _, _ = knowledge_report(tmp_path / "source", include_retrospective=False)
    settings, reference = _publish(tmp_path / "run", report)
    artifacts, state = load_local_operational_artifacts(
        settings,
        target_trade_date=report.trade_date + timedelta(days=2),
        as_of_time=max(report.as_of_time, report.generated_at) + timedelta(days=2),
        run_type=RunType.PRE_OPEN,
        mode=report.mode,
        top_n=1,
        universe_name=report.universe_name,
        daily_report_ref=reference,
        market_basis_trade_date=report.trade_date + timedelta(days=1),
    )
    assert artifacts == ()
    assert state.status is K.STALE
    assert state.reason_code == "PRODUCT_DATE_MISMATCH"


def test_schema_reference_and_pit_cutoff_mismatch_fail_closed(tmp_path):
    report, _, _ = knowledge_report(tmp_path / "source", include_retrospective=False)
    settings, reference = _publish(tmp_path / "run", report)
    _, schema_manifest = _operate(
        settings, report=report,
        reference=replace(reference, version="daily-intelligence-v1"),
        run_type=RunType.POST_CLOSE,
    )
    _, pit_manifest = _operate(
        settings, report=report, reference=reference, run_type=RunType.POST_CLOSE,
        as_of_time=max(report.as_of_time, report.generated_at) - timedelta(seconds=1),
    )
    assert schema_manifest.knowledge_state.status is K.CORRUPT
    assert schema_manifest.knowledge_state.reason_code == "PRODUCT_SCHEMA_MISMATCH"
    assert pit_manifest.knowledge_state.status is K.STALE
    assert pit_manifest.knowledge_state.reason_code == "PRODUCT_NOT_PIT_VISIBLE"
    assert not schema_manifest.is_success and not pit_manifest.is_success


@pytest.mark.parametrize("run_type", [RunType.PRE_OPEN, RunType.POST_CLOSE])
def test_pre_open_and_post_close_reuse_exact_product_without_knowledge_execution(
    tmp_path, monkeypatch, run_type,
):
    report, factory, _ = knowledge_report(
        tmp_path / "source", include_retrospective=False,
    )
    calls_before = factory.calls
    settings, reference = _publish(tmp_path / "run", report)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("operational product reuse attempted Knowledge execution")

    monkeypatch.setattr("quantos.knowledge_integration.retrieve_knowledge", forbidden)
    monkeypatch.setattr("quantos.knowledge_integration.assemble_knowledge_context", forbidden)
    _, manifest = _operate(
        settings, report=report, reference=reference, run_type=run_type,
    )
    assert manifest.is_success
    assert manifest.knowledge_state.status is K.READY
    assert manifest.knowledge_state.product_ref == reference
    assert factory.calls == calls_before
    assert manifest.summary.real_llm_requests == 0


def test_ready_manifest_provenance_traverses_daily_context_and_document(tmp_path):
    report, _, synthesis_input = knowledge_report(
        tmp_path / "source", include_retrospective=False,
    )
    settings, reference = _publish(tmp_path / "run", report)
    _, manifest = _operate(
        settings, report=report, reference=reference, run_type=RunType.POST_CLOSE,
    )
    stored_daily = DailyReportRepository(settings).read_json(
        Path(manifest.knowledge_state.product_ref.path),
    )
    context_id = stored_daily.candidate_briefs[0].synthesis.supplied_context_id
    assert context_id == synthesis_input.knowledge_context.context_id
    context = KnowledgeContextRepository(tmp_path / "source" / "knowledge" / "contexts").load(
        context_id,
    )
    document_id = context.historical_items[0].document_id
    document = KnowledgeRepository(
        tmp_path / "source" / "knowledge" / "knowledge",
    ).get_document(document_id)
    assert document.document_id == document_id
    assert document.provenance.source_identifier
    manifest_text = json.dumps(manifest_record(manifest))
    assert context_id not in manifest_text
    assert "historical_knowledge_background" not in manifest_text


def test_manifest_state_identity_excludes_absolute_path_and_detects_tampering(tmp_path):
    report, _, _ = knowledge_report(tmp_path / "source", include_retrospective=False)
    settings, reference = _publish(tmp_path / "run", report)
    _, manifest = _operate(
        settings, report=report, reference=reference, run_type=RunType.POST_CLOSE,
    )
    state = manifest.knowledge_state
    assert state.schema_version == KNOWLEDGE_OPERATIONAL_STATE_SCHEMA_VERSION
    relocated = replace(state, product_ref=replace(reference, path="/deployment/other/report.json"))
    assert state.state_id == relocated.state_id
    record = manifest_record(manifest)
    record["knowledge_state"]["status"] = K.EMPTY.value
    with pytest.raises(ValueError):
        manifest_from_record(record)
    record = manifest_record(manifest)
    record["knowledge_state"]["unexpected"] = "unsafe"
    with pytest.raises(ValueError):
        manifest_from_record(record)


@pytest.mark.parametrize(
    "field,value",
    [
        ("reason_code", "INTERNAL_FAILURE"),
        ("source_report_id", "0" * 64),
        ("ready_candidate_count", 0),
        ("artifact_id", "f" * 64),
        ("schema_version", "knowledge-operational-state-v999"),
    ],
)
def test_manifest_rejects_tampered_state_identity_fields(tmp_path, field, value):
    report, _, _ = knowledge_report(tmp_path / "source", include_retrospective=False)
    settings, reference = _publish(tmp_path / "run", report)
    _, manifest = _operate(
        settings, report=report, reference=reference, run_type=RunType.POST_CLOSE,
    )
    record = manifest_record(manifest)
    if field == "artifact_id":
        record["knowledge_state"]["product_ref"][field] = value
    else:
        record["knowledge_state"][field] = value
    with pytest.raises(ValueError):
        manifest_from_record(record)


def test_manifest_v1_v2_boundary_and_hard_state_success_contract(tmp_path):
    report, _, _ = knowledge_report(tmp_path / "source", include_retrospective=False)
    settings, reference = _publish(tmp_path / "run", report)
    _, successful = _operate(
        settings, report=report, reference=reference, run_type=RunType.POST_CLOSE,
    )
    downgraded = manifest_record(successful)
    downgraded["schema_version"] = RUN_SCHEMA_VERSION
    with pytest.raises(ValueError):
        manifest_from_record(downgraded)

    from tests.test_run_storage import manifest as legacy_manifest

    upgraded = manifest_record(legacy_manifest())
    upgraded["schema_version"] = KNOWLEDGE_RUN_SCHEMA_VERSION
    with pytest.raises(ValueError):
        manifest_from_record(upgraded)

    hard = replace(
        successful,
        knowledge_state=KnowledgeOperationalState(
            K.CORRUPT, "PRODUCT_CORRUPT", reference, None, 0, 0, 0,
        ),
    )
    assert not hard.is_success
    with pytest.raises(ValueError, match="contradicts"):
        manifest_from_record(manifest_record(hard))


@pytest.mark.parametrize(
    "status,reason",
    [
        (K.UNAVAILABLE, "PRODUCT_UNAVAILABLE"),
        (K.STALE, "PRODUCT_DATE_MISMATCH"),
        (K.CORRUPT, "PRODUCT_CORRUPT"),
        (K.FAILED_INTEGRITY, "PRODUCT_MODE_MISMATCH"),
    ],
)
def test_failure_manifest_roundtrip_preserves_observable_reason(
    tmp_path, status, reason,
):
    report, _, _ = knowledge_report(tmp_path / "source", include_retrospective=False)
    settings, reference = _publish(tmp_path / "run", report)
    _, successful = _operate(
        settings, report=report, reference=reference, run_type=RunType.POST_CLOSE,
    )
    failed = replace(
        successful,
        execution_results=(
            *successful.execution_results[:-1],
            replace(
                successful.execution_results[-1], status=E.FAIL,
                reason_code="MODULE_EXECUTION_FAILED", artifact_refs=(),
                safe_error_code="MODULE_EXECUTION_FAILED",
            ),
        ),
        summary=replace(
            successful.summary,
            passed_modules=successful.summary.passed_modules - 1,
            failed_modules=successful.summary.failed_modules + 1,
            report_id=None,
            report_reused=False,
            report_generated=False,
        ),
        knowledge_state=KnowledgeOperationalState(
            status, reason, reference, None, 0, 0, 0,
        ),
    )
    repository = RunRepository(settings)
    loaded = repository.read(repository.write(failed))
    assert not loaded.is_success
    assert loaded.knowledge_state.status is status
    assert loaded.knowledge_state.reason_code == reason


def test_manifest_rejects_product_ref_not_validated_by_required_module(tmp_path):
    report, _, _ = knowledge_report(tmp_path / "source", include_retrospective=False)
    settings, reference = _publish(tmp_path / "run", report)
    _, manifest = _operate(
        settings, report=report, reference=reference, run_type=RunType.POST_CLOSE,
    )
    inconsistent = replace(
        manifest,
        knowledge_state=replace(
            manifest.knowledge_state,
            product_ref=replace(reference, artifact_id="f" * 64),
        ),
    )
    with pytest.raises(ValueError, match="not validated"):
        manifest_from_record(manifest_record(inconsistent))


def test_intraday_manifest_cannot_claim_successful_knowledge_state(tmp_path):
    report, _, _ = knowledge_report(tmp_path / "source", include_retrospective=False)
    settings, reference = _publish(tmp_path / "run", report)
    _, manifest = _operate(
        settings, report=report, reference=reference, run_type=RunType.POST_CLOSE,
    )
    with pytest.raises(ValueError, match="intraday Knowledge"):
        replace(
            manifest,
            run_context=replace(
                manifest.run_context,
                run_type=RunType.INTRADAY,
                market_basis_trade_date=report.trade_date - timedelta(days=1),
            ),
        )


def test_manifest_repository_rejects_tampered_operational_state(tmp_path):
    report, _, _ = knowledge_report(tmp_path / "source", include_retrospective=False)
    settings, reference = _publish(tmp_path / "run", report)
    _, manifest = _operate(
        settings, report=report, reference=reference, run_type=RunType.POST_CLOSE,
    )
    repository = RunRepository(settings)
    path = repository.write(manifest)
    record = json.loads(path.read_text())
    record["knowledge_state"]["source_report_id"] = "0" * 64
    path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(StorageError):
        repository.read(path)


def test_discovery_refuses_ambiguous_same_basis_products(tmp_path):
    report, _, _ = knowledge_report(tmp_path / "source", include_retrospective=False)
    settings = Settings.from_project_root(tmp_path / "run")
    repository = DailyReportRepository(settings)
    repository.write(report)
    repository.write(report)
    artifacts, state = load_local_operational_artifacts(
        settings,
        target_trade_date=report.trade_date,
        as_of_time=max(report.as_of_time, report.generated_at) + timedelta(days=2),
        run_type=RunType.POST_CLOSE,
        mode=report.mode,
        top_n=1,
        universe_name=UNIVERSE,
    )
    assert artifacts == ()
    assert state.status is K.FAILED_INTEGRITY
    assert state.reason_code == "PRODUCT_SELECTION_AMBIGUOUS"


def test_explicit_reference_wins_over_ambiguous_product_generations(tmp_path):
    report, _, _ = knowledge_report(tmp_path / "source", include_retrospective=False)
    settings = Settings.from_project_root(tmp_path / "run")
    repository = DailyReportRepository(settings)
    first, _ = repository.write(report)
    repository.write(report)
    reference = ArtifactReference(
        hashlib.sha256(first.read_bytes()).hexdigest(),
        str(first.resolve()),
        report.schema_version,
    )
    artifacts, state = load_local_operational_artifacts(
        settings,
        target_trade_date=report.trade_date + timedelta(days=1),
        as_of_time=max(report.as_of_time, report.generated_at) + timedelta(days=2),
        run_type=RunType.PRE_OPEN,
        mode=report.mode,
        top_n=1,
        universe_name=report.universe_name,
        daily_report_ref=reference,
        market_basis_trade_date=report.trade_date,
    )
    assert artifacts
    assert state.status is K.READY
    assert state.product_ref == reference


def test_universe_and_top_n_mismatch_are_integrity_failures(tmp_path):
    report, _, _ = knowledge_report(tmp_path / "source", include_retrospective=False)
    settings, reference = _publish(tmp_path / "run", report)
    common = {
        "target_trade_date": report.trade_date,
        "as_of_time": max(report.as_of_time, report.generated_at) + timedelta(days=2),
        "run_type": RunType.POST_CLOSE,
        "mode": report.mode,
        "daily_report_ref": reference,
    }
    _, universe = load_local_operational_artifacts(
        settings, top_n=1, universe_name="different", **common,
    )
    _, top_n = load_local_operational_artifacts(
        settings, top_n=2, universe_name=report.universe_name, **common,
    )
    assert universe.status is K.FAILED_INTEGRITY
    assert universe.reason_code == "PRODUCT_UNIVERSE_MISMATCH"
    assert top_n.status is K.FAILED_INTEGRITY
    assert top_n.reason_code == "PRODUCT_TOP_N_MISMATCH"


def test_unexpected_product_read_error_is_failed_without_error_text(
    tmp_path, monkeypatch,
):
    report, _, _ = knowledge_report(tmp_path / "source", include_retrospective=False)
    settings, reference = _publish(tmp_path / "run", report)
    original = Path.read_bytes

    def fail_selected(path):
        if path.resolve() == Path(reference.path).resolve():
            raise PermissionError("secret absolute deployment detail")
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", fail_selected)
    _, manifest = _operate(
        settings, report=report, reference=reference,
        run_type=RunType.POST_CLOSE,
    )
    state = manifest.knowledge_state
    assert state.status is K.FAILED
    assert state.reason_code == "INTERNAL_FAILURE"
    assert not manifest.is_success
    assert "secret" not in json.dumps(manifest_record(manifest))


def test_artifact_change_after_preflight_cannot_produce_success(tmp_path):
    report, _, _ = knowledge_report(tmp_path / "source", include_retrospective=False)
    settings, reference = _publish(tmp_path / "run", report)
    cutoff = max(report.as_of_time, report.generated_at) + timedelta(days=2)
    artifacts, state = load_local_operational_artifacts(
        settings,
        target_trade_date=report.trade_date,
        as_of_time=cutoff,
        run_type=RunType.POST_CLOSE,
        mode=report.mode,
        top_n=1,
        universe_name=report.universe_name,
        daily_report_ref=reference,
    )
    with Path(reference.path).open("ab") as stream:
        stream.write(b" ")
    readiness = collect_readiness(
        target_trade_date=report.trade_date,
        as_of_time=cutoff,
        run_type=RunType.POST_CLOSE,
        mode=report.mode,
        artifacts=artifacts,
        knowledge_state=state,
    )
    context = RunContext(
        report.trade_date, readiness.market_basis_trade_date, cutoff,
        RunType.POST_CLOSE, report.mode, report.universe_name, 1, False, cutoff,
    )
    manifest = execute_run(
        context, readiness, handlers=local_artifact_handlers(readiness),
        clock=lambda: cutoff,
    )
    assert not manifest.is_success
    assert manifest.knowledge_state.status is K.FAILED
    assert manifest.knowledge_state.reason_code == "PRODUCT_VALIDATION_FAILED"


def test_tampered_markdown_pair_is_corrupt(tmp_path):
    report, _, _ = knowledge_report(tmp_path / "source", include_retrospective=False)
    settings, reference = _publish(tmp_path / "run", report)
    Path(reference.path).with_suffix(".md").write_text("tampered", encoding="utf-8")
    _, state = load_local_operational_artifacts(
        settings,
        target_trade_date=report.trade_date,
        as_of_time=max(report.as_of_time, report.generated_at) + timedelta(days=2),
        run_type=RunType.POST_CLOSE,
        mode=report.mode,
        top_n=1,
        universe_name=report.universe_name,
        daily_report_ref=reference,
    )
    assert state.status is K.CORRUPT
    assert state.reason_code == "PRODUCT_CORRUPT"


def test_operational_state_status_combinations_are_closed():
    reference = ArtifactReference("1" * 64, "/not-an-identity-input.json", "v")
    successful = {
        K.NOT_CONFIGURED: ("KNOWLEDGE_NOT_CONFIGURED", 1, 0, 0),
        K.EMPTY: ("KNOWLEDGE_EMPTY", 1, 0, 1),
        K.READY: ("KNOWLEDGE_READY", 1, 1, 0),
    }
    for status, (reason, total, ready, empty) in successful.items():
        assert KnowledgeOperationalState(
            status, reason, reference, "2" * 64, total, ready, empty,
        ).is_hard_failure is False
    for status, reason in (
        (K.UNAVAILABLE, "PRODUCT_UNAVAILABLE"),
        (K.STALE, "PRODUCT_DATE_MISMATCH"),
        (K.CORRUPT, "PRODUCT_CORRUPT"),
        (K.FAILED, "INTERNAL_FAILURE"),
        (K.FAILED_INTEGRITY, "PRODUCT_MODE_MISMATCH"),
    ):
        assert KnowledgeOperationalState(
            status, reason, None, None, 0, 0, 0,
        ).is_hard_failure
    with pytest.raises(ValueError):
        KnowledgeOperationalState(K.EMPTY, "OTHER", reference, "2" * 64, 1, 0, 1)


def test_legacy_manifest_v1_remains_without_knowledge_extension():
    from tests.test_run_storage import manifest

    value = manifest()
    record = manifest_record(value)
    assert "knowledge_state" not in record
    assert manifest_from_record(record) == value
