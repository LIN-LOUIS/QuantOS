"""Evaluation harness tests; production core semantics remain frozen."""

import csv
from datetime import date
import json
import pytest
import quantos.evaluation as evaluation

from quantos.evaluation import (
    DayObservation,
    EvaluationDataSource,
    _build_strict_day,
    evaluate_observations,
    run_synthetic_strict_evaluation,
)


def _observation(built, trade_date):
    return DayObservation(
        trade_date=trade_date,
        data_source=EvaluationDataSource.SYNTHETIC_FIXTURE,
        report=built["report"],
        daily_ref=built["daily_ref"],
        post_close_manifest=built["manifest"],
        post_close_manifest_ref=built["manifest_ref"],
        post_close_time_slice_id=built["time_slice_id"],
        candidates=built["candidates"],
        duration_ms=built["duration_ms"],
    )


def test_single_candidate_fixture_synthesis_is_generated(tmp_path):
    result = run_synthetic_strict_evaluation(
        tmp_path / "evaluation", trading_days=1, candidate_count=1,
    )
    assert result.summary["candidate_count"] == 1
    assert result.summary["provider_invocation_count"] == 1
    assert result.summary["synthesis_success_rate"] == 1.0
    assert result.summary["pre_open_replay_success_rate"] == 0.0
    assert result.candidates[0]["synthesis_status"] == "GENERATED"
    assert result.failure_cases == ()


def test_two_candidate_strict_day_generates_both_syntheses(tmp_path):
    result = run_synthetic_strict_evaluation(
        tmp_path / "evaluation", trading_days=1,
    )
    assert result.summary["candidate_count"] == 2
    assert result.summary["provider_invocation_count"] == 2
    assert result.summary["synthesis_success_rate"] == 1.0
    assert {row["synthesis_status"] for row in result.candidates} == {"GENERATED"}
    assert result.failure_cases == ()


def test_ten_day_strict_fixture_evaluation_is_complete_and_pit_safe(tmp_path):
    result = run_synthetic_strict_evaluation(tmp_path / "evaluation", trading_days=10)
    summary = result.summary
    assert summary["data_source"] == EvaluationDataSource.SYNTHETIC_FIXTURE.value
    assert summary["trading_days_evaluated"] == 10
    assert summary["run_count"] == 19
    assert summary["candidate_count"] == 20
    assert summary["post_close_success_rate"] == 1.0
    assert summary["pre_open_replay_success_rate"] == 1.0
    assert summary["evidence_coverage_rate"] == 1.0
    assert summary["attribution_coverage_rate"] == 1.0
    assert summary["knowledge_ready_rate"] == 0.5
    assert summary["knowledge_empty_rate"] == 0.5
    assert summary["synthesis_success_rate"] == 1.0
    assert summary["average_evidence_refs_per_candidate"] == 1.0
    assert summary["average_attribution_refs_per_candidate"] == 1.0
    assert summary["evidence_to_attribution_dropoff_rate"] == 0.0
    assert summary["daily_v1_count"] == 0
    assert summary["daily_v2_count"] == 10
    assert summary["pre_open_extra_retrieval_count"] == 0
    assert summary["pre_open_extra_context_assembly_count"] == 0
    assert summary["pre_open_extra_synthesis_count"] == 0
    assert summary["operational_failure_distribution"]["READY"] == 19
    assert sum(summary["operational_failure_distribution"].values()) == 19
    assert sum(summary["knowledge_candidate_status_distribution"].values()) == 20
    assert summary["knowledge_candidate_status_distribution"]["READY"] == 10
    assert summary["knowledge_candidate_status_distribution"]["EMPTY"] == 10
    assert sum(summary["synthesis_candidate_status_distribution"].values()) == 20
    assert summary["pit_violation_count"] == 0
    assert summary["causal_validation_failure_count"] == 0
    assert summary["determinism_mismatch_count"] == 0
    assert summary["provider_network_request_count"] == 0
    assert summary["provider_invocation_count"] == 20
    assert summary["total_input_tokens"] == 200
    assert summary["total_output_tokens"] == 100
    assert summary["total_tokens"] == 300
    assert summary["real_deepseek_request_count"] == 0
    assert summary["embedding_provider_request_count"] == 0
    assert result.failure_cases == ()


def test_evaluation_outputs_have_run_day_candidate_and_failure_layers(tmp_path):
    output = tmp_path / "evaluation"
    result = run_synthetic_strict_evaluation(output, trading_days=2)
    expected = {
        "evaluation_summary.json", "evaluation_runs.csv", "evaluation_days.csv",
        "evaluation_candidates.csv", "failure_cases.json", "evaluation_report.md",
    }
    assert expected <= {path.name for path in output.iterdir()}
    assert json.loads((output / "evaluation_summary.json").read_text())["evaluation_id"] == (
        result.summary["evaluation_id"]
    )
    with (output / "evaluation_runs.csv").open() as stream:
        assert len(tuple(csv.DictReader(stream))) == 3
    with (output / "evaluation_days.csv").open() as stream:
        assert len(tuple(csv.DictReader(stream))) == 2
    with (output / "evaluation_candidates.csv").open() as stream:
        rows = tuple(csv.DictReader(stream))
    assert len(rows) == 4
    assert {row["knowledge_preparation_status"] for row in rows} == {"READY", "EMPTY"}
    report = (output / "evaluation_report.md").read_text()
    assert "NOT REAL-HISTORICAL PERFORMANCE" in report
    assert "Candidate Knowledge READY: 50.00%" in report
    assert "Run-level operational READY: 3/3" in report
    with (output / "evaluation_runs.csv").open() as stream:
        run_rows = tuple(csv.DictReader(stream))
    assert all(not row["manifest_ref"].startswith("/") for row in run_rows)


