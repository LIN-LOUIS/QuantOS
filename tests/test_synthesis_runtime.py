import json
import threading
import time
from dataclasses import replace

from quantos.config import Settings, SynthesisBatchSettings
from quantos.schemas import SynthesisInput
from quantos.storage import SynthesisRepository
from quantos.synthesis import (
    LLMGeneration, SynthesisValidationError, _synthesis_input_id,
    diagnose_structured_response,
    evidence_synthesis_json_schema, synthesize_evidence,
)
from quantos.synthesis_runtime import SynthesisCacheKey, synthesize_batch
from tests.test_synthesis import NOW, provider_response, synthesis_input, valid_payload


MODEL_PROVIDER = "deepseek"
MODEL_NAME = "deepseek-v4-flash"
REASONING = "default/high"


class TrackingFactory:
    def __init__(self, *, failures=(), delay=0.0):
        self.failures = set(failures)
        self.delay = delay
        self.calls = 0
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()

    def __call__(self, value: SynthesisInput):
        parent = self

        class Client:
            def generate_structured(self, **_kwargs):
                with parent.lock:
                    parent.calls += 1
                    parent.active += 1
                    parent.max_active = max(parent.max_active, parent.active)
                try:
                    if parent.delay:
                        time.sleep(parent.delay)
                    if value.symbol in parent.failures:
                        raise RuntimeError("safe fake failure")
                    payload = valid_payload(evidence_id=value.attribution_evidence[0].evidence_id)
                    payload["symbol"] = value.symbol
                    raw = provider_response(json.dumps(payload))
                    parsed, diagnostics = diagnose_structured_response(
                        raw, evidence_synthesis_json_schema(),
                    )
                    diagnostics = replace(diagnostics, request_duration_ms=25)
                    return LLMGeneration(
                        parsed, MODEL_PROVIDER, MODEL_NAME, 21, 13, 34, diagnostics,
                    )
                finally:
                    with parent.lock:
                        parent.active -= 1

        return Client()


def runtime_input(index: int, *, evidence=True, rank=None) -> SynthesisInput:
    value = synthesis_input(no_evidence=not evidence)
    symbol = f"{601330 + index:06d}.SH"
    value = replace(
        value,
        candidate=replace(
            value.candidate, symbol=symbol, rank=rank if rank is not None else index + 1,
        ),
    )
    return replace(
        value,
        input_bundle_id=_synthesis_input_id(value, value.knowledge_context),
    )


def key(value, *, model=MODEL_NAME, reasoning=REASONING):
    return SynthesisCacheKey.for_input(
        value, model_provider=MODEL_PROVIDER, model_name=model,
        reasoning_config=reasoning,
    )


def run(values, tmp_path, factory=None, *, top_n=20, concurrency=2, model=MODEL_NAME,
        reasoning=REASONING):
    client_factory = factory or TrackingFactory()
    report = synthesize_batch(
        values, client_factory=client_factory,
        repository=SynthesisRepository(Settings.from_project_root(tmp_path)),
        model_provider=MODEL_PROVIDER, model_name=model, reasoning_config=reasoning,
        settings=SynthesisBatchSettings(top_n, concurrency),
    )
    return report, client_factory


def persist(value, repository, *, model=MODEL_NAME, reasoning=REASONING):
    payload = valid_payload(evidence_id=value.attribution_evidence[0].evidence_id)
    payload["symbol"] = value.symbol
    output = synthesize_evidence(
        value, _generation_client(value, payload), clock=lambda: NOW,
    )
    repository.write(output, cache_key=key(value, model=model, reasoning=reasoning).as_dict())


def _generation_client(value, payload, *, invalid=False):
    class Client:
        def generate_structured(self, **_kwargs):
            raw = provider_response(json.dumps(payload))
            parsed, diagnostics = diagnose_structured_response(
                raw, evidence_synthesis_json_schema(),
            )
            diagnostics = replace(diagnostics, request_duration_ms=25)
            generation = LLMGeneration(
                parsed, MODEL_PROVIDER, MODEL_NAME, 21, 13, 34, diagnostics,
            )
            if invalid:
                generation.payload["used_evidence_ids"] = ["unknown"]
            return generation
    return Client()


