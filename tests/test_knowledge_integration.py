"""E2E.1 deterministic Knowledge preparation and synthesis-input wiring."""

import json
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

import quantos.knowledge_integration as integration_module
from quantos.config import (
    KNOWLEDGE_QUERY_STRATEGY_VERSION,
    MARKET_TIMEZONE,
    KnowledgeIntegrationSettings,
)
from quantos.knowledge_retrieval import build_knowledge_lexical_index
from quantos.schemas import (
    AnomalyCandidate,
    AttributionEvidenceBundle,
    KnowledgeDocumentInput,
    KnowledgeProvenance,
    KnowledgeQueryMode,
    KnowledgeSourceType,
    KnowledgeTemporalClass,
    canonicalize_knowledge_document,
)
from quantos.storage.knowledge import KnowledgeRepository
from quantos.storage.knowledge_context import KnowledgeContextRepository
from quantos.storage.knowledge_index import KnowledgeLexicalIndexRepository
from quantos.synthesis import FakeLLMClient, synthesize_evidence
from quantos.knowledge_integration import (
    KnowledgePreparationError,
    KnowledgePreparationStatus,
    candidate_subject_query,
    prepare_candidate_synthesis_input,
)


EVENT = datetime(2026, 8, 31, 15, tzinfo=MARKET_TIMEZONE)
CUTOFF = datetime(2026, 9, 2, 12, tzinfo=MARKET_TIMEZONE)


def candidate(
    symbol="601330.SH", company_sector="环保", *, rank=1, with_sector=True,
):
    sector = ("C42", company_sector, "证监会行业分类") if with_sector else (None, None, None)
    return AnomalyCandidate(
        date(2026, 8, 31), CUTOFF, symbol, *sector,
        Decimal("10.25"), Decimal("4.2"), Decimal("3"), Decimal("2"),
        True, True, True, 3, "price_volume_amount",
        Decimal("1"), Decimal("0.5"), Decimal("9.25"),
        "stock_specific", 1, rank, 20, CUTOFF,
    )


def bundle(value=None):
    return AttributionEvidenceBundle(value or candidate(), None, (), (), ())


def document(
    family, content, *, symbol="601330.SH", available_at=EVENT,
):
    return canonicalize_knowledge_document(KnowledgeDocumentInput(
        document_family_id=family,
        document_version=1,
        source="official-source",
        source_type=KnowledgeSourceType.REFERENCE_DOCUMENT,
        title=f"Title {family}",
        content=content,
        temporal_class=KnowledgeTemporalClass.TIMELESS,
        published_at=available_at - timedelta(days=1),
        available_at=available_at,
        retrieved_at=available_at,
        effective_from=None,
        effective_to=None,
        entity_refs=(symbol,),
        provenance=KnowledgeProvenance(
            source_identifier=f"artifact:{family}",
            ingestion_adapter="fixture",
            ingestion_adapter_version="v1",
            origin_reference=f"https://example.invalid/{family}",
        ),
    ))


def deployment(tmp_path: Path, documents):
    knowledge = KnowledgeRepository(tmp_path / "knowledge")
    knowledge.documents_root.mkdir(parents=True, exist_ok=True)
    for item in documents:
        knowledge.store(item)
    index = build_knowledge_lexical_index(knowledge, built_at=CUTOFF)
    indexes = KnowledgeLexicalIndexRepository(tmp_path / "indexes")
    indexes.store(index)
    contexts = KnowledgeContextRepository(tmp_path / "contexts")
    settings = KnowledgeIntegrationSettings(
        enabled=True,
        lexical_index_version=index.lexical_index_version,
        retrieval_limit=10,
        context_max_items=10,
        context_max_chars=10_000,
    )
    return knowledge, indexes, contexts, settings, index


def prepare(
    tmp_path, documents, *, selected_bundle=None, mode="strict_live",
    company_name="绿色动力", cutoff=None, generated_at=CUTOFF,
):
    knowledge, indexes, contexts, settings, index = deployment(tmp_path, documents)
    result = prepare_candidate_synthesis_input(
        selected_bundle or bundle(),
        (),
        mode=mode,
        company_name=company_name,
        market_event_time=EVENT,
        research_corpus_cutoff=cutoff,
        generated_at=generated_at,
        settings=settings,
        knowledge_repository=knowledge,
        index_repository=indexes,
        context_repository=contexts,
    )
    return result, knowledge, indexes, contexts, settings, index


