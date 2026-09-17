"""Generate an offline TimeSlice product from existing local artifacts."""

from __future__ import annotations

import argparse
import sys

from quantos.commands import CommandFailure, render_json
from quantos.commands.report import (
    TimeSliceOptions, add_time_slice_arguments, execute_time_slice,
    validate_time_slice,
)
from quantos.config import DEFAULT_SETTINGS
from quantos.schemas.run import RunType


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-type", choices=[item.value for item in RunType], required=True)
    add_time_slice_arguments(parser, include_output=False)
    args = parser.parse_args(argv)
    run_type = RunType(args.run_type)
    top_n = validate_time_slice(args, parser, run_type)
    try:
        result = execute_time_slice(
            TimeSliceOptions(
                args.trade_date, args.as_of_time, run_type, args.mode,
                top_n, args.no_llm, args.plan_only,
            ),
            settings=DEFAULT_SETTINGS,
        )
    except CommandFailure as error:
        print(render_json(error.record()), file=sys.stderr)
        return error.exit_code
    print(render_json(result))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