def test_exact_pass_cache_hit_has_zero_provider_calls(tmp_path):
    value = runtime_input(0)
    repo = SynthesisRepository(Settings.from_project_root(tmp_path))
    persist(value, repo)
    factory = TrackingFactory()
    report = synthesize_batch(
        [value], client_factory=factory, repository=repo,
        model_provider=MODEL_PROVIDER, model_name=MODEL_NAME,
        reasoning_config=REASONING,
    )
    assert report.results[0].status == "CACHE_HIT"
    assert report.planned_llm_request_count == report.actual_llm_request_count == 0
    assert factory.calls == 0


def test_prompt_model_and_reasoning_are_cache_key_fields(tmp_path):
    value = runtime_input(0)
    repo = SynthesisRepository(Settings.from_project_root(tmp_path))
    persist(value, repo)
    variants = [
        (
            replace(
                replace(value, prompt_version="quantos-synthesis-v2"),
                input_bundle_id=_synthesis_input_id(
                    replace(value, prompt_version="quantos-synthesis-v2"),
                    value.knowledge_context,
                ),
            ),
            MODEL_NAME,
            REASONING,
        ),
        (value, "different-model", REASONING),
        (value, MODEL_NAME, "low"),
    ]
    for changed, model, reasoning in variants:
        factory = TrackingFactory()
        report = synthesize_batch(
            [changed], client_factory=factory, repository=repo,
            model_provider=MODEL_PROVIDER, model_name=model,
            reasoning_config=reasoning,
        )
        assert report.cache_misses == report.actual_llm_request_count == 1
        assert factory.calls == 1


def test_failed_historical_artifact_is_a_cache_miss(tmp_path):
    value = runtime_input(0)
    repo = SynthesisRepository(Settings.from_project_root(tmp_path))
    target = repo.settings.synthesis_dir / "failed.json"
    target.write_text(json.dumps({
        "validation_result": "FAIL", "cache_key": key(value).as_dict(),
        "structured_output": {},
    }))
    report, factory = run([value], tmp_path)
    assert report.cache_misses == 1 and factory.calls == 1


def test_pass_artifact_with_mismatched_internal_model_is_a_miss(tmp_path):
    value = runtime_input(0)
    repo = SynthesisRepository(Settings.from_project_root(tmp_path))
    persist(value, repo)
    path = next(repo.settings.synthesis_dir.rglob("*.json"))
    record = json.loads(path.read_text())
    record["structured_output"]["model_name"] = "other-model"
    path.write_text(json.dumps(record))
    report, factory = run([value], tmp_path)
    assert report.cache_misses == 1 and factory.calls == 1


def test_no_evidence_and_outside_top_n_make_no_provider_calls_and_preserve_ranks(tmp_path):
    selected = runtime_input(0, evidence=False, rank=1)
    outside = runtime_input(1, rank=2)
    original_ranks = [selected.candidate.rank, outside.candidate.rank]
    report, factory = run([outside, selected], tmp_path, top_n=1)
    assert factory.calls == 0
    assert [selected.candidate.rank, outside.candidate.rank] == original_ranks
    assert {item.status for item in report.results} == {
        "NOT_SELECTED", "NO_EVIDENCE_FAST_PATH",
    }


def test_bounded_concurrency_and_candidate_failure_isolation_without_retry(tmp_path):
    values = [runtime_input(index) for index in range(4)]
    factory = TrackingFactory(failures={values[0].symbol}, delay=0.03)
    report, _ = run(values, tmp_path, factory, concurrency=2)
    assert factory.calls == 4
    assert factory.max_active == report.max_concurrency_observed == 2
    assert [item.status for item in report.results].count("FAIL") == 1
    assert [item.status for item in report.results].count("PASS") == 3


