"""Phase 3D.1 deterministic schedule semantics; every test is offline."""

from dataclasses import replace
from dataclasses import fields
from datetime import date, datetime, time, timedelta, timezone
import inspect
import socket

import pytest

from quantos.config import MARKET_TIMEZONE
from quantos.schemas.run import RunType
from quantos.schemas.schedule import (
    ScheduleDecision, ScheduleDecisionStatus as S, ScheduleEvaluation,
    SchedulePolicy, ScheduleSlot,
)
from quantos.scheduling import (
    DEFAULT_SCHEDULE_POLICY, LocalTradingCalendar, evaluate_schedule,
    run_context_from_intent,
)

TARGET = date(2026, 9, 1)
MONDAY = date(2026, 8, 31)
FRIDAY = date(2026, 8, 28)
HOLIDAY_FIXTURE = date(2026, 9, 3)
SATURDAY = date(2026, 9, 5)
CALENDAR = LocalTradingCalendar((FRIDAY, MONDAY, TARGET, date(2026, 9, 2), date(2026, 9, 4)))


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("scheduling must not access network")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def at(hour, minute=0, second=0, *, day=TARGET, tz=MARKET_TIMEZONE):
    return datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=tz)


def decisions(value, *, calendar=CALENDAR, policy=DEFAULT_SCHEDULE_POLICY):
    return {item.slot_id: item for item in evaluate_schedule(
        evaluated_at=value, calendar=calendar, policy=policy,
    ).decisions}


def due(value, slot_id, *, calendar=CALENDAR):
    item = decisions(value, calendar=calendar)[slot_id]
    assert item.status == S.DUE and item.intent is not None
    return item.intent


def one_slot(slot):
    return SchedulePolicy((slot,), timezone=slot.timezone)


def test_default_four_slots_exist_in_fixed_order():
    assert tuple(slot.slot_id for slot in DEFAULT_SCHEDULE_POLICY.slots) == (
        "PRE_OPEN_0900", "INTRADAY_1030", "POST_CLOSE_1600", "POST_CLOSE_2000",
    )


def test_schedule_slot_canonical_fields():
    assert tuple(field.name for field in fields(ScheduleSlot)) == (
        "slot_id", "run_type", "local_time", "timezone", "grace_period", "enabled",
    )


def test_default_policy_uses_shanghai_and_fifteen_minute_grace():
    assert DEFAULT_SCHEDULE_POLICY.timezone == "Asia/Shanghai"
    assert all(slot.timezone == "Asia/Shanghai" and slot.grace_period == timedelta(minutes=15)
               for slot in DEFAULT_SCHEDULE_POLICY.slots)


def test_schedule_decision_states_are_closed():
    assert tuple(status.value for status in S) == (
        "DUE", "NOT_DUE", "MISSED_WINDOW", "NON_TRADING_DAY",
    )


@pytest.mark.parametrize("slot_id,expected", [
    ("PRE_OPEN_0900", RunType.PRE_OPEN),
    ("INTRADAY_1030", RunType.INTRADAY),
    ("POST_CLOSE_1600", RunType.POST_CLOSE),
    ("POST_CLOSE_2000", RunType.POST_CLOSE),
])
def test_default_slot_run_type_mapping(slot_id, expected):
    assert next(x for x in DEFAULT_SCHEDULE_POLICY.slots if x.slot_id == slot_id).run_type == expected


def test_post_close_slots_share_run_type_but_not_identity():
    late = DEFAULT_SCHEDULE_POLICY.slots[2:]
    assert {x.run_type for x in late} == {RunType.POST_CLOSE}
    assert len({x.slot_id for x in late}) == 2


def test_default_slot_ids_are_unique():
    values = [slot.slot_id for slot in DEFAULT_SCHEDULE_POLICY.slots]
    assert len(values) == len(set(values))


@pytest.mark.parametrize("offset,status", [
    (-1, S.NOT_DUE),
    (0, S.DUE),
    (60, S.DUE),
    (14 * 60 + 59, S.DUE),
    (15 * 60, S.MISSED_WINDOW),
    (16 * 60, S.MISSED_WINDOW),
])
def test_due_window_exact_half_open_boundaries(offset, status):
    value = at(9) + timedelta(seconds=offset)
    decision = decisions(value)["PRE_OPEN_0900"]
    assert decision.status == status
    assert (decision.intent is not None) == (status == S.DUE)


