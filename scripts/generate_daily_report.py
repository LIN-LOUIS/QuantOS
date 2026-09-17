"""Generate a Daily Intelligence report from existing local canonical artifacts."""

from __future__ import annotations

import argparse
import sys

from quantos.commands import CommandFailure, render_json
from quantos.commands.report import (
    DailyOptions, add_daily_arguments, execute_daily, prepare_synthesis_inputs,
    validate_common,
)
from quantos.config import DEFAULT_SETTINGS
from quantos.knowledge_integration import prepare_candidate_synthesis_input


def _prepare_synthesis_inputs(
    bundles, attribution_facts, *, company_names, mode, market_event_time,
    research_corpus_cutoff, generated_at, knowledge_settings, settings=None,
    knowledge_repository=None, index_repository=None, context_repository=None,
):
    """Compatibility helper backed by the shared command implementation."""
    return prepare_synthesis_inputs(
        bundles, attribution_facts,
        company_names=company_names, mode=mode,
        market_event_time=market_event_time,
        research_corpus_cutoff=research_corpus_cutoff,
        generated_at=generated_at, knowledge_settings=knowledge_settings,
        settings=settings or DEFAULT_SETTINGS,
        knowledge_repository=knowledge_repository,
        index_repository=index_repository,
        context_repository=context_repository,
        preparer=prepare_candidate_synthesis_input,
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_daily_arguments(parser, include_output=False)
    args = parser.parse_args(argv)
    top_n = validate_common(args, parser)
    try:
        result = execute_daily(
            DailyOptions(
                args.trade_date, args.as_of_time, args.mode, top_n, args.no_llm,
            ),
            settings=DEFAULT_SETTINGS,
            prepare_inputs=_prepare_synthesis_inputs,
        )
    except CommandFailure as error:
        print(render_json(error.record()), file=sys.stderr)
        return error.exit_code
    print(render_json(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