def test_disabled_is_legacy_not_configured_and_does_not_touch_storage():
    class ExplodingRepository:
        @property
        def documents_root(self):
            raise AssertionError("disabled Knowledge touched storage")

    result = prepare_candidate_synthesis_input(
        bundle(), (), mode="strict_live", company_name="绿色动力",
        market_event_time=EVENT, research_corpus_cutoff=None,
        generated_at=CUTOFF, settings=KnowledgeIntegrationSettings(),
        knowledge_repository=ExplodingRepository(),
        index_repository=ExplodingRepository(),
        context_repository=ExplodingRepository(),
    )
    assert result.status is KnowledgePreparationStatus.NOT_CONFIGURED
    assert result.knowledge_context is None
    assert result.synthesis_input.knowledge_context is None
    assert result.synthesis_input.prompt_version == "quantos-synthesis-v1"


def test_candidate_subject_v1_has_frozen_fields_and_order():
    assert candidate_subject_query(
        company_name="绿色动力", sector_name="环保", symbol="601330.SH",
    ) == "绿色动力 环保 601330.SH"
    assert candidate_subject_query(
        company_name="绿色动力", sector_name=None, symbol="601330.SH",
    ) == "绿色动力 601330.SH"
    assert candidate_subject_query(
        company_name=" 绿色动力 ", sector_name="  环保\t行业 ", symbol="601330.SH",
    ) == "绿色动力 环保 行业 601330.SH"
    assert KNOWLEDGE_QUERY_STRATEGY_VERSION == "candidate_subject:v1"


def test_ready_strict_prepares_and_wires_one_candidate(tmp_path):
    result, *_ = prepare(tmp_path, (
        document("company:601330", "绿色动力 环保 601330 公司背景"),
    ))
    assert result.status is KnowledgePreparationStatus.READY
    assert result.query == "绿色动力 环保 601330.SH"
    assert result.retrieval_request.entity_refs == ("601330.SH",)
    assert result.retrieval_request.effective_at == EVENT
    assert result.retrieval_request.as_of_time == EVENT
    assert result.retrieval_request.corpus_cutoff is None
    assert result.knowledge_context.as_of_time == EVENT
    assert result.knowledge_context.retrospective_items == ()
    assert result.synthesis_input.knowledge_context is result.knowledge_context
    assert result.context_path.is_file()


def test_ready_context_reaches_guarded_fake_synthesis(tmp_path):
    result, *_ = prepare(tmp_path, (
        document("company:601330", "绿色动力 环保 601330 公司背景"),
    ))
    client = FakeLLMClient({
        "symbol": "601330.SH", "mode": "strict_live", "evidence_summary": "",
        "possible_explanations": [], "contradicting_signals": [],
        "post_event_notes": [], "insufficient_evidence": True,
        "limitations": ["仅提供背景知识。"], "used_evidence_ids": [],
        "knowledge_background": [{"statement": "公司从事环保业务。",
                                  "knowledge_refs": ["K1"]}],
        "retrospective_knowledge_context": [],
    })
    output = synthesize_evidence(result.synthesis_input, client, clock=lambda: CUTOFF)
    assert len(client.calls) == 1
    assert output.supplied_context_id == result.context_id
    assert output.supplied_knowledge_refs == ("K1",)


def test_query_and_context_identity_exclude_generated_at(tmp_path):
    docs = (document("company:601330", "绿色动力 环保 601330 公司背景"),)
    knowledge, indexes, contexts, settings, _ = deployment(tmp_path, docs)
    first = prepare_candidate_synthesis_input(
        bundle(), (), mode="strict_live", company_name="绿色动力",
        market_event_time=EVENT, research_corpus_cutoff=None, generated_at=CUTOFF,
        settings=settings, knowledge_repository=knowledge,
        index_repository=indexes, context_repository=contexts,
    )
    second = prepare_candidate_synthesis_input(
        bundle(), (), mode="strict_live", company_name="绿色动力",
        market_event_time=EVENT, research_corpus_cutoff=None,
        generated_at=CUTOFF + timedelta(hours=1), settings=settings,
        knowledge_repository=knowledge, index_repository=indexes,
        context_repository=contexts,
    )
    assert first.retrieval_request == second.retrieval_request
    assert first.retrieval_request_id == second.retrieval_request_id
    assert first.context_id == second.context_id
    assert first.context_path == second.context_path
    assert first.synthesis_input.input_bundle_id == second.synthesis_input.input_bundle_id


