"""ScribeAgent: orchestrates the validation gauntlet end to end."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import traceback
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .config import ScribeConfig
from .environment import EnvironmentManager
from .llm import LLMBackend, LLMClient, build_backend
from .models import (
    STATUS_FAILURE,
    STATUS_PENDING,
    STATUS_SKIPPED,
    STATUS_SUCCESS,
    FinalReport,
    ScribeError,
    ScribeInputError,
    StepResult,
)
from .report import render
from .runner import ToolRunner
from .steps import WorkflowSteps

APP_NAME = "scribe-gate"

# Steps that consume the previous step's details as an argument.
_STEPS_WITH_DEPENDENCIES = {"generate_tests", "save_tests", "execute_tests"}


class ScribeAgent:
    """Runs the configured gauntlet steps in order and emits a final report."""

    def __init__(self, args: argparse.Namespace, llm_backend: LLMBackend | None = None) -> None:
        self.args = args
        self.in_place: bool = bool(getattr(args, "in_place", False))

        # Commit policy: NEVER commit unless explicitly requested, and even
        # then only with operator approval (see request_commit_approval).
        self._review_only = bool(args.review_only)
        self._commit_requested = bool(args.commit) and not self._review_only
        self._do_commit = self._commit_requested and not bool(args.no_commit)

        self._run_id = f"scribe_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')}"
        self._logger = logging.getLogger(APP_NAME)
        self._overall_success = True

        self.source_file_path: Path | None = None
        self.target_file_path: Path | None = None
        self.target_file_relative: str = str(args.target_file)

        try:
            target_dir = Path(args.target_dir).expanduser().resolve()
            self.config = ScribeConfig(
                config_path_override=args.config_file,
                project_dir_for_local_config=target_dir,
            )
            self.target_dir = self._validate_target_dir(target_dir)
            self.tool_runner = ToolRunner(self.config)
            self.env_manager = EnvironmentManager(self.target_dir, self.tool_runner, self.config)
            backend = llm_backend or build_backend(
                self.config,
                backend=args.llm_backend,
                model=args.llm_model,
                base_url=args.llm_base_url,
            )
            self.llm_client = LLMClient(self.config, backend)
            self.git_exe = self._find_git()
            self.report_data: FinalReport = self._initialize_report()
            self.workflow = WorkflowSteps(self)
        except ScribeError as exc:
            self._emit_minimal_error_report(f"Initialization failed: {exc}")
            raise SystemExit(2) from exc
        except Exception as exc:  # noqa: BLE001 - init must never crash silently
            self._emit_minimal_error_report(
                f"Unexpected initialization error: {exc}", include_traceback=True
            )
            raise SystemExit(2) from exc

        self._logger.info(
            "%s v%s starting | run=%s | target=%s | commit_planned=%s",
            APP_NAME,
            __version__,
            self._run_id,
            self.target_dir,
            self._do_commit,
        )

    # ------------------------------------------------------------------
    # setup helpers
    # ------------------------------------------------------------------
    def _validate_target_dir(self, target_dir: Path) -> Path:
        if not target_dir.is_dir():
            raise ScribeInputError(f"Target dir is not a directory: {target_dir}")
        allowed = [
            Path(p).expanduser().resolve()
            for p in self.config.get_list("allowed_target_bases", [])
        ]
        if allowed and not any(target_dir.is_relative_to(base) for base in allowed):
            raise ScribeInputError(
                f"Target dir '{target_dir}' is outside allowed bases: {allowed}. "
                "Adjust 'allowed_target_bases' in scribe.toml to permit it."
            )
        return target_dir

    def _find_git(self) -> str | None:
        try:
            return str(self.tool_runner._find_executable("git"))
        except ScribeError:
            self._logger.warning("git executable not found; commit step will skip.")
            return None

    def _initialize_report(self) -> FinalReport:
        return {
            "scribe_version": __version__,
            "run_id": self._run_id,
            "start_time": datetime.now(timezone.utc).isoformat(),
            "end_time": "",
            "total_duration_seconds": 0.0,
            "overall_status": STATUS_PENDING,
            "target_project_dir": str(self.target_dir),
            "target_file_relative": self.target_file_relative,
            "language": "python",
            "python_version": sys.version.split()[0],
            "commit_attempted": self._do_commit,
            "commit_sha": None,
            "steps": [],
            "audit_findings": None,
            "ai_review_findings": None,
            "test_results_summary": None,
            "error_message": None,
        }

    # ------------------------------------------------------------------
    # commit approval gate
    # ------------------------------------------------------------------
    def request_commit_approval(self) -> bool:
        """Human gate: approve the git commit, or decline safely.

        ``--yes`` bypasses the prompt (explicit operator intent for CI).
        Without a TTY and without ``--yes``, approval is declined — the
        safe default is to never commit blind.
        """
        if getattr(self.args, "yes", False):
            self._logger.info("Commit approved via --yes flag.")
            return True
        stdin = sys.stdin
        if not stdin.isatty():
            self._logger.warning(
                "Commit requested but stdin is not interactive and --yes was not "
                "passed; declining commit (safe default)."
            )
            return False
        try:
            answer = (
                input(
                    f"\n[{APP_NAME}] Validation passed. Commit "
                    f"'{self.target_file_relative}'? [y/N] "
                )
                .strip()
                .lower()
            )
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        approved = answer in ("y", "yes")
        self._logger.info("Operator commit approval: %s", "granted" if approved else "declined")
        return approved

    # ------------------------------------------------------------------
    # main loop
    # ------------------------------------------------------------------
    def run(self) -> int:
        steps = self.config.get_list("validation_steps")
        if not steps:
            self._record_custom(
                "pipeline_setup",
                STATUS_FAILURE,
                {"message": "No validation steps configured."},
                error_message="Empty validation_steps in config.",
            )
            self._overall_success = False
            self._finish_report()
            return 1

        step_outputs: dict[str, dict[str, Any]] = {}
        for step_name in steps:
            step_func = getattr(self.workflow, step_name, None)
            if not callable(step_func):
                self._logger.error("Unknown step '%s' in validation_steps.", step_name)
                self._record_custom(
                    step_name,
                    STATUS_FAILURE,
                    {"message": f"Unknown step '{step_name}'."},
                    error_message=f"Unknown step '{step_name}'.",
                )
                self._overall_success = False
                break

            if step_name == "run_precommit" and self._review_only:
                self._record_custom(
                    step_name, STATUS_SKIPPED, {"message": "Skipped (--review-only)."}
                )
                continue
            if step_name in {"generate_tests", "save_tests", "execute_tests"} and self._review_only:
                self._record_custom(
                    step_name, STATUS_SKIPPED, {"message": "Skipped (--review-only)."}
                )
                continue
            if step_name == "commit_changes" and not self._do_commit:
                reason = (
                    "--review-only"
                    if self._review_only
                    else "--no-commit"
                    if self.args.no_commit
                    else "commit not requested (pass --commit to enable)"
                )
                self._record_custom(
                    step_name,
                    STATUS_SKIPPED,
                    {"message": f"Skipped ({reason})."},
                )
                self.report_data["commit_attempted"] = False
                continue

            dependency = None
            if step_name in _STEPS_WITH_DEPENDENCIES:
                dependency = step_outputs.get(
                    {
                        "generate_tests": "extract_signatures",
                        "save_tests": "generate_tests",
                        "execute_tests": "save_tests",
                    }[step_name],
                    {},
                ).get("details")

            if not self._execute_step(step_name, step_func, step_outputs, dependency):
                self._logger.warning("Halting pipeline at failed step '%s'.", step_name)
                break

            if step_name == "review_code" and self._review_only:
                self._logger.info("--review-only: stopping after review_code.")
                break

        self._finish_report()
        return 0 if self._overall_success else 1

    def _execute_step(
        self,
        name: str,
        step_func: Callable,
        step_outputs: dict[str, dict[str, Any]],
        dependency: dict[str, Any] | None,
    ) -> bool:
        start = datetime.now(timezone.utc)
        result: StepResult = {
            "name": name,
            "status": STATUS_PENDING,
            "start_time": start.isoformat(),
            "end_time": "",
            "duration_seconds": 0.0,
            "details": None,
            "error_message": None,
        }
        try:
            self._logger.info("--- Step: %s ---", name)
            if name in _STEPS_WITH_DEPENDENCIES:
                status, details = step_func(dependency)
            else:
                status, details = step_func()
            result["status"] = status
            result["details"] = details
            step_outputs[name] = {"details": details}
            if status == STATUS_FAILURE:
                self._overall_success = False
        except ScribeError as exc:
            self._logger.error("Step '%s' failed: %s", name, exc)
            result["status"] = STATUS_FAILURE
            result["error_message"] = str(exc)
            result["details"] = exc.details or {"message": str(exc)}
            self._overall_success = False
        except Exception as exc:  # noqa: BLE001 - a step must never kill the report
            self._logger.critical("Step '%s' crashed: %s", name, exc, exc_info=True)
            result["status"] = STATUS_FAILURE
            result["error_message"] = f"{type(exc).__name__}: {exc}"
            result["details"] = {"traceback": traceback.format_exc()}
            self._overall_success = False
        self._record(result)
        return result["status"] != STATUS_FAILURE

    # ------------------------------------------------------------------
    # report bookkeeping
    # ------------------------------------------------------------------
    def _record(self, result: StepResult) -> None:
        end = datetime.now(timezone.utc)
        result["end_time"] = end.isoformat()
        try:
            start = datetime.fromisoformat(result["start_time"])
            result["duration_seconds"] = round((end - start).total_seconds(), 3)
        except ValueError:
            result["duration_seconds"] = -1.0
        self.report_data["steps"].append(result)
        self._logger.info(
            "Step '%s' -> %s (%.2fs)",
            result["name"],
            result["status"],
            result["duration_seconds"],
        )

    def _record_custom(
        self,
        name: str,
        status: str,
        details: dict[str, Any],
        error_message: str | None = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self._record(
            {
                "name": name,
                "status": status,
                "start_time": now,
                "end_time": now,
                "duration_seconds": 0.0,
                "details": details,
                "error_message": error_message,
            }
        )

    def _finish_report(self) -> None:
        self.report_data["end_time"] = datetime.now(timezone.utc).isoformat()
        try:
            start = datetime.fromisoformat(self.report_data["start_time"])
            end = datetime.fromisoformat(self.report_data["end_time"])
            self.report_data["total_duration_seconds"] = round((end - start).total_seconds(), 3)
        except ValueError:
            self.report_data["total_duration_seconds"] = -1.0
        self.report_data["overall_status"] = (
            STATUS_SUCCESS if self._overall_success else STATUS_FAILURE
        )
        output = render(self.report_data, self.args.report_format)
        report_file = getattr(self.args, "report_file", None)
        if report_file:
            try:
                Path(report_file).expanduser().write_text(output, encoding="utf-8")
                self._logger.info("Wrote report to %s", report_file)
            except OSError as exc:
                self._logger.error("Could not write report file: %s", exc)
        print(output)

    def _emit_minimal_error_report(self, message: str, include_traceback: bool = False) -> None:
        now = datetime.now(timezone.utc).isoformat()
        step: StepResult = {
            "name": "initialization_failure",
            "status": STATUS_FAILURE,
            "start_time": now,
            "end_time": now,
            "duration_seconds": 0.0,
            "details": {"message": message},
            "error_message": message,
        }
        if include_traceback:
            step["details"] = {
                "message": message,
                "traceback": traceback.format_exc(),
            }
        report: dict[str, Any] = {
            "scribe_version": __version__,
            "run_id": getattr(self, "_run_id", "unknown"),
            "start_time": now,
            "end_time": now,
            "total_duration_seconds": 0.0,
            "overall_status": STATUS_FAILURE,
            "target_project_dir": "unset",
            "target_file_relative": "unset",
            "language": "python",
            "python_version": sys.version.split()[0],
            "commit_attempted": False,
            "commit_sha": None,
            "steps": [step],
            "audit_findings": None,
            "ai_review_findings": None,
            "test_results_summary": None,
            "error_message": message,
        }
        try:
            print(json.dumps(report, indent=2, default=str))
        except Exception:  # noqa: BLE001 - last resort
            print(f'{{"overall_status": "FAILURE", "error_message": {message!r}}}')
