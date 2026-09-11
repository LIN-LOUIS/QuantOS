"""Centralized conversion of provider symbols to QuantOS canonical symbols."""

from __future__ import annotations

import re


_CANONICAL_PATTERN = re.compile(r"^(\d{6})\.(SH|SZ|BJ)$")
_BAOSTOCK_PATTERN = re.compile(r"^(sh|sz)\.(\d{6})$")


def validate_canonical_symbol(symbol: str) -> str:
    if not _CANONICAL_PATTERN.fullmatch(symbol):
        raise ValueError("symbol must use the canonical 000001.SZ format")
    return symbol


def canonical_to_tushare(symbol: str) -> str:
    return validate_canonical_symbol(symbol)


def tushare_to_canonical(symbol: str) -> str:
    return validate_canonical_symbol(symbol)


def canonical_to_baostock(symbol: str) -> str:
    match = _CANONICAL_PATTERN.fullmatch(symbol)
    if match is None:
        raise ValueError("symbol must use the canonical 000001.SZ format")
    code, exchange = match.groups()
    if exchange == "BJ":
        raise ValueError("BaoStock does not support canonical BJ symbols")
    return f"{exchange.lower()}.{code}"


def baostock_to_canonical(symbol: str) -> str:
    match = _BAOSTOCK_PATTERN.fullmatch(symbol)
    if match is None:
        raise ValueError("BaoStock symbol must use the sh.600519 format")
    exchange, code = match.groups()
    return f"{code}.{exchange.upper()}"
