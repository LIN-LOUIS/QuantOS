import pytest

from quantos.collectors.symbols import (
    baostock_to_canonical,
    canonical_to_baostock,
    canonical_to_tushare,
    tushare_to_canonical,
)


@pytest.mark.parametrize("symbol", ["600519.SH", "000001.SZ", "300750.SZ"])
def test_tushare_symbol_round_trip(symbol: str) -> None:
    assert tushare_to_canonical(canonical_to_tushare(symbol)) == symbol


@pytest.mark.parametrize(
    ("canonical", "provider"),
    [("600519.SH", "sh.600519"), ("000001.SZ", "sz.000001"), ("300750.SZ", "sz.300750")],
)
def test_baostock_symbol_round_trip(canonical: str, provider: str) -> None:
    assert canonical_to_baostock(canonical) == provider
    assert baostock_to_canonical(provider) == canonical


def test_baostock_does_not_claim_bse_support() -> None:
    with pytest.raises(ValueError, match="does not support.*BJ"):
        canonical_to_baostock("430047.BJ")
