"""Deterministic Ask planning and bounded execution over registered capabilities."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from typing import Callable, Sequence
from uuid import uuid4

from quantos.config import MARKET_TIMEZONE
from quantos.qa import (
    QAContext, QAMarketFacts, QAResearchEvidence, QA_SCHEMA, grounding_payload,
    grounding_prompt, validate_answer,
)
from quantos.schemas import SecurityMaster

from .capabilities import AttributionLookupResult
from .contracts import (
    AskIntent, AskRequest, AskResponse, AskTrace, QueryPlan, QueryStep,
    ResolvedEntity, ToolInvocation, _identity,
)
from .intents import resolve_intent
from .registry import CapabilityAvailability, ToolRegistry, ToolRejected


_ENTITY_INTENTS = frozenset({
    AskIntent.MARKET_OVERVIEW, AskIntent.ENTITY_MOVE_EXPLANATION,
    AskIntent.EVIDENCE_LOOKUP, AskIntent.ATTRIBUTION_LOOKUP,
    AskIntent.KNOWLEDGE_LOOKUP, AskIntent.REPORT_LOOKUP,
    AskIntent.HISTORICAL_COMPARE,
})
_MARKET_INTENTS = frozenset({
    AskIntent.MARKET_OVERVIEW, AskIntent.ENTITY_MOVE_EXPLANATION,
    AskIntent.EVIDENCE_LOOKUP, AskIntent.ATTRIBUTION_LOOKUP,
})
_RESEARCH_INTENTS = frozenset({
    AskIntent.MARKET_OVERVIEW, AskIntent.ENTITY_MOVE_EXPLANATION,
    AskIntent.EVIDENCE_LOOKUP, AskIntent.ATTRIBUTION_LOOKUP,
})
_ATTRIBUTION_INTENTS = frozenset({
    AskIntent.ENTITY_MOVE_EXPLANATION, AskIntent.ATTRIBUTION_LOOKUP,
})


def _operational_identity_visible(item: SecurityMaster, as_of: datetime) -> bool:
    return (
        item.available_at <= as_of
        and item.effective_from <= as_of.date()
        and (item.effective_to is None or item.effective_to >= as_of.date())
        and item.is_active
    )


class AskService:
    def __init__(self, *, securities: Callable[[datetime | None], Sequence[SecurityMaster]],
                 registry: ToolRegistry | None = None, llm=None,
                 trace_repository=None,
                 clock: Callable[[], datetime] | None = None,
                 identity_visibility: Callable[[SecurityMaster, datetime], bool] | None = None,
                 ) -> None:
        self._securities = securities
        self._registry = registry or ToolRegistry()
        self._llm = llm
        self._trace_repository = trace_repository
        self._clock = clock or (lambda: datetime.now(MARKET_TIMEZONE))
        self._identity_visibility = identity_visibility or _operational_identity_visible

    def plan(self, request: AskRequest) -> QueryPlan:
        intent = resolve_intent(request.question)
        raw = request.entity_hints[0] if request.entity_hints else ""
        sampled_time = request.as_of_time or self._clock()
        if sampled_time.utcoffset() is None:
            raise ValueError("TIMEZONE_REQUIRED")
        as_of = sampled_time.astimezone(MARKET_TIMEZONE)
        identities = ()
        identity_unavailable = False
        if intent in _ENTITY_INTENTS:
            try:
                identities = tuple(self._securities(as_of))
            except Exception:
                identity_unavailable = True
        entities: tuple[ResolvedEntity, ...] = ()
        reason = None
        if intent is AskIntent.UNSUPPORTED:
            reason = "UNSUPPORTED_INTENT"
        elif intent in _ENTITY_INTENTS:
            matches = [
                item for item in identities
                if self._identity_visibility(item, as_of)
                and raw in (item.symbol, item.company_name, *item.aliases)
            ]
            symbols = {item.symbol for item in matches}
            if identity_unavailable:
                reason = "IDENTITY_DATA_UNAVAILABLE"
                entities = (ResolvedEntity(raw, "", "security_master", "UNAVAILABLE"),)
            elif len(symbols) > 1:
                reason = "AMBIGUOUS_ENTITY"
                entities = (ResolvedEntity(raw, "", "security_master", "AMBIGUOUS"),)
            elif not matches:
                reason = "ENTITY_NOT_FOUND"
                entities = (ResolvedEntity(raw, "", "security_master", "NOT_FOUND"),)
            else:
                chosen = max(matches, key=lambda item: item.available_at)
                entities = (ResolvedEntity(raw, chosen.symbol, chosen.source),)
        if intent is AskIntent.HISTORICAL_COMPARE:
            reason = reason or "HISTORICAL_DATA_UNAVAILABLE"

        desired = self._desired_capabilities(intent, request.no_research)
        availability = tuple(sorted(
            (name, self._registry.availability(name).value) for name in desired
        ))
        if reason is None and desired and self._registry.availability(desired[0]) in {
            CapabilityAvailability.UNAVAILABLE, CapabilityAvailability.DISABLED,
        }:
            reason = "TOOL_UNAVAILABLE"

        request_id = _identity({"request": request.to_dict(), "as_of_time": as_of})
        steps: tuple[QueryStep, ...] = ()
        if reason is None:
            symbol = entities[0].symbol if entities else ""
            steps = tuple(self._step(name, request.question, symbol, as_of) for name in desired)
        return QueryPlan(
            request_id, intent, request.question, entities, as_of, "latest_visible",
            steps, "DENY" if reason else "ALLOW", reason, availability,
        )

    def ask(self, request: AskRequest) -> AskResponse:
        started = time.monotonic()
        plan = self.plan(request)
        created_at = plan.as_of_time if request.as_of_time is None else self._clock()
        invocations: list[ToolInvocation] = []
        limitations: list[str] = []
        reasons: list[str] = []
        facts: list[dict] = []
        evidence_refs: list[str] = []
        knowledge_refs: list[str] = []
        report_refs: list[str] = []
        answer = ""
        status = "PASS"
        market: QAMarketFacts | None = None
        evidence: tuple[QAResearchEvidence, ...] = ()
        attribution: AttributionLookupResult | None = None
        run_id: str | None = None
        source_refs: tuple[str, ...] = ()
        llm_requests = 0

        if plan.unsupported_reason:
            reasons.append(plan.unsupported_reason)
            status = ("UNSUPPORTED" if plan.unsupported_reason in {
                "UNSUPPORTED_INTENT", "ENTITY_NOT_FOUND", "AMBIGUOUS_ENTITY",
                "HISTORICAL_DATA_UNAVAILABLE",
            } else "FAIL")
            if plan.unsupported_reason == "IDENTITY_DATA_UNAVAILABLE":
                answer = ("Security Master has not been initialized. "
                          "Run quantos data bootstrap security-master.")
            else:
                answer = ("当前 Ask 能力无法安全回答该问题。" if status == "UNSUPPORTED"
                          else "所需 QuantOS capability 当前不可用。")
        else:
            for step in plan.steps:
                availability = self._registry.availability(step.capability)
                if availability in {CapabilityAvailability.UNAVAILABLE,
                                    CapabilityAvailability.DISABLED}:
                    invocations.append(ToolInvocation(
                        step.capability, "SKIP", 0, "TOOL_UNAVAILABLE",
                    ))
                    reasons.append("TOOL_UNAVAILABLE")
                    limitations.append(f"{step.capability} unavailable")
                    continue
                try:
                    result = self._registry.execute(
                        step.capability, plan.intent, self._arguments(step, plan),
                    )
                    count, accepted = self._accept_result(
                        step.capability, result, plan, facts, evidence_refs,
                        knowledge_refs, report_refs, limitations,
                    )
                    if step.capability == "market.snapshot":
                        market = result
                    elif step.capability == "evidence.retrieve":
                        evidence = accepted
                    elif step.capability == "attribution.lookup":
                        attribution = result
                    elif step.capability == "report.lookup":
                        run_id = result.run_id
                    invocations.append(ToolInvocation(step.capability, "PASS", count))
                except ToolRejected as error:
                    code = str(error) if str(error) in {
                        "DATA_UNAVAILABLE", "TOOL_UNAVAILABLE", "POLICY_DENIED",
                    } else "TOOL_FAILED"
                    invocations.append(ToolInvocation(step.capability, "FAIL", 0, code))
                    reasons.append(code)
                    if step.capability == "market.snapshot":
                        break
                    limitations.append(f"{step.capability} unavailable")
                except Exception:
                    code = "DATA_UNAVAILABLE" if step.capability == "market.snapshot" else "TOOL_FAILED"
                    invocations.append(ToolInvocation(step.capability, "FAIL", 0, code))
                    reasons.append(code)
                    if step.capability == "market.snapshot":
                        break
                    limitations.append(f"{step.capability} unavailable")

            if plan.intent in _MARKET_INTENTS:
                if market is None:
                    status, answer = "FAIL", "市场事实不可用。"
                else:
                    answer = (
                        f"{plan.entities[0].symbol} 在 {market.trade_date} 收盘价为 "
                        f"{market.close}，涨跌幅为 {market.change_pct or '未知'}%。"
                    )
                    source_refs = ("M1",)
                    if plan.intent in _ATTRIBUTION_INTENTS:
                        if attribution is not None and attribution.causal_allowed:
                            answer += " 既有 Attribution Policy 允许使用已验证归因结果。"
                            report_refs.extend(attribution.report_refs)
                        else:
                            reasons.append("ATTRIBUTION_INSUFFICIENT")
                            limitations.append("当前证据不足，不能确定价格变动的原因。")
                            answer += " 当前没有足够有效的 Attribution Evidence 支持因果归因。"
                            status = "PARTIAL"
                            if attribution is not None:
                                report_refs.extend(attribution.report_refs)
                                if attribution.limitation:
                                    limitations.append(attribution.limitation)
                    if any(code in reasons for code in (
                        "TOOL_UNAVAILABLE", "TOOL_FAILED", "POLICY_DENIED",
                    )):
                        status = "PARTIAL"
                    elif plan.intent not in _ATTRIBUTION_INTENTS and self._llm is not None:
                        context = QAContext(plan.entities[0].symbol, plan.as_of_time, market, evidence)
                        try:
                            llm_requests += 1
                            generation = self._llm.generate_structured(
                                system_prompt=grounding_prompt(has_research=bool(evidence)),
                                input_payload=json.dumps(
                                    grounding_payload(context, request.question), ensure_ascii=False,
                                ), output_schema=QA_SCHEMA,
                            )
                            validated = validate_answer(generation.payload, context, request.question)
                            answer = validated.answer
                            source_refs = validated.source_refs
                            evidence_refs[:] = [ref for ref in validated.source_refs if ref != "M1"]
                        except Exception:
                            status = "PARTIAL"
                            reasons.append("SYNTHESIS_FAILED")
                            limitations.append("Structured synthesis unavailable")
            elif plan.intent is AskIntent.KNOWLEDGE_LOOKUP:
                status, answer = self._bounded_answer("知识", knowledge_refs, reasons)
            elif plan.intent is AskIntent.REPORT_LOOKUP:
                status, answer = self._bounded_answer("报告", report_refs, reasons)
            elif plan.intent is AskIntent.SYSTEM_STATUS:
                status = "PASS" if facts else "PARTIAL"
                names = ", ".join(str(item.get("capability", "unknown")) for item in facts)
                answer = f"QuantOS 能力状态：{names or '无可用状态'}。"
                if not facts:
                    reasons.append("TOOL_UNAVAILABLE")
            elif plan.intent is AskIntent.SECTOR_PERFORMANCE:
                status, answer = self._bounded_answer("板块", report_refs, reasons)

        reasons = _unique(reasons) or ["OK"]
        limitations = _unique(limitations)
        evidence_refs = _unique(evidence_refs)
        knowledge_refs = _unique(knowledge_refs)
        report_refs = _unique(report_refs)
        duration_ms = (time.monotonic() - started) * 1000
        trace_id = uuid4().hex
        trace = AskTrace(
            trace_id, plan.request_id, created_at,
            created_at + timedelta(milliseconds=duration_ms), plan.intent,
            request.raw_question, plan.normalized_question, plan.entities,
            plan.as_of_time, plan, tuple(invocations), tuple(evidence_refs),
            tuple(knowledge_refs), tuple(report_refs), tuple(reasons),
            tuple(limitations), tuple(code for code in reasons if code not in {
                "OK", "ATTRIBUTION_INSUFFICIENT", "NO_EVIDENCE",
            }), duration_ms, run_id,
        )
        if self._trace_repository is not None:
            self._trace_repository.save(trace)
        return AskResponse(
            plan.request_id, trace_id, status, answer, plan.intent, plan.entities,
            plan.as_of_time, tuple(facts), tuple(evidence_refs), tuple(knowledge_refs),
            tuple(report_refs), tuple(limitations), tuple(reasons), trace,
            tuple(item.payload() for item in evidence), source_refs,
            "market_only" if request.no_research or any(code in reasons for code in (
                "TOOL_UNAVAILABLE", "TOOL_FAILED", "POLICY_DENIED",
            )) else "grounded_qa",
            "disabled" if request.no_research else
            "unavailable" if any(code in reasons for code in (
                "TOOL_UNAVAILABLE", "TOOL_FAILED", "POLICY_DENIED",
            )) else "available",
            sum(item.capability == "market.snapshot" and item.status == "PASS"
                for item in invocations),
            sum(item.capability == "evidence.retrieve" and item.status == "PASS"
                for item in invocations), llm_requests,
        )

    def _desired_capabilities(self, intent: AskIntent, no_research: bool) -> tuple[str, ...]:
        if intent in _MARKET_INTENTS:
            names = ["market.snapshot"]
            if intent in _RESEARCH_INTENTS and not no_research:
                names.append("evidence.retrieve")
            if intent in _ATTRIBUTION_INTENTS and not no_research:
                names.append("attribution.lookup")
            return tuple(names)
        return {
            AskIntent.SECTOR_PERFORMANCE: ("sector.performance",),
            AskIntent.KNOWLEDGE_LOOKUP: ("knowledge.query",),
            AskIntent.REPORT_LOOKUP: ("report.lookup",),
            AskIntent.SYSTEM_STATUS: ("system.status",),
        }.get(intent, ())

    @staticmethod
    def _step(name: str, question: str, symbol: str, as_of: datetime) -> QueryStep:
        args: dict[str, str] = {"as_of_time": as_of.isoformat()}
        if name in {"knowledge.query", "report.lookup", "sector.performance"}:
            args["query"] = question
        if name not in {"system.status", "sector.performance"}:
            args["symbol"] = symbol
        return QueryStep(name, tuple(sorted(args.items())))

    @staticmethod
    def _arguments(step: QueryStep, plan: QueryPlan) -> dict[str, object]:
        return {
            key: plan.as_of_time if key == "as_of_time" else value
            for key, value in step.args
        }

    @staticmethod
    def _accept_result(capability, result, plan, facts, evidence_refs,
                       knowledge_refs, report_refs, limitations):
        if capability == "market.snapshot":
            if not isinstance(result, QAMarketFacts) or result.available_at > plan.as_of_time:
                raise ToolRejected("DATA_UNAVAILABLE")
            facts.append(result.payload())
            return 1, result
        if capability == "evidence.retrieve":
            if not isinstance(result, tuple) or any(
                not isinstance(item, QAResearchEvidence) for item in result
            ):
                raise ToolRejected("INVALID_TOOL_OUTPUT")
            visible = tuple(
                item for item in result
                if item.published_at is None or item.published_at <= plan.as_of_time
            )
            evidence_refs.extend(item.ref for item in visible)
            if any(not item.strict_pit for item in visible):
                limitations.append("NON_STRICT_PIT_RESEARCH")
            return len(visible), visible
        if capability == "attribution.lookup":
            if result.limitation:
                limitations.append(result.limitation)
            return len(result.evidence_refs), result
        if capability == "knowledge.query":
            knowledge_refs.extend(result.refs)
            facts.extend(result.facts)
            return len(result.refs), result
        if capability in {"report.lookup", "sector.performance"}:
            report_refs.extend(result.refs)
            facts.extend(result.facts)
            return len(result.refs), result
        if capability == "system.status":
            facts.extend(result.facts)
            return len(result.facts), result
        raise ToolRejected("POLICY_DENIED")

    @staticmethod
    def _bounded_answer(label: str, refs: list[str], reasons: list[str]):
        if refs:
            return "PASS", f"已检索到{label}引用：{', '.join(refs)}。"
        reasons.append("NO_EVIDENCE")
        return "PARTIAL", f"当前没有可用的{label}结果。"


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))