def test_future_slots_are_not_due():
    state = decisions(at(9, 20))
    assert state["PRE_OPEN_0900"].status == S.MISSED_WINDOW
    assert all(state[key].status == S.NOT_DUE for key in (
        "INTRADAY_1030", "POST_CLOSE_1600", "POST_CLOSE_2000",
    ))


def test_earlier_slot_is_only_classified_missed_not_recovered():
    state = decisions(at(10, 30))
    assert state["PRE_OPEN_0900"].status == S.MISSED_WINDOW
    assert state["INTRADAY_1030"].status == S.DUE
    assert len(evaluate_schedule(evaluated_at=at(10, 30), calendar=CALENDAR).intents) == 1


def test_normal_trading_day_uses_calendar_fact():
    state = decisions(at(9))
    assert state["PRE_OPEN_0900"].status == S.DUE
    assert all(item.status != S.NON_TRADING_DAY for item in state.values())


@pytest.mark.parametrize("closed_day", [SATURDAY, HOLIDAY_FIXTURE])
def test_non_trading_day_all_slots_have_no_intent(closed_day):
    evaluation = evaluate_schedule(evaluated_at=at(9, day=closed_day), calendar=CALENDAR)
    assert all(item.status == S.NON_TRADING_DAY and item.intent is None for item in evaluation.decisions)
    assert evaluation.intents == ()


def test_monday_pre_open_basis_is_friday_not_calendar_minus_one():
    intent = due(at(9, day=MONDAY), "PRE_OPEN_0900")
    assert intent.market_basis_trade_date == FRIDAY


def test_consecutive_trading_day_basis_is_previous_trading_day():
    assert due(at(10, 30), "INTRADAY_1030").market_basis_trade_date == MONDAY


def test_post_close_basis_is_target_trade_date():
    assert due(at(16), "POST_CLOSE_1600").market_basis_trade_date == TARGET


def test_scheduled_intent_canonical_fields():
    intent = due(at(9), "PRE_OPEN_0900")
    assert tuple(field.name for field in fields(type(intent))) == (
        "slot_id", "run_type", "scheduled_for", "evaluated_at", "target_trade_date",
        "market_basis_trade_date", "timezone",
    )


def test_utc_input_is_normalized_to_shanghai():
    utc_input = datetime(2026, 9, 1, 1, 4, tzinfo=timezone.utc)
    evaluation = evaluate_schedule(evaluated_at=utc_input, calendar=CALENDAR)
    item = next(x for x in evaluation.decisions if x.slot_id == "PRE_OPEN_0900")
    assert evaluation.evaluated_at == at(9, 4)
    assert item.status == S.DUE and item.intent.evaluated_at == at(9, 4)


def test_naive_datetime_is_rejected():
    with pytest.raises(ValueError, match="timezone-aware"):
        evaluate_schedule(evaluated_at=datetime(2026, 9, 1, 9), calendar=CALENDAR)


def test_machine_timezone_environment_does_not_change_result(monkeypatch):
    baseline = evaluate_schedule(evaluated_at=at(9, 4), calendar=CALENDAR)
    monkeypatch.setenv("TZ", "America/New_York")
    assert evaluate_schedule(evaluated_at=at(9, 4), calendar=CALENDAR) == baseline


def test_duplicate_slot_id_rejected():
    slot = DEFAULT_SCHEDULE_POLICY.slots[0]
    with pytest.raises(ValueError, match="duplicate"):
        SchedulePolicy((slot, slot))


@pytest.mark.parametrize("grace", [timedelta(0), timedelta(seconds=-1)])
def test_non_positive_grace_rejected(grace):
    with pytest.raises(ValueError, match="positive"):
        replace(DEFAULT_SCHEDULE_POLICY.slots[0], grace_period=grace)


def test_pre_open_window_crossing_open_is_rejected():
    slot = ScheduleSlot("BAD_PRE", RunType.PRE_OPEN, time(9, 25), "Asia/Shanghai", timedelta(minutes=15))
    with pytest.raises(ValueError, match="market open"):
        one_slot(slot)


@pytest.mark.parametrize("value", [time(14, 59), time(15)])
def test_post_close_slot_not_strictly_after_close_rejected(value):
    slot = ScheduleSlot("BAD_POST", RunType.POST_CLOSE, value, "Asia/Shanghai", timedelta(minutes=1))
    with pytest.raises(ValueError, match="after market close"):
        one_slot(slot)