def test_zero_hits_produces_valid_empty_not_none(tmp_path):
    result, *_ = prepare(tmp_path, (
        document("company:601330", "completely unrelated material"),
    ))
    assert result.status is KnowledgePreparationStatus.EMPTY
    assert result.knowledge_context is not None
    assert result.knowledge_context.retrieved_hit_count == 0
    assert result.knowledge_context.selected_item_count == 0
    assert result.synthesis_input.knowledge_context is result.knowledge_context


def test_existing_empty_corpus_is_valid_empty_not_missing_repository(tmp_path):
    result, *_ = prepare(tmp_path, ())
    assert result.status is KnowledgePreparationStatus.EMPTY
    assert result.knowledge_context.retrieved_hit_count == 0
    assert result.context_path.is_file()


def test_research_uses_event_as_historical_cutoff_and_explicit_corpus_cutoff(tmp_path):
    result, *_ = prepare(tmp_path, (
        document("historical", "绿色动力 环保 601330 历史背景"),
        document("retrospective", "绿色动力 环保 601330 事后研究",
                 available_at=EVENT + timedelta(days=1)),
    ), mode="research", cutoff=CUTOFF)
    assert result.retrieval_request.mode is KnowledgeQueryMode.RESEARCH
    assert result.retrieval_request.as_of_time == EVENT
    assert result.retrieval_request.corpus_cutoff == CUTOFF
    assert result.knowledge_context.as_of_time == EVENT
    assert result.knowledge_context.corpus_cutoff == CUTOFF
    assert result.knowledge_context.historical_item_count == 1
    assert result.knowledge_context.retrospective_item_count == 1


@pytest.mark.parametrize("cutoff,code", [
    (None, "RESEARCH_KNOWLEDGE_CUTOFF_REQUIRED"),
    (EVENT - timedelta(seconds=1), "RESEARCH_KNOWLEDGE_CUTOFF_BEFORE_EVENT"),
])
def test_research_missing_or_early_cutoff_fails_closed(cutoff, code):
    with pytest.raises(KnowledgePreparationError) as caught:
        prepare_candidate_synthesis_input(
            bundle(), (), mode="research", company_name="绿色动力",
            market_event_time=EVENT, research_corpus_cutoff=cutoff,
            generated_at=CUTOFF,
            settings=KnowledgeIntegrationSettings(enabled=True, lexical_index_version="0" * 64),
        )
    assert caught.value.status is KnowledgePreparationStatus.FAILED_INTEGRITY
    assert caught.value.safe_reason_code == code


def test_strict_rejects_research_cutoff_before_storage_access():
    with pytest.raises(KnowledgePreparationError) as caught:
        prepare_candidate_synthesis_input(
            bundle(), (), mode="strict_live", company_name="绿色动力",
            market_event_time=EVENT, research_corpus_cutoff=CUTOFF,
            generated_at=CUTOFF,
            settings=KnowledgeIntegrationSettings(enabled=True, lexical_index_version="0" * 64),
        )
    assert caught.value.status is KnowledgePreparationStatus.FAILED_INTEGRITY


def test_naive_event_time_is_typed_integrity_failure_before_storage():
    class ExplodingRepository:
        @property
        def documents_root(self):
            raise AssertionError("invalid time touched Knowledge storage")

    with pytest.raises(KnowledgePreparationError) as caught:
        prepare_candidate_synthesis_input(
            bundle(), (), mode="strict_live", company_name="绿色动力",
            market_event_time=EVENT.replace(tzinfo=None), research_corpus_cutoff=None,
            generated_at=CUTOFF,
            settings=KnowledgeIntegrationSettings(
                enabled=True, lexical_index_version="0" * 64,
            ),
            knowledge_repository=ExplodingRepository(),
            index_repository=ExplodingRepository(),
            context_repository=ExplodingRepository(),
        )
    assert caught.value.status is KnowledgePreparationStatus.FAILED_INTEGRITY
    assert caught.value.safe_reason_code == "KNOWLEDGE_TIME_INVALID"


