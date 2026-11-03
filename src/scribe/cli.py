"""Command-line entry point for scribe-gate (``scribe`` / ``scribe-gate``)."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from . import __version__
from .agent import APP_NAME, ScribeAgent
from .config import (
    DEFAULT_OLLAMA_BASE_URL,
)

LOG_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scribe",
        description=(
            f"{APP_NAME} v{__version__}: a validation gauntlet / trust gate for "
            "agent-written Python code. Validates, reports, and only commits "
            "with explicit operator approval."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "target_dir",
        nargs="?",
        default=".",
        help="Project directory the code belongs to (default: current dir).",
    )
    source_group = parser.add_mutually_exclusive_group()
    source_group.add_argument(
        "--source-file",
        default=None,
        help="Path to the new code file to validate.",
    )
    source_group.add_argument(
        "--files",
        nargs="+",
        default=None,
        metavar="FILE",
        help="Validate these files in place (paths relative to target_dir). "
        "Used by the pre-commit hook.",
    )
    parser.add_argument(
        "--target-file",
        default=None,
        help="Relative path inside target_dir to place the new code "
        "(e.g. 'src/main.py'). Required with --source-file.",
    )
    parser.add_argument(
        "--review-only",
        action="store_true",
        help="Run through the AI review step, print the report, then exit. Never commits.",
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Allow a git commit if validation passes. Still requires "
        "operator approval at the prompt unless --yes is given.",
    )
    parser.add_argument(
        "--no-commit",
        action="store_true",
        help="Explicitly disable the commit step (this is already the default).",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Approve the commit non-interactively. Only meaningful with --commit.",
    )
    parser.add_argument(
        "--llm-backend",
        default=None,
        choices=["ollama", "openai", "openai-compatible"],
        help="LLM backend (default: SCRIBE_LLM_BACKEND or 'ollama').",
    )
    parser.add_argument(
        "--llm-model",
        default=None,
        help="Model name (default: SCRIBE_LLM_MODEL or backend default).",
    )
    parser.add_argument(
        "--llm-base-url",
        default=None,
        help=f"Backend base URL (default: SCRIBE_LLM_BASE_URL or {DEFAULT_OLLAMA_BASE_URL}).",
    )
    parser.add_argument(
        "--config-file",
        default=None,
        help="Explicit path to a scribe.toml config file.",
    )
    parser.add_argument(
        "--report-format",
        default="json",
        choices=["json", "text"],
        help="Final report output format.",
    )
    parser.add_argument(
        "--report-file",
        default=None,
        help="Also write the final report to this path.",
    )
    parser.add_argument(
        "--log-level", default="INFO", choices=LOG_LEVELS, help="Console log level."
    )
    parser.add_argument("--log-file", default=None, help="Append detailed DEBUG logs to this file.")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    return parser


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    args = build_parser().parse_args(argv)
    args.in_place = args.files is not None
    if args.files:
        if args.source_file or args.target_file:
            raise SystemExit("error: --files cannot be combined with --source-file/--target-file.")
    else:
        if not args.source_file or not args.target_file:
            raise SystemExit(
                "error: --source-file and --target-file are required "
                "(or use --files for in-place validation)."
            )
    return args


def setup_logging(level: str, log_file: str | None = None) -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for handler in root.handlers[:]:
        root.removeHandler(handler)
        try:
            handler.close()
        except Exception:  # noqa: BLE001 - logging cleanup must not fail
            pass
    logging.Formatter.converter = time.gmtime
    formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s (UTC)")
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(getattr(logging, level.upper(), logging.INFO))
    console.setFormatter(formatter)
    root.addHandler(console)
    if log_file:
        try:
            path = Path(log_file).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.FileHandler(path, mode="a", encoding="utf-8")
            file_handler.setLevel(logging.DEBUG)
            file_handler.setFormatter(formatter)
            root.addHandler(file_handler)
        except OSError as exc:
            logging.getLogger(APP_NAME).error("Could not open log file '%s': %s", log_file, exc)


def _run_single(args: argparse.Namespace) -> int:
    agent = ScribeAgent(args)
    return agent.run()


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging(args.log_level, args.log_file)

    if not args.in_place:
        return _run_single(args)

    # --files mode: one gauntlet run per file, each with its own report.
    assert args.files is not None
    worst = 0
    for index, filename in enumerate(args.files):
        if index:
            print("---", file=sys.stderr)
        per_file = argparse.Namespace(
            **{**vars(args), "source_file": filename, "target_file": filename, "in_place": True}
        )
        try:
            code = _run_single(per_file)
        except SystemExit as exc:
            code = int(exc.code) if isinstance(exc.code, int) else 1
        worst = max(worst, code)
    return worst


if __name__ == "__main__":
    raise SystemExit(main())
