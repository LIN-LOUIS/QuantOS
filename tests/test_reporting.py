import json
from dataclasses import replace
from datetime import datetime
from decimal import Decimal

import pytest

from quantos.calibration import (
    calibrate_attribution_evidence, calibrate_event_evidence,
    link_stored_query_a_results,
)
from quantos.config import MARKET_TIMEZONE, Settings
from quantos.reporting import (
    PROVENANCE_FIELDS, RESEARCH_DISCLAIMER, STRICT_DISCLAIMER,
    assemble_daily_report, canonical_json_bytes, generate_daily_report,
    render_daily_report, report_from_dict, report_to_dict,
)
from quantos.schemas import AnomalyStatus, MarketSnapshot, StockAnomaly
from quantos.storage import SynthesisRepository
from quantos.synthesis import LLMGeneration, build_synthesis_input
from tests.test_synthesis import EVENT, NOW, candidate, evidence, flow, valid_payload


class FakeFactory:
    def __init__(self, fail_symbols=()):
        self.calls = 0
        self.fail_symbols = set(fail_symbols)

    def __call__(self, value):
        parent = self

        class Client:
            def generate_structured(self, **_kwargs):
                parent.calls += 1
                if value.symbol in parent.fail_symbols:
                    raise RuntimeError("fake safe failure")
                payload = valid_payload(evidence_id=value.attribution_evidence[0].evidence_id)
                payload["symbol"] = value.symbol
                payload["post_event_notes"] = []
                return LLMGeneration(payload, "deepseek", "deepseek-v4-flash", 10, 5, 15)
        return Client()


def artifacts(*, count=2, strict=False):
    candidates = []
    anomalies = []
    records = []
    names = {}
    flows = []
    for index in range(count):
        symbol = f"{601330 + index:06d}.SH"
        item = replace(candidate(symbol), rank=index + 1, priority_tier=index + 1)
        candidates.append(item)
        names[symbol] = f"公司{index + 1}"
        flows.append(flow(symbol))
        anomalies.append(StockAnomaly(
            item.trade_date, item.as_of_time, symbol, item.return_pct,
            item.return_zscore, item.price_anomaly, 100 + index, Decimal("50"),
            item.volume_ratio, item.volume_anomaly, Decimal("1000") + index,
            Decimal("500"), item.amount_ratio, item.amount_anomaly, 20,
            AnomalyStatus.OK, item.available_at,
        ))
        record = evidence(f"e{index + 1}", strict=strict)
        records.append(replace(
            record, query_symbol=symbol, query=names[symbol],
            title=f"{names[symbol]}公告", provider_record_id=f"pr-{index}",
        ))
    event_selection = calibrate_event_evidence(
        records, link_stored_query_a_results(records), market_event_time=EVENT,
    )
    attribution = calibrate_attribution_evidence(
        event_selection, candidates, flows, market_event_time=EVENT, as_of_time=NOW,
    )
    market = MarketSnapshot(
        candidates[0].trade_date, NOW, "shse_szse_a_share", count, count,
        Decimal("1"), count, 0, 0, Decimal("1"), 1000 * count,
        Decimal("1000000") * count, 0, 0, 0, 0, (), (), 0, 0, (), NOW,
    )
    inputs = [build_synthesis_input(
        bundle, attribution.attribution_facts,
        mode="strict_live" if strict else "research",
        company_name=names[bundle.candidate.symbol], market_event_time=EVENT,
    ) for bundle in attribution.bundles]
    return market, anomalies, candidates, names, attribution, inputs


def build_report(tmp_path, *, strict=False, count=2, top_n=2, no_llm=False,
                 fail_symbols=(), generated_at=NOW):
    market, anomalies, candidates, names, attribution, inputs = artifacts(
        count=count, strict=strict,
    )
    factory = FakeFactory(fail_symbols)
    report = generate_daily_report(
        market_snapshot=market, sector_snapshots=(), anomalies=anomalies,
        candidates=candidates, company_names=names, attribution=attribution,
        synthesis_inputs=inputs, client_factory=factory,
        synthesis_repository=SynthesisRepository(Settings.from_project_root(tmp_path)),
        mode="strict_live" if strict else "research", top_n=top_n,
        generated_at=generated_at, no_llm=no_llm,
        provenance={"quantos_git_commit": "c903c57", "synthesis_prompt_version": "quantos-synthesis-v1"},
    )
    return report, factory, (market, anomalies, candidates, attribution, inputs)


def test_research_and_strict_report_assembly_and_disclaimers(tmp_path):
    research, _, _ = build_report(tmp_path / "research")
    strict, factory, _ = build_report(tmp_path / "strict", strict=True)
    assert research.mode == "research" and RESEARCH_DISCLAIMER in render_daily_report(research)
    assert strict.mode == "strict_live" and STRICT_DISCLAIMER in render_daily_report(strict)
    assert strict.data_quality["evidence"]["historical_proxy_used"] is False
    assert all(item.evidence_stats.eligible_attribution_count == 0 for item in strict.candidate_briefs)
    assert factory.calls == 0


def test_top_n_preserves_ranks_and_total_candidate_count(tmp_path):
    report, _, (_, _, candidates, _, _) = build_report(tmp_path, count=3, top_n=2)
    assert [item.rank for item in report.candidate_briefs] == [1, 2]
    assert [item.rank for item in candidates] == [1, 2, 3]
    assert report.candidate_summary == {
        "total_candidates": 3, "selected_for_report": 2,
        "not_selected": 1, "requested_top_n": 2,
    }