def test_failed_validation_usage_is_aggregated_without_retry(tmp_path):
    value = runtime_input(0)
    calls = 0

    def factory(candidate):
        nonlocal calls
        payload = valid_payload(evidence_id=candidate.attribution_evidence[0].evidence_id)
        payload["symbol"] = candidate.symbol
        client = _generation_client(candidate, payload, invalid=True)

        class CountingClient:
            def generate_structured(self, **kwargs):
                nonlocal calls
                calls += 1
                return client.generate_structured(**kwargs)
        return CountingClient()

    report = synthesize_batch(
        [value], client_factory=factory,
        repository=SynthesisRepository(Settings.from_project_root(tmp_path)),
        model_provider=MODEL_PROVIDER, model_name=MODEL_NAME,
        reasoning_config=REASONING,
    )
    assert calls == report.actual_llm_request_count == 1
    assert report.results[0].status == "FAIL"
    assert report.results[0].validation_error_code == "UNKNOWN_EVIDENCE_ID"
    assert (report.total_input_tokens, report.total_cached_input_tokens) == (21, 2)
    assert (report.total_output_tokens, report.total_reasoning_tokens) == (13, 8)
    assert (report.total_tokens, report.total_llm_duration_ms) == (34, 25)


def test_same_key_concurrent_candidates_generate_once(tmp_path):
    value = runtime_input(0)
    factory = TrackingFactory(delay=0.02)
    report, _ = run([value, value], tmp_path, factory, concurrency=2)
    assert factory.calls == report.actual_llm_request_count == 1
    assert report.duplicate_generation_prevented == 1
    assert [item.status for item in report.results] == ["PASS", "CACHE_HIT"]


def test_fake_batch_accounting_tokens_cache_fast_path_and_secret_free(tmp_path):
    cached = runtime_input(0, rank=1)
    no_evidence = runtime_input(1, evidence=False, rank=2)
    generated = runtime_input(2, rank=3)
    outside = runtime_input(3, rank=5)
    repo = SynthesisRepository(Settings.from_project_root(tmp_path))
    persist(cached, repo)
    factory = TrackingFactory()
    report = synthesize_batch(
        [cached, no_evidence, generated, generated, outside],
        client_factory=factory, repository=repo,
        model_provider=MODEL_PROVIDER, model_name=MODEL_NAME,
        reasoning_config=REASONING, settings=SynthesisBatchSettings(4, 2),
    )
    assert (report.candidate_count, report.selected_candidate_count) == (5, 4)
    assert report.planned_llm_request_count == report.actual_llm_request_count == 1
    assert (report.cache_hits, report.cache_misses) == (2, 2)
    assert report.no_evidence_fast_path_count == 1
    assert report.requests_avoided_by_cache == 2
    assert report.requests_avoided_by_no_evidence == 1
    assert report.total_requests_avoided == 3
    assert report.duplicate_generation_prevented == 1
    assert (report.total_input_tokens, report.total_cached_input_tokens) == (21, 2)
    assert (report.total_output_tokens, report.total_reasoning_tokens) == (13, 8)
    assert (report.total_tokens, report.total_llm_duration_ms) == (34, 25)
    persisted = "\n".join(
        path.read_text() for path in repo.settings.synthesis_dir.rglob("*.json")
    )
    assert "api_key" not in persisted.lower() and "authorization" not in persisted.lower()


def test_cache_identity_changes_for_every_required_field():
    value = runtime_input(0)
    baseline = key(value)
    changes = [
        replace(baseline, input_bundle_id="other"),
        replace(baseline, prompt_version="v2"),
        replace(baseline, schema_version="v2"),
        replace(baseline, model_provider="other"),
        replace(baseline, model_name="other"),
        replace(baseline, reasoning_config="low"),
    ]
    assert len({baseline.identity, *(item.identity for item in changes)}) == 7
