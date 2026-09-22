"""Offline acceptance for the bounded Ask command."""

from datetime import date, datetime, timedelta
import json

import pytest

from quantos import cli
from quantos.config import MARKET_TIMEZONE
from quantos.schemas import SecurityMaster, WebSearchResult
from quantos.collectors import RawMarketRecord
from quantos.synthesis import FakeLLMClient


NOW = datetime(2026, 9, 18, 19, tzinfo=MARKET_TIMEZONE)


def bar():
    return RawMarketRecord("tushare", "600519.SH", "daily:1", NOW,
                           {"timestamp": "2026-09-18", "frequency": "1d",
                            "open": "1400", "high": "1450", "low": "1390",
                            "close": "1430", "prev_close": "1400",
                            "volume": "1000", "amount": "1430000",
                            "available_at": NOW.replace(hour=18).isoformat()})


def evidence():
    return WebSearchResult("ref1", "600519 贵州茅台", "600519.SH", "公告标题",
                           "摘要", "https://example.org/item", "example.org",
                           NOW - timedelta(hours=2), "provider_reported", NOW,
                           NOW, "tavily", "item1")


def response(text="可能相关", refs=None):
    refs = ["M1"] if refs is None else refs
    return {"answer": text, "claims": [{"kind": "interpretation", "text": text,
                                           "source_refs": refs}],
            "source_refs": refs, "certainty": "uncertain"}


class Market:
    def __init__(self, records=None, error=None):
        self.records = [bar()] if records is None else records
        self.error = error
        self.calls = []

    def fetch_bars(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.records

    def fetch_listed_securities(self, *, as_of_time):
        return [SecurityMaster("600519.SH", "贵州茅台", "SHSE",
                               date(2001, 8, 27), NOW - timedelta(days=1),
                               source="tushare")]


class Research:
    def __init__(self, records=None, error=None):
        self.records = [evidence()] if records is None else records
        self.error = error
        self.calls = []

    def search(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.records


def test_qa_uses_one_market_and_research_request_with_non_strict_pit():
    from quantos.commands.ask import AskSession
    market, research = Market(), Research()
    llm = FakeLLMClient(response("公开消息可能相关，但不能证明因果。", ["E1"]))
    session = AskSession("600519.SH", as_of_time=NOW, market=market,
                         research=research, llm=llm)
    result = session.ask("最近有哪些公开消息可能相关？")
    assert result["status"] == "PASS"
    assert result["strict_pit"] is False
    assert result["source_refs"] == ["E1"]
    assert "strict_pit=false" in llm.calls[0][0]
    assert len(market.calls) == len(research.calls) == len(llm.calls) == 1
    assert research.calls[0]["pit_mode"] == "source_timestamp_proxy"
    session.ask("能证明上涨就是这些新闻导致的吗？")
    assert len(market.calls) == len(research.calls) == 1
    assert len(llm.calls) == 1


def test_market_failure_suppresses_research_and_llm():
    from quantos.commands.ask import AskSession
    research = Research()
    llm = FakeLLMClient(response())
    result = AskSession("600519.SH", as_of_time=NOW, market=Market(records=[]),
                        research=research, llm=llm).ask("表现？")
    assert result["status"] == "FAIL"
    assert result["reason_codes"] == ["DATA_UNAVAILABLE"]
    assert not research.calls and not llm.calls


def test_invalid_symbol_remains_a_structured_cli_error():
    from quantos.commands.ask import AskSession, AskFailure
    with pytest.raises(AskFailure, match="INVALID_SYMBOL"):
        AskSession("not-a-symbol", as_of_time=NOW, market=Market(),
                   research=Research(), llm=FakeLLMClient(response()))


def test_tavily_failure_degrades_to_market_only_without_llm():
    from quantos.commands.ask import AskSession
    llm = FakeLLMClient(response())
    result = AskSession("600519.SH", as_of_time=NOW, market=Market(),
                        research=Research(error=RuntimeError("secret response")),
                        llm=llm).ask("最近表现？")
    assert result["mode"] == "market_only"
    assert result["llm_requests"] == 0
    assert "secret response" not in str(result)


def test_no_research_uses_llm_with_market_only_and_zero_tavily_requests():
    from quantos.commands.ask import AskSession
    research = Research()
    llm = FakeLLMClient(response("收盘价为 1430。"))
    result = AskSession("600519.SH", as_of_time=NOW, market=Market(),
                        research=research, llm=llm, no_research=True).ask("收盘价？")
    assert result["mode"] == "market_only"
    assert result["research_requests"] == 0
    assert result["llm_requests"] == 1
    assert not research.calls
    assert "No research evidence is available." in llm.calls[0][0]


def test_renderer_neutralizes_terminal_controls():
    from quantos.commands.ask import render
    result = {"symbol": "600519.SH", "market": {"trade_date": "2026-09-18", "close": "1430"},
              "answer": "safe\x1b[31m", "pit_warning": "strict_pit=false",
              "sources": [{"ref": "r", "title": "bad\x1b[2J", "url": "https://example.org"}]}
    assert "\x1b" not in render(result)


def test_live_cutoff_never_advances_after_research_collection():
    from quantos.commands.ask import AskSession
    later = NOW + timedelta(seconds=2)
    record = WebSearchResult("later", "q", "600519.SH", "新公告", "摘要",
                             "https://example.org/new", "example.org", None,
                             "unknown", later, later, "tavily", "later")
    llm = FakeLLMClient(response("无可用公告"))
    live = AskSession("600519.SH", as_of_time=NOW, market=Market(),
                      research=Research(records=[record]), llm=llm, live_cutoff=True)
    result = live.ask("新公告？")
    assert result["sources"] == []
    assert result["as_of_time"] == NOW.isoformat()
    historical = AskSession("600519.SH", as_of_time=NOW, market=Market(),
                            research=Research(records=[record]), llm=FakeLLMClient(
                                response("无可用公告")))
    assert historical.ask("新公告？")["sources"] == []


def test_cli_ask_help_and_single_question(monkeypatch, capsys):
    from quantos.commands import ask as ask_command
    monkeypatch.setattr(ask_command, "make_session", lambda symbol, as_of_time, **kwargs:
                        AskSessionStub(symbol))
    assert cli.main(["ask", "600519.SH", "--question", "最近表现？"]) == 0
    assert "市场事实" in capsys.readouterr().out
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["ask", "--help"])
    assert exit_info.value.code == 0


def test_cli_missing_security_master_is_safe_failure(monkeypatch, capsys, tmp_path):
    from quantos.commands import ask as ask_command
    from quantos.config import Settings
    monkeypatch.setattr(ask_command, "DEFAULT_SETTINGS", Settings.from_project_root(tmp_path))
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    assert cli.main(["ask", "600519.SH", "--question", "表现？"]) == 2
    output = capsys.readouterr()
    assert "Security Master has not been initialized" in output.out
    assert "Traceback" not in output.out + output.err


def test_persisted_ask_does_not_fetch_stock_basic(tmp_path):
    from quantos.commands.ask import AskSession
    from quantos.config import Settings
    from quantos.normalizers import MarketNormalizer
    from quantos.storage import MarketDataRepository, SecurityMasterRepository

    settings = Settings.from_project_root(tmp_path)
    security_repository = SecurityMasterRepository(settings)
    security_repository.write_snapshot(
        Market().fetch_listed_securities(as_of_time=NOW),
        provider_id="tushare", observed_at=NOW,
    )
    market_repository = MarketDataRepository(settings)
    raw = bar()
    market_repository.write_raw((raw,))
    market_repository.write_bars((MarketNormalizer().normalize(raw),))

    class ForbiddenLiveMarket:
        calls = 0
        def fetch_listed_securities(self, **_kwargs):
            self.calls += 1
            raise AssertionError("stock_basic must not be called by normal Ask")

    live_market = ForbiddenLiveMarket()
    result = AskSession(
        "600519.SH", as_of_time=NOW, market=live_market,
        research=Research(records=[]), llm=FakeLLMClient(response("收盘价为 1430。")),
        no_research=True, security_repository=security_repository,
        market_repository=market_repository,
    ).ask("收盘价？")

    assert result.status == "PASS"
    assert result.facts[0]["close"] == "1430.0000000000"
    assert live_market.calls == 0


def test_cli_interactive_reuses_session(monkeypatch, capsys):
    from quantos.commands import ask as ask_command
    sessions = []
    def build(symbol, as_of_time, **kwargs):
        session = AskSessionStub(symbol)
        sessions.append(session)
        return session
    questions = iter(["表现？", "消息？", ":q"])
    monkeypatch.setattr(ask_command, "make_session", build)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: next(questions))
    assert cli.main(["ask", "600519.SH"]) == 0
    assert len(sessions) == 1
    assert capsys.readouterr().out.count("市场事实  2026-09-18") == 2