def test_overlapping_due_windows_rejected_even_with_distinct_ids():
    first = ScheduleSlot("ONE", RunType.POST_CLOSE, time(16), "Asia/Shanghai", timedelta(minutes=15))
    second = ScheduleSlot("TWO", RunType.POST_CLOSE, time(16, 10), "Asia/Shanghai", timedelta(minutes=15))
    with pytest.raises(ValueError, match="overlap"):
        SchedulePolicy((first, second))


def test_identical_due_windows_fail_with_policy_error_not_sorting_error():
    first = ScheduleSlot("ONE", RunType.POST_CLOSE, time(16), "Asia/Shanghai", timedelta(minutes=15))
    second = ScheduleSlot("TWO", RunType.POST_CLOSE, time(16), "Asia/Shanghai", timedelta(minutes=15))
    with pytest.raises(ValueError, match="overlap"):
        SchedulePolicy((first, second))


def test_invalid_timezone_rejected_at_construction():
    with pytest.raises(ValueError, match="unknown timezone"):
        ScheduleSlot("BAD_TZ", RunType.PRE_OPEN, time(9), "Invalid/Zone", timedelta(minutes=1))


def test_slot_timezone_must_match_policy_timezone():
    slot = ScheduleSlot("UTC_POST", RunType.POST_CLOSE, time(16), "UTC", timedelta(minutes=1))
    with pytest.raises(ValueError, match="policy timezone"):
        SchedulePolicy((slot,), timezone="Asia/Shanghai")


@pytest.mark.parametrize("value,grace", [
    (time(9), timedelta(minutes=15)),
    (time(11, 25), timedelta(minutes=15)),
    (time(12), timedelta(minutes=15)),
    (time(14, 50), timedelta(minutes=15)),
])
def test_intraday_window_must_stay_in_one_market_session(value, grace):
    slot = ScheduleSlot("BAD_INTRA", RunType.INTRADAY, value, "Asia/Shanghai", grace)
    with pytest.raises(ValueError, match="market session"):
        one_slot(slot)


def test_slot_local_time_with_tzinfo_is_rejected():
    with pytest.raises(ValueError, match="wall-clock"):
        ScheduleSlot("AWARE_TIME", RunType.PRE_OPEN, time(9, tzinfo=MARKET_TIMEZONE),
                     "Asia/Shanghai", timedelta(minutes=1))


def test_due_window_cannot_cross_local_midnight():
    slot = ScheduleSlot("LATE", RunType.POST_CLOSE, time(23, 55), "Asia/Shanghai", timedelta(minutes=15))
    with pytest.raises(ValueError, match="midnight"):
        one_slot(slot)


@pytest.mark.parametrize("name", [
    "collect_readiness", "build_run_plan", "execute_run", "QuantOSRunManifest",
    "ReportType", "FundFlowEvidence", "EvidenceSynthesis", "TimeSliceIntelligenceReport",
])
def test_scheduling_namespace_has_no_downstream_responsibility(name):
    import quantos.scheduling as scheduling

    assert name not in vars(scheduling)


def test_evaluation_reads_only_trading_calendar_methods():
    class Spy:
        def __init__(self):
            self.calls = []

        def is_trading_day(self, value):
            self.calls.append(("is_trading_day", value))
            return True

        def previous_trading_day(self, value):
            self.calls.append(("previous_trading_day", value))
            return MONDAY

    calendar = Spy()
    evaluate_schedule(evaluated_at=at(9), calendar=calendar)
    assert {name for name, _ in calendar.calls} == {"is_trading_day", "previous_trading_day"}
    assert calendar.calls.count(("is_trading_day", TARGET)) == 4
    assert calendar.calls.count(("previous_trading_day", TARGET)) == 1


def test_scheduling_does_not_expose_report_type_or_product_decision():
    item = decisions(at(9))["PRE_OPEN_0900"]
    assert not hasattr(item, "report_type") and not hasattr(item.intent, "report_type")


def test_evaluation_has_no_manifest_or_execution_result():
    result = evaluate_schedule(evaluated_at=at(9), calendar=CALENDAR)
    assert not hasattr(result, "manifest") and not hasattr(result, "execution_results")


