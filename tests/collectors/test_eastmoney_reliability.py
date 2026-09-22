from __future__ import annotations

import logging
from datetime import date, datetime
from http.client import RemoteDisconnected
from urllib.error import HTTPError, URLError
from zoneinfo import ZoneInfo

import pytest

import quantos.collectors.eastmoney as eastmoney_module
from quantos.collectors import (
    EastmoneyMarketCollector,
    ProviderResponseError,
    ProviderTransportError,
)
from quantos.collectors.eastmoney import UrllibJsonTransport
from quantos.normalizers import MarketNormalizer
from quantos.observability import HealthPipelineRunner, ResultStatus, RunHealth
from quantos.schemas import JobContext, JobType

SHANGHAI = ZoneInfo("Asia/Shanghai")
PAYLOAD = b'{"rc":0,"data":{"klines":["2024-01-02,1715,1685.01,1718.19,1678.10,32156,5440082548"]}}'


class FakeResponse:
    def __init__(self, body: bytes = PAYLOAD, status: int = 200) -> None:
        self.body = body
        self.status = status
        self.headers: dict[str, str] = {}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None

    def read(self) -> bytes:
        return self.body


class SequenceUrlOpen:
    def __init__(self, *effects: object) -> None:
        self.effects = list(effects)
        self.calls = 0

    def __call__(self, request, timeout):
        effect = self.effects[self.calls]
        self.calls += 1
        if isinstance(effect, BaseException):
            raise effect
        return effect


def transport(sleeps: list[float], logger: logging.Logger | None = None):
    return UrllibJsonTransport(
        sleeper=sleeps.append,
        jitter=lambda: 0.0,
        logger=logger,
    )


def request(instance: UrllibJsonTransport):
    return instance.get_json("https://example.invalid/kline", {"secid": "1.600519"})


def test_first_attempt_success_has_no_sleep(monkeypatch) -> None:
    opener = SequenceUrlOpen(FakeResponse())
    sleeps: list[float] = []
    monkeypatch.setattr(eastmoney_module, "urlopen", opener)

    assert request(transport(sleeps))["rc"] == 0
    assert opener.calls == 1
    assert sleeps == []


def test_one_transient_failure_then_success(monkeypatch) -> None:
    opener = SequenceUrlOpen(URLError("temporary"), FakeResponse())
    sleeps: list[float] = []
    monkeypatch.setattr(eastmoney_module, "urlopen", opener)

    assert request(transport(sleeps))["rc"] == 0
    assert opener.calls == 2
    assert sleeps == [0.25]


def test_two_transient_failures_then_success(monkeypatch) -> None:
    opener = SequenceUrlOpen(
        RemoteDisconnected("temporary"), TimeoutError("slow"), FakeResponse()
    )
    sleeps: list[float] = []
    monkeypatch.setattr(eastmoney_module, "urlopen", opener)

    assert request(transport(sleeps))["rc"] == 0
    assert opener.calls == 3
    assert sleeps == [0.25, 0.50]


def test_all_attempts_fail_with_chained_provider_error(monkeypatch) -> None:
    final = ConnectionResetError("reset")
    opener = SequenceUrlOpen(URLError("one"), TimeoutError("two"), final)
    sleeps: list[float] = []
    monkeypatch.setattr(eastmoney_module, "urlopen", opener)

    with pytest.raises(ProviderTransportError, match="failed after 3 attempts") as info:
        request(transport(sleeps))

    assert opener.calls == 3
    assert sleeps == [0.25, 0.50]
    assert info.value.__cause__ is final


@pytest.mark.parametrize(
    "failure",
    [
        RemoteDisconnected("closed"),
        URLError("unavailable"),
        TimeoutError("timed out"),
        ConnectionResetError("reset"),
    ],
    ids=["remote_disconnected", "url_error", "timeout", "connection_reset"],
)
def test_supported_transient_errors_are_retried(monkeypatch, failure) -> None:
    opener = SequenceUrlOpen(failure, FakeResponse())
    sleeps: list[float] = []
    monkeypatch.setattr(eastmoney_module, "urlopen", opener)

    assert request(transport(sleeps))["rc"] == 0
    assert opener.calls == 2
    assert sleeps == [0.25]


def test_non_retryable_exception_is_not_retried(monkeypatch) -> None:
    opener = SequenceUrlOpen(ValueError("programming error"))
    sleeps: list[float] = []
    monkeypatch.setattr(eastmoney_module, "urlopen", opener)

    with pytest.raises(ValueError, match="programming error"):
        request(transport(sleeps))

    assert opener.calls == 1
    assert sleeps == []


def test_invalid_json_is_not_retried(monkeypatch) -> None:
    opener = SequenceUrlOpen(FakeResponse(b"not-json"))
    sleeps: list[float] = []
    monkeypatch.setattr(eastmoney_module, "urlopen", opener)

    with pytest.raises(ProviderResponseError, match="invalid JSON"):
        request(transport(sleeps))

    assert opener.calls == 1
    assert sleeps == []


