"""CLI and fresh-state integration for the Phase 6B data foundation."""

from datetime import date, datetime
import json

from quantos import cli
from quantos.collectors import RawMarketRecord
from quantos.commands.status import inspect_status
from quantos.commands.ask import AskSession
from quantos.config import MARKET_TIMEZONE, Settings
from quantos.data import DataBootstrapService
from quantos.normalizers import MarketNormalizer
from quantos.schemas import SecurityMaster
from quantos.storage import MarketDataRepository, SecurityMasterRepository


NOW = datetime(2026, 9, 19, 20, tzinfo=MARKET_TIMEZONE)


def test_unconfigured_data_cli_fails_safely_and_persists_audit(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)

    code = cli.main([
        "data", "bootstrap", "security-master", "--provider", "tushare",
        "--project-root", str(tmp_path), "--json",
    ])

    payload = json.loads(capsys.readouterr().out)
    assert code == 2
    assert payload["status"] == "FAIL"
    assert payload["reason_codes"] == ["UNCONFIGURED"]
    assert "token" not in json.dumps(payload).lower()


def test_fresh_state_bootstrap_status_and_persisted_ask_foundation(tmp_path):
    class FakeProvider:
        def fetch_listed_securities(self, *, as_of_time):
            return [SecurityMaster(
                "600519.SH", "贵州茅台", "SHSE", date(2001, 8, 27),
                as_of_time, source="tushare",
            )]

        def fetch_trade_dates(self, **_kwargs):
            return [date(2026, 9, 18)]

        def fetch_bars(self, **_kwargs):
            return [RawMarketRecord(
                "tushare", "600519.SH", "daily:fresh", NOW,
                {"timestamp": "2026-09-18", "frequency": "1d",
                 "open": "1250", "high": "1280", "low": "1240",
                 "close": "1266.98", "prev_close": "1258", "volume": "1000",
                 "amount": "1266980", "available_at": NOW.replace(day=18, hour=18).isoformat()},
            )]

    settings = Settings.from_project_root(tmp_path)
    provider = FakeProvider()
    bootstrap = DataBootstrapService(
        settings=settings, providers={"tushare": provider}, clock=lambda: NOW,
    )
    assert bootstrap.bootstrap_security_master("tushare").status == "PASS"
    assert bootstrap.bootstrap_market(
        "tushare", symbols=("600519.SH",), trading_days=1, as_of_time=NOW,
    ).status == "PASS"

    identity = SecurityMasterRepository(settings).resolve("贵州茅台", as_of_time=NOW)
    market = MarketDataRepository(settings).load_market_bar_history(
        "600519.SH", end_date=NOW.date(), lookback=1, as_of_time=NOW,
    )
    status = inspect_status(tmp_path)
    ask = AskSession(
        "600519.SH", as_of_time=NOW, market=None,
        research=None,
        llm=None,
        no_research=True,
        security_repository=SecurityMasterRepository(settings),
        market_repository=MarketDataRepository(settings),
    ).ask("最近一个交易日表现怎么样？")

    assert identity[0].symbol == "600519.SH"
    assert market[0].close == MarketNormalizer().normalize(provider.fetch_bars()[0]).close
    assert status["data_availability"]["security_master"]["availability"] == "READY"
    assert status["data_availability"]["recent_market"]["availability"] == "READY"
    assert ask.status == "PASS"
    assert ask.facts[0]["source_record_id"] == "daily:fresh"