def test_evaluation_csv_artifacts_use_lf_only_and_remain_parseable(tmp_path):
    output = tmp_path / "evaluation"
    run_synthetic_strict_evaluation(output, trading_days=1)
    expected_rows = {
        "evaluation_runs.csv": 1,
        "evaluation_days.csv": 1,
        "evaluation_candidates.csv": 2,
    }
    for name, row_count in expected_rows.items():
        raw = (output / name).read_bytes()
        assert b"\r\n" not in raw
        assert b"\r" not in raw
        with (output / name).open(encoding="utf-8", newline="") as stream:
            assert len(tuple(csv.DictReader(stream))) == row_count


def test_pre_open_reuses_exact_prior_daily_without_reexecution(tmp_path):
    result = run_synthetic_strict_evaluation(tmp_path / "evaluation", trading_days=3)
    pre_open_runs = [row for row in result.runs if row["run_type"] == "PRE_OPEN"]
    assert len(pre_open_runs) == 2
    assert all(row["is_success"] for row in pre_open_runs)
    assert all(row["knowledge_operational_status"] == "READY" for row in pre_open_runs)
    assert all(row["safe_reason_code"] == "KNOWLEDGE_READY" for row in pre_open_runs)
    assert result.summary["pre_open_extra_retrieval_count"] == 0
    assert result.summary["pre_open_extra_context_assembly_count"] == 0
    assert result.summary["pre_open_extra_synthesis_count"] == 0


def test_evaluation_identity_is_path_independent_and_binds_candidate_cardinality(tmp_path):
    first = run_synthetic_strict_evaluation(
        tmp_path / "first", trading_days=2, candidate_count=2,
    )
    relocated = run_synthetic_strict_evaluation(
        tmp_path / "relocated", trading_days=2, candidate_count=2,
    )
    smaller = run_synthetic_strict_evaluation(
        tmp_path / "smaller", trading_days=2, candidate_count=1,
    )
    assert first.summary["evaluation_id"] == relocated.summary["evaluation_id"]
    assert first.summary["evaluation_id"] != smaller.summary["evaluation_id"]
    assert first.summary["determinism_mismatch_count"] == 0
    assert relocated.summary["determinism_mismatch_count"] == 0


def test_evaluation_session_is_immutable_and_existing_results_are_preserved(tmp_path):
    output = tmp_path / "evaluation"
    run_synthetic_strict_evaluation(output, trading_days=1)
    names = (
        "evaluation_summary.json", "evaluation_runs.csv", "evaluation_days.csv",
        "evaluation_candidates.csv", "failure_cases.json", "evaluation_report.md",
    )
    original = {name: (output / name).read_bytes() for name in names}
    with pytest.raises(FileExistsError, match="already exists"):
        run_synthetic_strict_evaluation(output, trading_days=1)
    assert {name: (output / name).read_bytes() for name in names} == original


def test_failed_candidate_remains_in_denominator_and_failure_artifact(tmp_path):
    built = _build_strict_day(
        tmp_path / "artifacts", date(2026, 8, 24),
        failed_symbols=("601331.SH",),
    )
    observation = _observation(built, date(2026, 8, 24))
    result = evaluate_observations((observation,))
    assert result.summary["candidate_count"] == 2
    assert result.summary["synthesis_success_rate"] == 0.5
    assert result.summary["synthesis_candidate_status_distribution"] == {
        "GENERATED": 1, "CACHE_HIT": 0, "NO_EVIDENCE_FAST_PATH": 0, "FAIL": 1,
    }
    assert result.summary["failure_case_count"] == 1
    assert len(result.candidates) == 2
    assert result.failure_cases[0]["failure_stage"] == "SYNTHESIS"
    assert "synthetic evaluation failure" not in json.dumps(result.failure_cases)
    evaluation.write_evaluation_artifacts(result, tmp_path / "results")
    persisted = json.loads((tmp_path / "results" / "failure_cases.json").read_text())
    assert len(persisted["failure_cases"]) == 1
    assert persisted["failure_cases"][0]["failure_stage"] == "SYNTHESIS"


def test_zero_failure_artifact_has_closed_empty_shape(tmp_path):
    output = tmp_path / "evaluation"
    run_synthetic_strict_evaluation(output, trading_days=1)
    payload = json.loads((output / "failure_cases.json").read_text())
    assert payload == {
        "schema_version": "quantos-v1-evaluation-v1",
        "failure_cases": [],
    }


def test_failed_result_publication_leaves_no_partial_session(tmp_path, monkeypatch):
    output = tmp_path / "evaluation"

    def reject_staging(_staging, _result):
        raise ValueError("injected staged-result rejection")

    monkeypatch.setattr(evaluation, "_validate_staged_evaluation", reject_staging)
    with pytest.raises(ValueError, match="staged-result rejection"):
        run_synthetic_strict_evaluation(output, trading_days=1)
    assert not output.exists()


def test_missing_eligible_pre_open_is_a_failed_opportunity_not_dropped(tmp_path):
    first_date = date(2026, 8, 24)
    second_date = date(2026, 8, 25)
    first = _build_strict_day(tmp_path / "first", first_date)
    second = _build_strict_day(tmp_path / "second", second_date)
    result = evaluate_observations((
        _observation(first, first_date),
        _observation(second, second_date),
    ))
    assert result.summary["pre_open_replay_success_rate"] == 0.0
    assert result.days[0]["pre_open_reuse_status"] == "NOT_APPLICABLE"
    assert result.days[1]["pre_open_reuse_status"] == "FAIL"
    assert result.summary["failure_case_count"] == 1
    assert result.failure_cases[0]["failure_stage"] == "PRE_OPEN_REUSE"
    assert result.failure_cases[0]["safe_reason_code"] == "PRE_OPEN_REUSE_MISSING"