def test_post_close_due_has_no_fund_flow_readiness_semantics():
    item = decisions(at(16))["POST_CLOSE_1600"]
    assert item.status == S.DUE
    assert not hasattr(item, "fund_flow_ready") and not hasattr(item.intent, "fund_flow_ready")


def test_evaluation_writes_no_files(tmp_path):
    before = tuple(tmp_path.iterdir())
    evaluate_schedule(evaluated_at=at(9), calendar=CALENDAR)
    assert tuple(tmp_path.iterdir()) == before == ()


def test_module_has_no_readiness_provider_or_storage_imports():
    import quantos.scheduling as scheduling

    source = inspect.getsource(scheduling)
    for forbidden in ("quantos.orchestration", "quantos.storage", "quantos.collectors",
                      "ReadinessSnapshot", "build_run_plan", "execute_run"):
        assert forbidden not in source


@pytest.mark.parametrize("slot_id,value,expected_type", [
    ("PRE_OPEN_0900", at(9, 4), RunType.PRE_OPEN),
    ("INTRADAY_1030", at(10, 34), RunType.INTRADAY),
    ("POST_CLOSE_1600", at(16, 4), RunType.POST_CLOSE),
])
def test_due_intent_builds_run_context_at_boundary(slot_id, value, expected_type):
    intent = due(value, slot_id)
    context = run_context_from_intent(intent, mode="strict_live", universe_name="shse_szse_a_share",
                                      top_n=20, llm_allowed=False)
    assert context.run_type == expected_type
    assert context.target_trade_date == TARGET
    assert context.market_basis_trade_date == intent.market_basis_trade_date
    assert context.as_of_time == value
    assert context.mode == "strict_live" and context.llm_allowed is False


@pytest.mark.parametrize("mode", ["research", "strict_live"])
def test_mode_is_explicit_caller_input_not_slot_policy(mode):
    intent = due(at(9), "PRE_OPEN_0900")
    context = run_context_from_intent(intent, mode=mode, universe_name="u", top_n=1, llm_allowed=False)
    assert context.mode == mode
    assert all(not hasattr(slot, "mode") for slot in DEFAULT_SCHEDULE_POLICY.slots)


def test_run_context_as_of_uses_evaluated_not_scheduled_time():
    intent = due(at(9, 4, 31), "PRE_OPEN_0900")
    context = run_context_from_intent(intent, mode="research", universe_name="u", top_n=1, llm_allowed=False)
    assert intent.scheduled_for == at(9)
    assert intent.evaluated_at == context.as_of_time == at(9, 4, 31)


def test_invalid_mode_is_not_selected_or_corrected_by_scheduler():
    intent = due(at(9), "PRE_OPEN_0900")
    with pytest.raises(ValueError, match="mode"):
        run_context_from_intent(intent, mode="", universe_name="u", top_n=1, llm_allowed=False)


def test_same_inputs_produce_identical_evaluation_and_intent():
    first = evaluate_schedule(evaluated_at=at(9, 4), calendar=CALENDAR)
    second = evaluate_schedule(evaluated_at=at(9, 4), calendar=CALENDAR)
    assert first == second and first.intents == second.intents


def test_only_exact_time_boundary_changes_decision():
    before = decisions(at(8, 59, 59))["PRE_OPEN_0900"]
    exact = decisions(at(9))["PRE_OPEN_0900"]
    end = decisions(at(9, 15))["PRE_OPEN_0900"]
    assert (before.status, exact.status, end.status) == (S.NOT_DUE, S.DUE, S.MISSED_WINDOW)


def test_manually_constructed_decision_cannot_disagree_with_window():
    valid = decisions(at(9))["PRE_OPEN_0900"]
    with pytest.raises(ValueError, match="disagrees"):
        replace(valid, status=S.NOT_DUE, intent=None)


def test_manually_constructed_evaluation_rejects_mixed_calendar_status():
    valid = evaluate_schedule(evaluated_at=at(8), calendar=CALENDAR)
    changed = replace(valid.decisions[0], status=S.NON_TRADING_DAY)
    with pytest.raises(ValueError, match="consistent"):
        ScheduleEvaluation(valid.evaluated_at, valid.market_timezone, valid.local_trade_date,
                           (changed, *valid.decisions[1:]))