def test_context_is_isolated_per_candidate(tmp_path):
    docs = (
        document("company:601330", "绿色动力 环保 601330 公司背景"),
        document("company:600519", "贵州茅台 白酒 600519 公司背景", symbol="600519.SH"),
    )
    knowledge, indexes, contexts, settings, _ = deployment(tmp_path, docs)
    first = prepare_candidate_synthesis_input(
        bundle(), (), mode="strict_live", company_name="绿色动力",
        market_event_time=EVENT, research_corpus_cutoff=None, generated_at=CUTOFF,
        settings=settings, knowledge_repository=knowledge,
        index_repository=indexes, context_repository=contexts,
    )
    second_candidate = candidate("600519.SH", "白酒", rank=2)
    second = prepare_candidate_synthesis_input(
        bundle(second_candidate), (), mode="strict_live", company_name="贵州茅台",
        market_event_time=EVENT, research_corpus_cutoff=None, generated_at=CUTOFF,
        settings=settings, knowledge_repository=knowledge,
        index_repository=indexes, context_repository=contexts,
    )
    assert first.retrieval_request.entity_refs == ("601330.SH",)
    assert second.retrieval_request.entity_refs == ("600519.SH",)
    assert first.context_id != second.context_id


def test_enabled_missing_repository_is_unavailable_and_provider_is_not_called(tmp_path):
    client = FakeLLMClient({})
    with pytest.raises(KnowledgePreparationError) as caught:
        prepare_candidate_synthesis_input(
            bundle(), (), mode="strict_live", company_name="绿色动力",
            market_event_time=EVENT, research_corpus_cutoff=None, generated_at=CUTOFF,
            settings=KnowledgeIntegrationSettings(enabled=True, lexical_index_version="0" * 64),
            knowledge_repository=KnowledgeRepository(tmp_path / "missing"),
            index_repository=KnowledgeLexicalIndexRepository(tmp_path / "indexes"),
            context_repository=KnowledgeContextRepository(tmp_path / "contexts"),
        )
    assert caught.value.status is KnowledgePreparationStatus.UNAVAILABLE
    assert caught.value.safe_reason_code == "KNOWLEDGE_REPOSITORY_UNAVAILABLE"
    assert client.calls == []


def test_enabled_missing_index_is_unavailable_and_never_rebuilt(tmp_path, monkeypatch):
    knowledge = KnowledgeRepository(tmp_path / "knowledge")
    knowledge.documents_root.mkdir(parents=True)
    monkeypatch.setattr(integration_module, "build_knowledge_lexical_index", None, raising=False)
    with pytest.raises(KnowledgePreparationError) as caught:
        prepare_candidate_synthesis_input(
            bundle(), (), mode="strict_live", company_name="绿色动力",
            market_event_time=EVENT, research_corpus_cutoff=None, generated_at=CUTOFF,
            settings=KnowledgeIntegrationSettings(enabled=True, lexical_index_version="0" * 64),
            knowledge_repository=knowledge,
            index_repository=KnowledgeLexicalIndexRepository(tmp_path / "indexes"),
            context_repository=KnowledgeContextRepository(tmp_path / "contexts"),
        )
    assert caught.value.status is KnowledgePreparationStatus.UNAVAILABLE
    assert caught.value.safe_reason_code == "KNOWLEDGE_INDEX_UNAVAILABLE"


def test_real_stale_index_fails_closed(tmp_path):
    docs = (document("first", "绿色动力 环保 601330 公司背景"),)
    knowledge, indexes, contexts, settings, _ = deployment(tmp_path, docs)
    knowledge.store(document("second", "绿色动力 环保 601330 新版本背景"))
    with pytest.raises(KnowledgePreparationError) as caught:
        prepare_candidate_synthesis_input(
            bundle(), (), mode="strict_live", company_name="绿色动力",
            market_event_time=EVENT, research_corpus_cutoff=None, generated_at=CUTOFF,
            settings=settings, knowledge_repository=knowledge,
            index_repository=indexes, context_repository=contexts,
        )
    assert caught.value.status is KnowledgePreparationStatus.STALE


