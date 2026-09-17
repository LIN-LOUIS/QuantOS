"""Evaluate QuantOS scheduling once, optionally execute, print JSON, and exit."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import sys

from quantos.commands import CommandFailure
from quantos.commands.scheduler import add_arguments, execute, validate
from quantos.scheduler_ops import QuantOSRunExecutorAdapter


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    add_arguments(parser)
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        top_n = validate(args, parser)
        exit_code, record = execute(
            args, top_n=top_n, datetime_type=datetime,
            executor_factory=QuantOSRunExecutorAdapter,
        )
    except CommandFailure as error:
        _error(error.error_code)
        return error.exit_code
    print(json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return exit_code


def _error(code: str) -> None:
    print(json.dumps({"error_code": code}, sort_keys=True), file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