def test_manually_constructed_evaluation_rejects_decision_from_other_time():
    valid = evaluate_schedule(evaluated_at=at(8), calendar=CALENDAR)
    changed = ScheduleDecision(
        valid.decisions[0].slot_id, valid.decisions[0].run_type, S.NOT_DUE,
        valid.decisions[0].scheduled_for, valid.decisions[0].window_end,
        valid.evaluated_at + timedelta(seconds=1),
    )
    with pytest.raises(ValueError, match="evaluated_at"):
        ScheduleEvaluation(valid.evaluated_at, valid.market_timezone, valid.local_trade_date,
                           (changed, *valid.decisions[1:]))


def test_disabled_slot_is_not_evaluated():
    slots = tuple(replace(slot, enabled=False) if slot.slot_id == "INTRADAY_1030" else slot
                  for slot in DEFAULT_SCHEDULE_POLICY.slots)
    result = evaluate_schedule(evaluated_at=at(10, 30), calendar=CALENDAR, policy=SchedulePolicy(slots))
    assert "INTRADAY_1030" not in {item.slot_id for item in result.decisions}


@pytest.mark.parametrize("values", [
    (),
    (TARGET, TARGET),
    (TARGET, MONDAY),
    (datetime(2026, 9, 1, 0),),
])
def test_local_calendar_rejects_missing_duplicate_unsorted_or_non_date(values):
    with pytest.raises(ValueError):
        LocalTradingCalendar(values)


def test_calendar_factory_normalizes_local_date_facts():
    calendar = LocalTradingCalendar.from_dates([TARGET, MONDAY, TARGET])
    assert calendar.trading_dates == (MONDAY, TARGET)


def test_missing_previous_calendar_coverage_fails_closed_only_when_due():
    calendar = LocalTradingCalendar((TARGET,))
    with pytest.raises(ValueError, match="previous trading day"):
        evaluate_schedule(evaluated_at=at(9), calendar=calendar)
    assert decisions(at(8), calendar=calendar)["PRE_OPEN_0900"].status == S.NOT_DUE


def test_non_trading_day_is_not_remapped_to_next_session():
    evaluation = evaluate_schedule(evaluated_at=at(9, day=SATURDAY), calendar=CALENDAR)
    assert evaluation.local_trade_date == SATURDAY and evaluation.intents == ()


def test_existing_run_and_report_type_enums_remain_unchanged():
    from quantos.schemas.time_slice import ReportType

    assert tuple(x.value for x in RunType) == ("PRE_OPEN", "INTRADAY", "POST_CLOSE")
    assert tuple(x.value for x in ReportType) == (
        "PRE_OPEN_BRIEF", "INTRADAY_BRIEF", "POST_CLOSE_REPORT",
    )


def test_intraday_product_capability_remains_unsupported():
    from quantos.time_slices import INTRADAY_DISCLAIMER

    assert "未接入 canonical 盘中行情数据" in INTRADAY_DISCLAIMER
    assert due(at(10, 30), "INTRADAY_1030").run_type == RunType.INTRADAY


@pytest.mark.parametrize("case,value,slot_id,status,run_type,basis", [
    ("pre_open", at(9), "PRE_OPEN_0900", S.DUE, RunType.PRE_OPEN, MONDAY),
    ("intraday", at(10, 30), "INTRADAY_1030", S.DUE, RunType.INTRADAY, MONDAY),
    ("post_1600", at(16), "POST_CLOSE_1600", S.DUE, RunType.POST_CLOSE, TARGET),
    ("post_2000", at(20), "POST_CLOSE_2000", S.DUE, RunType.POST_CLOSE, TARGET),
    ("non_trading", at(9, day=SATURDAY), "PRE_OPEN_0900", S.NON_TRADING_DAY, RunType.PRE_OPEN, None),
    ("grace_end", at(9, 15), "PRE_OPEN_0900", S.MISSED_WINDOW, RunType.PRE_OPEN, None),
])
def test_offline_schedule_smokes(case, value, slot_id, status, run_type, basis):
    decision = decisions(value)[slot_id]
    assert decision.status == status and decision.run_type == run_type
    if status == S.DUE:
        assert decision.intent.target_trade_date == TARGET
        assert decision.intent.market_basis_trade_date == basis
        context = run_context_from_intent(decision.intent, mode="research", universe_name="u",
                                          top_n=20, llm_allowed=False)
        assert context.as_of_time == value
    else:
        assert decision.intent is None