def test_real_corrupt_index_fails_closed(tmp_path):
    knowledge, indexes, contexts, settings, index = deployment(tmp_path, (
        document("company:601330", "绿色动力 环保 601330 公司背景"),
    ))
    path = indexes.root / f"lexical_index_version={index.lexical_index_version}.json"
    path.write_text("{", encoding="utf-8")
    with pytest.raises(KnowledgePreparationError) as caught:
        prepare_candidate_synthesis_input(
            bundle(), (), mode="strict_live", company_name="绿色动力",
            market_event_time=EVENT, research_corpus_cutoff=None, generated_at=CUTOFF,
            settings=settings, knowledge_repository=knowledge,
            index_repository=indexes, context_repository=contexts,
        )
    assert caught.value.status is KnowledgePreparationStatus.CORRUPT


def test_wrong_index_identity_at_configured_path_is_corrupt(tmp_path):
    knowledge, indexes, contexts, settings, index = deployment(tmp_path, (
        document("company:601330", "绿色动力 环保 601330 公司背景"),
    ))
    wrong_version = "0" * 64 if index.lexical_index_version != "0" * 64 else "1" * 64
    canonical_path = indexes.root / (
        f"lexical_index_version={index.lexical_index_version}.json"
    )
    wrong_path = indexes.root / f"lexical_index_version={wrong_version}.json"
    wrong_path.write_bytes(canonical_path.read_bytes())
    with pytest.raises(KnowledgePreparationError) as caught:
        prepare_candidate_synthesis_input(
            bundle(), (), mode="strict_live", company_name="绿色动力",
            market_event_time=EVENT, research_corpus_cutoff=None, generated_at=CUTOFF,
            settings=replace(settings, lexical_index_version=wrong_version),
            knowledge_repository=knowledge,
            index_repository=indexes,
            context_repository=contexts,
        )
    assert caught.value.status is KnowledgePreparationStatus.CORRUPT
    assert caught.value.safe_reason_code == "KNOWLEDGE_ARTIFACT_CORRUPT"


@pytest.mark.parametrize("error", [RuntimeError("boom"), TypeError("boom")])
def test_retrieval_internal_exception_is_failed_not_empty(tmp_path, monkeypatch, error):
    knowledge, indexes, contexts, settings, _ = deployment(tmp_path, ())
    monkeypatch.setattr(integration_module, "retrieve_knowledge",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(error))
    with pytest.raises(KnowledgePreparationError) as caught:
        prepare_candidate_synthesis_input(
            bundle(), (), mode="strict_live", company_name="绿色动力",
            market_event_time=EVENT, research_corpus_cutoff=None, generated_at=CUTOFF,
            settings=settings, knowledge_repository=knowledge,
            index_repository=indexes, context_repository=contexts,
        )
    assert caught.value.status is KnowledgePreparationStatus.FAILED


def test_assembly_internal_exception_is_failed_not_empty(tmp_path, monkeypatch):
    knowledge, indexes, contexts, settings, _ = deployment(tmp_path, ())
    monkeypatch.setattr(
        integration_module, "assemble_knowledge_context",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            integration_module.KnowledgeContextAssemblyError("boom")
        ),
    )
    with pytest.raises(KnowledgePreparationError) as caught:
        prepare_candidate_synthesis_input(
            bundle(), (), mode="strict_live", company_name="绿色动力",
            market_event_time=EVENT, research_corpus_cutoff=None, generated_at=CUTOFF,
            settings=settings, knowledge_repository=knowledge,
            index_repository=indexes, context_repository=contexts,
        )
    assert caught.value.status is KnowledgePreparationStatus.FAILED
    assert caught.value.safe_reason_code == "KNOWLEDGE_CONTEXT_ASSEMBLY_FAILED"


