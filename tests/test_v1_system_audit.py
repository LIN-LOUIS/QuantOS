"""Cross-layer regressions found by the QuantOS V1 final system audit."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta, timezone
from decimal import Decimal
import json
import os
from threading import Barrier

import pytest

from quantos.config import Settings, SynthesisBatchSettings
from quantos.reporting import validate_daily_report_identity
from quantos.schemas.run import RunType
from quantos.serialization import canonical_identity_json_bytes, canonical_json_bytes
from quantos.storage import SynthesisRepository
from quantos.storage.market import StorageError
from quantos.synthesis import (
    _synthesis_input_id, synthesis_record, synthesize_evidence,
    validate_synthesis_input,
)
from quantos.synthesis_runtime import SynthesisCacheKey, synthesize_batch
from tests.test_e2e_product_knowledge import knowledge_report
from tests.test_orchestration import setup_run
from tests.test_synthesis import NOW, synthesis_input, valid_payload
from tests.test_synthesis_runtime import (
    MODEL_NAME, MODEL_PROVIDER, REASONING, TrackingFactory, _generation_client,
    persist, runtime_input,
)
from tests.test_time_slices import CLOSE, product


def test_equivalent_timezone_offsets_preserve_cross_layer_logical_identities(tmp_path):
    context, _ = setup_run()
    assert canonical_identity_json_bytes({"at": context.as_of_time}) == (
        canonical_json_bytes({"at": context.as_of_time})
    )
    equivalent_context = replace(
        context, as_of_time=context.as_of_time.astimezone(timezone.utc),
    )
    assert equivalent_context.as_of_time == context.as_of_time
    assert equivalent_context.run_id == context.run_id

    synthesis = synthesis_input()
    equivalent_synthesis = replace(
        synthesis,
        candidate=replace(
            synthesis.candidate,
            market_event_time=synthesis.candidate.market_event_time.astimezone(
                timezone.utc,
            ),
        ),
    )
    assert _synthesis_input_id(
        equivalent_synthesis, equivalent_synthesis.knowledge_context,
    ) == synthesis.input_bundle_id
    validate_synthesis_input(equivalent_synthesis)

    daily, _, _ = knowledge_report(
        tmp_path / "daily", include_retrospective=False,
    )
    equivalent_daily = replace(
        daily, as_of_time=daily.as_of_time.astimezone(timezone.utc),
    )
    validate_daily_report_identity(equivalent_daily)
    assert equivalent_daily.report_id == daily.report_id

    time_slice, _, _, _, _ = product(
        tmp_path / "time-slice",
        run_type=RunType.POST_CLOSE,
        cutoff=CLOSE.replace(hour=20),
    )
    equivalent_time_slice = replace(
        time_slice,
        as_of_time=time_slice.as_of_time.astimezone(timezone.utc),
    )
    assert equivalent_time_slice.report_id == time_slice.report_id


def test_legacy_input_identity_is_validated_before_cache_lookup(tmp_path):
    value = runtime_input(0)
    repository = SynthesisRepository(Settings.from_project_root(tmp_path))
    persist(value, repository)
    tampered = replace(
        value,
        market_facts=replace(value.market_facts, return_pct=Decimal("99.99")),
    )
    factory = TrackingFactory()
    report = synthesize_batch(
        [tampered],
        client_factory=factory,
        repository=repository,
        model_provider="deepseek",
        model_name="deepseek-v4-flash",
        reasoning_config="default/high",
        settings=SynthesisBatchSettings(1, 1),
    )
    assert report.results[0].status == "FAIL"
    assert report.results[0].validation_error_code == "KNOWLEDGE_CONTEXT_MISMATCH"
    assert report.cache_hits == report.cache_misses == 0
    assert report.actual_llm_request_count == factory.calls == 0


def test_synthesis_cache_publish_failure_leaves_no_partial_artifact(
    tmp_path, monkeypatch,
):
    value = runtime_input(0)
    repository = SynthesisRepository(Settings.from_project_root(tmp_path))

    def fail_publish(_source, _target):
        raise OSError("injected atomic publish failure")

    monkeypatch.setattr(os, "link", fail_publish)
    with pytest.raises(OSError, match="atomic publish failure"):
        persist(value, repository)
    assert list(repository.settings.synthesis_dir.rglob("*.json")) == []
    assert list(repository.settings.synthesis_dir.rglob(".synthesis-*.tmp")) == []


def _cache_fixture(tmp_path):
    value = runtime_input(0)
    key = SynthesisCacheKey.for_input(
        value,
        model_provider=MODEL_PROVIDER,
        model_name=MODEL_NAME,
        reasoning_config=REASONING,
    ).as_dict()
    payload = valid_payload(evidence_id=value.attribution_evidence[0].evidence_id)
    payload["symbol"] = value.symbol
    first = synthesize_evidence(
        value, _generation_client(value, payload), clock=lambda: NOW,
    )
    second_payload = dict(payload)
    second_payload["evidence_summary"] = "另一份独立合法的证据摘要。"
    second = synthesize_evidence(
        value, _generation_client(value, second_payload), clock=lambda: NOW,
    )
    settings = Settings.from_project_root(tmp_path)
    return settings, key, first, second


def test_same_cache_key_is_idempotent_and_different_payload_collides(tmp_path):
    settings, key, first, different = _cache_fixture(tmp_path)
    repository = SynthesisRepository(settings)
    original = repository.write(first, cache_key=key)
    original_bytes = original.read_bytes()
    observationally_different = replace(
        first,
        generated_at=first.generated_at + timedelta(seconds=1),
        input_tokens=(first.input_tokens or 0) + 1,
        duration_ms=(first.duration_ms or 0) + 1,
    )
    assert repository.write(observationally_different, cache_key=key) == original
    assert len(list(repository.settings.synthesis_dir.rglob("*.json"))) == 1
    with pytest.raises(StorageError, match="logical-key collision"):
        repository.write(different, cache_key=key)
    assert original.read_bytes() == original_bytes
    assert repository.find_valid(key) == first
    assert len(list(repository.settings.synthesis_dir.rglob("*.json"))) == 1


@pytest.mark.parametrize("different_payloads", [False, True])
def test_concurrent_cache_writers_have_one_logical_winner(
    tmp_path, different_payloads,
):
    settings, key, first, different = _cache_fixture(tmp_path)
    barrier = Barrier(2)

    def write(output):
        repository = SynthesisRepository(settings)
        barrier.wait()
        try:
            return "SUCCESS", repository.write(output, cache_key=key)
        except StorageError:
            return "COLLISION", None

    outputs = (first, different if different_payloads else replace(
        first, generated_at=first.generated_at + timedelta(seconds=1),
    ))
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(write, outputs))
    statuses = sorted(status for status, _ in results)
    if different_payloads:
        assert statuses == ["COLLISION", "SUCCESS"]
    else:
        assert statuses == ["SUCCESS", "SUCCESS"]
        assert len({path for _, path in results}) == 1
    assert len(list(settings.synthesis_dir.rglob("*.json"))) == 1
    assert SynthesisRepository(settings).find_valid(key) is not None


def test_preexisting_different_payloads_for_same_key_fail_lookup_closed(tmp_path):
    settings, key, first, different = _cache_fixture(tmp_path)
    target = settings.synthesis_dir / "generated_date=2026-09-03"
    target.mkdir(parents=True, exist_ok=True)
    for index in (1, 2):
        record = dict(synthesis_record(first))
        record["cache_key"] = key
        (target / f"historical-{index}.json").write_text(
            json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    assert SynthesisRepository(settings).find_valid(key) == first
    different_record = dict(synthesis_record(different))
    different_record["cache_key"] = key
    (target / "historical-2.json").write_text(
        json.dumps(different_record, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(StorageError, match="logical-key collision"):
        SynthesisRepository(settings).find_valid(key)