def test_cli_no_research_flag_reaches_session(monkeypatch, capsys):
    from quantos.commands import ask as ask_command
    seen = []
    def build(symbol, as_of_time, **kwargs):
        seen.append(kwargs)
        return AskSessionStub(symbol)
    monkeypatch.setattr(ask_command, "make_session", build)
    assert cli.main(["ask", "600519.SH", "--no-research", "--question", "表现？"]) == 0
    assert seen[0]["no_research"] is True


def test_cli_json_uses_canonical_response_and_trace(monkeypatch, capsys):
    from quantos.commands import ask as ask_command
    monkeypatch.setattr(ask_command, "make_session", lambda symbol, as_of_time, **kwargs:
                        ask_command.AskSession(symbol, as_of_time=NOW, market=Market(),
                                               research=Research(records=[]),
                                               llm=FakeLLMClient(response("收盘价为 1430。"))))
    assert cli.main(["ask", "600519.SH", "--question", "收盘价？", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["trace_id"] == result["trace"]["trace_id"]
    assert result["trace"]["query_plan"]["steps"][0]["capability"] == "market.snapshot"
    assert result["market"]["close"] == "1430"
    assert result["source_refs"] == ["M1"]


class AskSessionStub:
    def __init__(self, symbol):
        self.symbol = symbol

    def ask(self, question):
        return {"status": "PASS", "symbol": self.symbol, "question": question,
                "mode": "market_only", "strict_pit": False,
                "market": {"trade_date": "2026-09-18", "close": "1430"}, "answer": "市场事实",
                "pit_warning": "strict_pit=false",
                "sources": [], "source_refs": [], "market_requests": 1,
                "research_requests": 0, "llm_requests": 0}