def test_context_persistence_failure_is_hard_before_provider(tmp_path):
    knowledge, indexes, _contexts, settings, _index = deployment(tmp_path, (
        document("company:601330", "绿色动力 环保 601330 公司背景"),
    ))
    client = FakeLLMClient({})

    class FailingContextRepository:
        def store(self, _context):
            raise integration_module.KnowledgeContextStorageError("publish failed")

    with pytest.raises(KnowledgePreparationError) as caught:
        result = prepare_candidate_synthesis_input(
            bundle(), (), mode="strict_live", company_name="绿色动力",
            market_event_time=EVENT, research_corpus_cutoff=None, generated_at=CUTOFF,
            settings=settings,
            knowledge_repository=knowledge,
            index_repository=indexes,
            context_repository=FailingContextRepository(),
        )
        synthesize_evidence(result.synthesis_input, client, clock=lambda: CUTOFF)
    assert caught.value.status is KnowledgePreparationStatus.FAILED
    assert caught.value.safe_reason_code == "KNOWLEDGE_STORAGE_FAILED"
    assert client.calls == []


def test_invalid_mode_is_integrity_failure_before_storage():
    with pytest.raises(KnowledgePreparationError) as caught:
        prepare_candidate_synthesis_input(
            bundle(), (), mode="invalid", company_name="绿色动力",
            market_event_time=EVENT, research_corpus_cutoff=None, generated_at=CUTOFF,
            settings=KnowledgeIntegrationSettings(enabled=True, lexical_index_version="0" * 64),
        )
    assert caught.value.status is KnowledgePreparationStatus.FAILED_INTEGRITY
    assert caught.value.safe_reason_code == "KNOWLEDGE_MODE_MISMATCH"


def test_integration_rejects_valid_context_with_wrong_as_of(tmp_path, monkeypatch):
    docs = (document("company:601330", "绿色动力 环保 601330 公司背景"),)
    normal, knowledge, indexes, contexts, settings, _ = prepare(tmp_path / "normal", docs)
    from quantos.schemas.knowledge_context import knowledge_context_id
    original = normal.knowledge_context
    wrong_as_of = EVENT + timedelta(hours=1)
    wrong_id = knowledge_context_id(
        policy=original.policy,
        retrieval_request_id=original.retrieval_request_id,
        mode=original.mode,
        as_of_time=wrong_as_of,
        corpus_cutoff=original.corpus_cutoff,
        corpus_version=original.corpus_version,
        chunk_manifest_version=original.chunk_manifest_version,
        lexical_index_version=original.lexical_index_version,
        retrieved_hit_count=original.retrieved_hit_count,
        selection_stop_reason=original.selection_stop_reason,
        selected_items=original.selected_items,
    )
    wrong = replace(original, as_of_time=wrong_as_of, context_id=wrong_id)
    monkeypatch.setattr(integration_module, "assemble_knowledge_context",
                        lambda *_args, **_kwargs: wrong)
    with pytest.raises(KnowledgePreparationError) as caught:
        prepare_candidate_synthesis_input(
            bundle(), (), mode="strict_live", company_name="绿色动力",
            market_event_time=EVENT, research_corpus_cutoff=None, generated_at=CUTOFF,
            settings=settings, knowledge_repository=knowledge,
            index_repository=indexes, context_repository=contexts,
        )
    assert caught.value.status is KnowledgePreparationStatus.FAILED_INTEGRITY


def test_configured_context_storage_is_append_only_idempotent(tmp_path):
    result, _knowledge, _indexes, contexts, _settings, _index = prepare(tmp_path, (
        document("company:601330", "绿色动力 环保 601330 公司背景"),
    ))
    assert contexts.store(replace(result.knowledge_context,
                                  generated_at=CUTOFF + timedelta(hours=1))) == result.context_path
    assert tuple(contexts.root.glob("context_id=*.json")) == (result.context_path,)


def test_daily_product_reuses_e2e1_helper_without_reimplementing_knowledge_algorithms():
    root = Path(__file__).parents[1]
    daily = (root / "scripts/generate_daily_report.py").read_text(encoding="utf-8")
    assert "prepare_candidate_synthesis_input" in daily
    assert "retrieve_knowledge(" not in daily
    assert "assemble_knowledge_context(" not in daily
    assert "build_knowledge_lexical_index" not in daily


def test_module_does_not_import_rebuild_network_product_or_scheduler_dependencies():
    source = Path(integration_module.__file__).read_text(encoding="utf-8")
    forbidden = (
        "build_knowledge_lexical_index", "knowledge_ingestion", "reporting",
        "time_slices", "scheduler", "collectors", "embedding", "vector",
    )
    assert all(term not in source for term in forbidden)