def test_retryable_http_status_is_retried(monkeypatch) -> None:
    error = HTTPError("https://example.invalid", 503, "unavailable", {}, None)
    opener = SequenceUrlOpen(error, FakeResponse())
    sleeps: list[float] = []
    monkeypatch.setattr(eastmoney_module, "urlopen", opener)

    assert request(transport(sleeps))["rc"] == 0
    assert opener.calls == 2
    assert sleeps == [0.25]


def test_regular_http_4xx_is_not_retried(monkeypatch) -> None:
    error = HTTPError("https://example.invalid", 404, "not found", {}, None)
    opener = SequenceUrlOpen(error)
    sleeps: list[float] = []
    monkeypatch.setattr(eastmoney_module, "urlopen", opener)

    with pytest.raises(ProviderTransportError, match="HTTP 404"):
        request(transport(sleeps))

    assert opener.calls == 1
    assert sleeps == []


def test_provider_retry_structured_logging_excludes_sensitive_error(
    monkeypatch, caplog
) -> None:
    secret = "http://user:password@127.0.0.1:7890/?token=secret"
    opener = SequenceUrlOpen(URLError(secret), FakeResponse())
    sleeps: list[float] = []
    logger = logging.getLogger("quantos.eastmoney.retry.test")
    monkeypatch.setattr(eastmoney_module, "urlopen", opener)

    with caplog.at_level(logging.WARNING, logger=logger.name):
        request(transport(sleeps, logger))

    record = caplog.records[-1]
    assert record.message == "provider_retry"
    assert record.provider == "eastmoney"
    assert record.attempt == 2
    assert record.max_attempts == 3
    assert record.delay_ms == 250.0
    assert record.error_type == "URLError"
    assert secret not in caplog.text
    assert "password" not in caplog.text
    assert "token" not in caplog.text


def test_logging_failure_does_not_break_retry(monkeypatch) -> None:
    opener = SequenceUrlOpen(URLError("temporary"), FakeResponse())
    sleeps: list[float] = []

    class FailingLogger:
        def warning(self, *args, **kwargs) -> None:
            raise RuntimeError("logging unavailable")

    monkeypatch.setattr(eastmoney_module, "urlopen", opener)
    assert request(transport(sleeps, FailingLogger()))["rc"] == 0
    assert opener.calls == 2


class InMemoryRepository:
    def __init__(self) -> None:
        self.bars = []

    def write_raw(self, records) -> int:
        return len(records)

    def write_bars(self, bars) -> int:
        self.bars.extend(bars)
        return len(bars)

    def read_by_symbol(self, symbol, *, as_of_time, start_date, end_date):
        return [
            bar
            for bar in self.bars
            if bar.symbol == symbol
            and start_date <= bar.timestamp.date() <= end_date
            and bar.available_at <= as_of_time
        ]


def run_pipeline(collector: EastmoneyMarketCollector):
    tick = iter(index / 1000 for index in range(100))
    runner = HealthPipelineRunner(
        collector=collector,
        normalizer=MarketNormalizer(),
        repository=InMemoryRepository(),
        wall_clock=lambda: datetime(2024, 1, 5, 16, 0, tzinfo=SHANGHAI),
        monotonic_clock=lambda: next(tick),
    )
    context = JobContext(
        run_id="provider-retry-run",
        job_type=JobType.MARKET_INGESTION,
        trade_date=date(2024, 1, 5),
        as_of_time=datetime(2024, 1, 5, 23, 59, 59, tzinfo=SHANGHAI),
    )
    return runner.run(
        job_context=context,
        symbols=["600519.SH"],
        frequency="1d",
        start_time=datetime(2024, 1, 2, 0, 0, tzinfo=SHANGHAI),
        end_time=datetime(2024, 1, 5, 23, 59, 59, tzinfo=SHANGHAI),
    )


def test_exhausted_provider_retry_preserves_failure_observability(monkeypatch) -> None:
    opener = SequenceUrlOpen(
        RemoteDisconnected("one"),
        RemoteDisconnected("two"),
        RemoteDisconnected("three"),
    )
    monkeypatch.setattr(eastmoney_module, "urlopen", opener)
    collector = EastmoneyMarketCollector(
        transport=transport([]),
        clock=lambda: datetime(2024, 1, 5, 16, 0, tzinfo=SHANGHAI),
    )

    report = run_pipeline(collector)

    assert report.status is RunHealth.UNHEALTHY
    assert report.modules[0].status is ResultStatus.FAILED
    assert all(module.status is ResultStatus.SKIPPED for module in report.modules[1:])


def test_eventual_retry_success_keeps_pipeline_healthy(monkeypatch) -> None:
    opener = SequenceUrlOpen(RemoteDisconnected("temporary"), FakeResponse())
    monkeypatch.setattr(eastmoney_module, "urlopen", opener)
    collector = EastmoneyMarketCollector(
        transport=transport([]),
        clock=lambda: datetime(2024, 1, 5, 16, 0, tzinfo=SHANGHAI),
    )

    report = run_pipeline(collector)

    assert opener.calls == 2
    assert report.status is RunHealth.HEALTHY
    assert all(module.status is ResultStatus.SUCCESS for module in report.modules)
