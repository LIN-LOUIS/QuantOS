"""Thin CLI session adapter over the canonical Ask service."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from decimal import Decimal
import re
from typing import Any, Callable

from quantos.ask import (AskIntent, AskRequest, AskResponse, AskService, Capability,
                         ToolRegistry)
from quantos.ask.adapters import LocalAskCapabilities
from quantos.collectors import MarketDataError, TavilyWebSearchProvider
from quantos.config import (
    DEFAULT_SETTINGS, MARKET_TIMEZONE, KnowledgeIntegrationSettings, Settings,
)
from quantos.normalizers import MarketNormalizer
from quantos.qa import QAMarketFacts, QAResearchEvidence, sanitize_terminal_text
from quantos.synthesis import DeepSeekLLMClient
from quantos.storage import (
    AskTraceRepository, MarketDataRepository, SecurityMasterRepository,
)


class AskFailure(ValueError):
    """Safe command error code; provider payloads never escape."""


_MARKET_INTENTS = (AskIntent.MARKET_OVERVIEW, AskIntent.ENTITY_MOVE_EXPLANATION,
                   AskIntent.EVIDENCE_LOOKUP, AskIntent.ATTRIBUTION_LOOKUP)
_SYMBOL = re.compile(r"^\d{6}\.(SH|SZ|BJ)$")


class AskSession:
    def __init__(self, symbol: str, *, as_of_time: datetime | None, market: Any,
                 research: Any, llm: Any, live_cutoff: bool = False,
                 no_research: bool = False,
                 local_capabilities: LocalAskCapabilities | None = None,
                 trace_repository: AskTraceRepository | None = None,
                 security_repository: SecurityMasterRepository | None = None,
                 market_repository: MarketDataRepository | Callable[[], MarketDataRepository] | None = None,
                 clock: Callable[[], datetime] | None = None) -> None:
        if not _SYMBOL.fullmatch(symbol):
            raise AskFailure("INVALID_SYMBOL")
        if as_of_time is not None and as_of_time.utcoffset() is None:
            raise AskFailure("TIMEZONE_REQUIRED")
        self.symbol, self.as_of_time = symbol, as_of_time
        self.market, self.research, self.llm = market, research, llm
        self.no_research = no_research
        self.local_capabilities = local_capabilities
        self.trace_repository = trace_repository
        self.security_repository = security_repository
        self.market_repository = market_repository
        self._clock = clock or (lambda: datetime.now(MARKET_TIMEZONE))
        self._identities = None
        self._market_fact = None
        self._research_evidence = None

    def _securities(self, as_of_time):
        if self._identities is None:
            if self.security_repository is not None:
                self._identities = self.security_repository.list_as_of(
                    as_of_time or self._clock()
                )
            elif not hasattr(self.market, "fetch_listed_securities"):
                return ()
            else:
                self._identities = tuple(self.market.fetch_listed_securities(
                    as_of_time=as_of_time or self._clock()))
        return self._identities

    def _market_snapshot(self, args):
        if self._market_fact is not None:
            return self._market_fact
        as_of_time, symbol = args["as_of_time"], args["symbol"]
        local = as_of_time.astimezone(MARKET_TIMEZONE)
        last_day = local.date() if local.time() >= time(18) else local.date() - timedelta(days=1)
        if self.market_repository is not None:
            repository = (self.market_repository()
                          if callable(self.market_repository) else self.market_repository)
            bars = repository.load_market_bar_history(
                symbol, end_date=last_day, lookback=1, as_of_time=as_of_time,
            )
        else:
            start = datetime.combine(last_day - timedelta(days=14), time.min, MARKET_TIMEZONE)
            end = datetime.combine(last_day, time(23, 59, 59), MARKET_TIMEZONE)
            raw = self.market.fetch_bars(symbols=[symbol], frequency="1d",
                                         start_time=start, end_time=end, as_of_time=as_of_time)
            bars = [MarketNormalizer().normalize(item) for item in raw]
        bars = [item for item in bars if item.symbol == symbol
                and item.is_available_as_of(as_of_time)]
        if not bars:
            raise AskFailure("DATA_UNAVAILABLE")
        latest = max(bars, key=lambda item: item.timestamp)
        previous = latest.prev_close
        change_pct = ((latest.close / previous - Decimal(1)) * 100) if previous else None
        self._market_fact = QAMarketFacts(
            "M1", latest.timestamp.date().isoformat(), str(latest.close),
            str(previous) if previous else None,
            str(change_pct.quantize(Decimal("0.01"))) if change_pct is not None else None,
            latest.volume, str(latest.amount), latest.source,
            latest.source_record_id, latest.available_at)
        return self._market_fact

    def _evidence_retrieve(self, args):
        if self._research_evidence is not None:
            return self._research_evidence
        as_of_time, symbol = args["as_of_time"], args["symbol"]
        found = self.research.search(query=f"{symbol} 股票 公告 新闻", query_symbol=symbol,
                                     start_time=as_of_time - timedelta(days=7),
                                     end_time=as_of_time, max_results=5,
                                     pit_mode="source_timestamp_proxy")
        visible = [item for item in found if item.query_symbol == symbol
                   and item.available_at <= as_of_time and not item.strict_pit]
        self._research_evidence = tuple(QAResearchEvidence(
            f"E{index}", item.title, item.snippet, item.url,
            item.published_at, item.provider, False)
            for index, item in enumerate(visible, 1))
        return self._research_evidence

    def ask(self, question: str) -> AskResponse:
        capabilities = [
            Capability("market.snapshot", (("symbol", str), ("as_of_time", datetime)), _MARKET_INTENTS,
                       self._market_snapshot, QAMarketFacts),
            Capability("evidence.retrieve", (("symbol", str), ("as_of_time", datetime)), _MARKET_INTENTS,
                       self._evidence_retrieve, tuple),
        ]
        if self.local_capabilities is not None:
            capabilities.extend(self.local_capabilities.capabilities())
        registry = ToolRegistry(tuple(capabilities))
        try:
            request = AskRequest(question, self.as_of_time,
                                 entity_hints=(self.symbol,), no_research=self.no_research)
        except ValueError as error:
            raise AskFailure(str(error)) from None
        result = AskService(securities=self._securities, registry=registry,
                            llm=self.llm, trace_repository=self.trace_repository,
                            clock=self._clock).ask(request)
        self.as_of_time = result.as_of_time
        return result


def make_session(symbol: str, as_of_time: datetime | None, *, live_cutoff: bool = False,
                 no_research: bool = False, offline: bool = False,
                 settings: Settings | None = None,
                 clock: Callable[[], datetime] | None = None) -> AskSession:
    settings = settings or DEFAULT_SETTINGS
    security_repository = SecurityMasterRepository(settings)
    if offline:
        research, llm = _UnavailableResearch(), None
    else:
        try:
            research = TavilyWebSearchProvider()
        except MarketDataError:
            research = _UnavailableResearch()
        llm = DeepSeekLLMClient()
    local = LocalAskCapabilities(
        settings=settings,
        knowledge_settings=KnowledgeIntegrationSettings.from_env(),
    )
    return AskSession(symbol, as_of_time=as_of_time, market=None,
                      research=research, llm=llm,
                      live_cutoff=live_cutoff, no_research=no_research,
                      local_capabilities=local,
                      trace_repository=AskTraceRepository(settings=settings),
                      security_repository=security_repository,
                      market_repository=lambda: MarketDataRepository(
                          settings, read_only=offline,
                      ),
                      clock=clock)


class _UnavailableResearch:
    def search(self, **_kwargs):
        raise AskFailure("TOOL_UNAVAILABLE")


def render(result: AskResponse | dict[str, Any]) -> str:
    safe = sanitize_terminal_text
    value = result.to_dict() if isinstance(result, AskResponse) else result
    market = value.get("market")
    if market is None:
        return f"QuantOS Ask  {safe(value.get('status'))}\n回答  {safe(value.get('answer'))}"
    lines = [f"QuantOS Ask  {safe(value['symbol'])}",
             f"市场事实  {safe(market['trade_date'])} 收盘 {safe(market['close'])}  涨跌幅 {safe(market.get('change_pct') or '未知')}%",
             f"回答  {safe(value['answer'])}", safe(value["pit_warning"])]
    if value["sources"]:
        lines.append("Current Research Evidence (non-strict PIT)")
    for source in value["sources"]:
        lines.append(f"[{safe(source['ref'])}] {safe(source['title'])}  {safe(source['url'])} (strict_pit=false)")
    return "\n".join(lines)