def test_canonical_market_anomaly_and_fund_flow_numbers_are_copied(tmp_path):
    report, _, (market, anomalies, _, _, _) = build_report(tmp_path)
    assert report.market_overview["total_amount"] == market.total_amount
    assert report.candidate_briefs[0].market_facts["return_zscore"] == anomalies[0].return_zscore
    assert report.candidate_briefs[0].market_facts["volume"] == anomalies[0].volume
    assert report.candidate_briefs[0].fund_flow["net_mf_amount"] == Decimal("-100")
    assert report.sector_overview["classification_system"] == "证监会行业分类"


def test_generated_cache_hit_fast_path_and_fail_status_mapping(tmp_path):
    generated, _, (_, _, candidates, _, inputs) = build_report(tmp_path / "generated", top_n=1)
    assert generated.candidate_briefs[0].synthesis.status == "GENERATED"
    strict, _, _ = build_report(tmp_path / "strict", strict=True, top_n=1)
    assert strict.candidate_briefs[0].synthesis.status == "NO_EVIDENCE_FAST_PATH"
    failed, factory, _ = build_report(
        tmp_path / "failed", top_n=2, fail_symbols={candidates[0].symbol},
    )
    assert factory.calls == 2
    assert [item.synthesis.status for item in failed.candidate_briefs] == ["FAIL", "GENERATED"]
    assert failed.candidate_briefs[0].synthesis.possible_explanations == ()


def test_no_llm_cache_miss_and_no_evidence_never_call_factory(tmp_path):
    research, factory, _ = build_report(tmp_path / "research", no_llm=True)
    assert factory.calls == 0
    assert all(item.synthesis.error_category == "LLM_DISABLED_CACHE_MISS"
               for item in research.candidate_briefs)
    strict, strict_factory, _ = build_report(tmp_path / "strict", strict=True, no_llm=True)
    assert strict_factory.calls == 0
    assert all(item.synthesis.status == "NO_EVIDENCE_FAST_PATH"
               for item in strict.candidate_briefs)


def test_cache_hit_mapping_and_no_llm_cache_hit(tmp_path):
    first, first_factory, _ = build_report(tmp_path, top_n=1)
    assert first_factory.calls == 1
    second, second_factory, _ = build_report(tmp_path, top_n=1, no_llm=True)
    assert second_factory.calls == 0
    assert second.candidate_briefs[0].synthesis.status == "CACHE_HIT"
    assert second.synthesis_runtime["cache_hits"] == 1
    assert second.synthesis_runtime["actual_llm_requests"] == 0


def test_evidence_quality_and_runtime_aggregations(tmp_path):
    report, _, _ = build_report(tmp_path)
    assert report.evidence_overview["discovery_evidence_count"] == 2
    assert report.evidence_overview["event_evidence_count"] == 2
    assert report.evidence_overview["research_attribution_count"] == 2
    assert report.synthesis_runtime["actual_llm_requests"] == 2
    assert report.synthesis_runtime["input_tokens"] == 20
    assert report.synthesis_runtime["total_tokens"] == 30
    assert report.data_quality["evidence"]["historical_proxy_used"] is True


def test_provenance_keys_roundtrip_report_id_and_markdown_are_deterministic(tmp_path):
    first, _, _ = build_report(tmp_path / "a", generated_at=NOW)
    later = NOW.replace(microsecond=NOW.microsecond + 1)
    second, _, _ = build_report(tmp_path / "b", generated_at=later)
    assert first.report_id == second.report_id
    assert set(first.provenance) == set(PROVENANCE_FIELDS)
    assert first.provenance["triage_version"] is None
    encoded = canonical_json_bytes(report_to_dict(first))
    decoded = report_from_dict(json.loads(encoded))
    assert decoded.report_id == first.report_id
    assert render_daily_report(decoded) == render_daily_report(decoded)


def test_invalid_model_prose_is_not_rendered_and_numbers_are_not_parsed(tmp_path):
    report, _, _ = build_report(tmp_path, fail_symbols={"601330.SH"})
    markdown = render_daily_report(report)
    assert "fake safe failure" not in markdown
    assert "证据综合生成失败" in markdown
    assert report.candidate_briefs[0].market_facts["return_pct"] == Decimal("10.25")


def test_strict_rejects_proxy_mislabeled_attribution(tmp_path):
    market, anomalies, candidates, names, attribution, inputs = artifacts(strict=True)
    bad_record = attribution.bundles[0].strict_attribution_evidence
    # The calibrated strict channel is empty because records were known after the event.
    assert bad_record == ()
    proxy = evidence("proxy")
    bad_bundle = replace(attribution.bundles[0], strict_attribution_evidence=(proxy,))
    bad_attribution = replace(attribution, bundles=(bad_bundle, *attribution.bundles[1:]))
    runtime, _, _ = build_report(tmp_path / "runtime", strict=True)
    with pytest.raises(ValueError, match="proxy attribution"):
        assemble_daily_report(
            market_snapshot=market, sector_snapshots=(), anomalies=anomalies,
            candidates=candidates, company_names=names, attribution=bad_attribution,
            synthesis_inputs={}, synthesis_report=_empty_runtime(), mode="strict_live",
            top_n=2, generated_at=NOW,
        )


def test_no_historical_sector_backprojection(tmp_path):
    report, _, _ = build_report(tmp_path)
    assert all(item.sector_context is None for item in report.candidate_briefs)


def _empty_runtime():
    from quantos.synthesis_runtime import SynthesisBatchReport
    return SynthesisBatchReport((), 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0.0,
                                0, 0, 0, 0, 0, 0, 0, 0, 0)
