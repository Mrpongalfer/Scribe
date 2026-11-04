"""The validation gauntlet: one method per pipeline stage.

Each step returns ``(status, details)`` where status is one of the
``STATUS_*`` constants. Steps raise :class:`ScribeError` subclasses for
controlled failures; unexpected exceptions are caught by the orchestrator.

Deliberate v1 design: Scribe *validates* — it never rewrites the target
code. Formatting and linting run in check mode, not fix mode.
"""

from __future__ import annotations

import ast
import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

from .models import (
    STATUS_ADVISORY,
    STATUS_FAILURE,
    STATUS_SKIPPED,
    STATUS_SUCCESS,
    STATUS_WARNING,
    ScribeFileSystemError,
    ScribeInputError,
    ScribeToolError,
    StepOutputDetails,
)

if TYPE_CHECKING:  # pragma: no cover
    from .agent import ScribeAgent

TEST_DIR_NAME = "tests/scribe_generated"

_SEVERITY_RANK = {
    "low": 0,
    "info": 0,
    "moderate": 1,
    "medium": 1,
    "high": 2,
    "critical": 3,
}


class WorkflowSteps:
    """Gauntlet stages, executed in order by :class:`ScribeAgent`."""

    def __init__(self, agent: ScribeAgent) -> None:
        self.agent = agent
        self._logger = logging.getLogger("scribe.steps")

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _truncate(text: str | None, limit: int = 2000) -> str:
        if not text:
            return ""
        text = text.strip()
        if len(text) > limit:
            return f"{text[:800]}\n...({len(text)} chars total)...\n{text[-800:]}"
        return text

    def _module_name(self) -> str:
        """Dotted module name for the target file, for test imports."""
        rel = self.agent.target_file_relative
        stem = Path(rel).with_suffix("")
        return ".".join(stem.parts)

    # ------------------------------------------------------------------
    # 1. input validation
    # ------------------------------------------------------------------
    def validate_inputs(self) -> tuple[str, StepOutputDetails]:
        self._logger.info("Validating inputs...")
        try:
            if self.agent.in_place:
                target = Path(self.agent.args.target_file).expanduser()
                if not target.is_absolute():
                    target = (self.agent.target_dir / target).resolve()
                else:
                    target = target.resolve()
                if not target.is_file():
                    raise ScribeInputError(f"Target file not found: {target}")
                try:
                    rel = target.relative_to(self.agent.target_dir.resolve())
                except ValueError as exc:
                    raise ScribeInputError(
                        f"Target file '{target}' is outside the target dir."
                    ) from exc
                self.agent.target_file_path = target
                self.agent.target_file_relative = str(rel)
                self.agent.source_file_path = target
                return STATUS_SUCCESS, {"message": f"In-place validation of {rel}."}
            source = Path(self.agent.args.source_file).expanduser().resolve(strict=True)
            rel_target = Path(self.agent.args.target_file)
            if rel_target.is_absolute() or ".." in rel_target.parts:
                raise ScribeInputError("Target file must be a relative path inside the target dir.")
            target = (self.agent.target_dir / rel_target).resolve()
            base = str(self.agent.target_dir.resolve()) + os.sep
            if not str(target).startswith(base):
                raise ScribeInputError("Target file escapes the target directory.")
            target.parent.mkdir(parents=True, exist_ok=True)
            if not source.is_file():
                raise ScribeInputError(f"Source file is not a file: {source}")
            self.agent.source_file_path = source
            self.agent.target_file_path = target
            self.agent.target_file_relative = str(rel_target)
            return STATUS_SUCCESS, {"message": f"Inputs OK. Source: {source}, target: {target}."}
        except ScribeInputError:
            raise
        except Exception as exc:
            raise ScribeInputError(f"Input validation failed: {exc}") from exc

    # ------------------------------------------------------------------
    # 2-4. environment
    # ------------------------------------------------------------------
    def setup_environment(self) -> tuple[str, StepOutputDetails]:
        self.agent.env_manager.setup_venv()
        return STATUS_SUCCESS, {
            "message": f"Virtualenv ready at {self.agent.env_manager.venv_path}."
        }

    def install_deps(self) -> tuple[str, StepOutputDetails]:
        self.agent.env_manager.install_dependencies()
        return STATUS_SUCCESS, {"message": "Dependencies installed (or none declared)."}

    def audit_deps(self) -> tuple[str, StepOutputDetails]:
        result = self.agent.env_manager.run_pip_audit()
        details: StepOutputDetails = {
            "tool_name": "pip-audit",
            "return_code": result.returncode,
        }
        payload: dict | None = None
        if result.stdout.strip():
            try:
                parsed = json.loads(result.stdout)
                if isinstance(parsed, dict):
                    payload = parsed
            except json.JSONDecodeError:
                payload = None
        if payload is None:
            # pip-audit missing, crashed, or unparseable: the audit is
            # inconclusive. A trust gate must never read that as clean.
            self.agent.report_data["audit_findings"] = None
            return STATUS_WARNING, {
                **details,
                "message": "pip-audit unavailable or its output was unparseable; "
                "audit inconclusive.",
                "stderr_summary": self._truncate(result.stderr),
            }
        vulns: list[dict] = []
        # Current pip-audit JSON nests findings under "dependencies".
        for entry in payload.get("dependencies", []):
            pkg_label = f"{entry.get('name')}=={entry.get('version')}"
            for vuln in entry.get("vulns", []):
                vulns.append(
                    {
                        "package": pkg_label,
                        "id": vuln.get("id"),
                        "aliases": vuln.get("aliases", []),
                        "fix_versions": vuln.get("fix_versions", []),
                        "description": (vuln.get("description") or "")[:500],
                    }
                )
        # Older pip-audit JSON used a top-level "vulnerabilities" list
        # of per-package entries with "spec" and nested "vulns".
        for entry in payload.get("vulnerabilities", []):
            nested = entry.get("vulns")
            if isinstance(nested, list):
                for vuln in nested:
                    vulns.append(
                        {
                            "package": entry.get("spec"),
                            "id": vuln.get("id"),
                            "aliases": vuln.get("aliases", []),
                            "fix_versions": vuln.get("fix_versions", []),
                            "description": (vuln.get("description") or "")[:500],
                        }
                    )
        details["vulnerability_count"] = len(vulns)
        details["vulnerabilities"] = vulns
        self.agent.report_data["audit_findings"] = vulns
        if not vulns:
            details["message"] = "No known vulnerabilities in dependencies."
            return STATUS_SUCCESS, details
        # pip-audit JSON carries no severity; treat unknown as high (conservative).
        threshold = _SEVERITY_RANK.get(
            str(self.agent.config.get_str("fail_on_audit_severity", "high")).lower(), 2
        )
        worst = 2  # unknown severity -> high
        details["highest_severity"] = "high (assumed; pip-audit reports no severity)"
        details["configured_fail_severity"] = self.agent.config.get_str(
            "fail_on_audit_severity", "high"
        )
        details["message"] = f"Found {len(vulns)} vulnerabilities."
        if worst >= threshold:
            return STATUS_FAILURE, details
        return STATUS_WARNING, details

    # ------------------------------------------------------------------
    # 5. apply code
    # ------------------------------------------------------------------
    def apply_code(self) -> tuple[str, StepOutputDetails]:
        if self.agent.in_place:
            return STATUS_SUCCESS, {"message": "In-place mode: nothing to copy."}
        assert self.agent.source_file_path is not None
        assert self.agent.target_file_path is not None
        try:
            code = self.agent.source_file_path.read_text(encoding="utf-8")
            self.agent.target_file_path.write_text(code, encoding="utf-8")
        except OSError as exc:
            raise ScribeFileSystemError(f"Could not copy code into target: {exc}") from exc
        return STATUS_SUCCESS, {
            "message": f"Wrote {len(code)} bytes to {self.agent.target_file_relative}."
        }

    # ------------------------------------------------------------------
    # 6-8. static checks (check mode only — Scribe never rewrites code)
    # ------------------------------------------------------------------
    def format_code(self) -> tuple[str, StepOutputDetails]:
        result = self.agent.tool_runner.run_tool(
            ["ruff", "format", "--check", str(self.agent.target_file_path)],
            cwd=self.agent.target_dir,
            venv_path=self._venv_path_or_none(),
        )
        details: StepOutputDetails = {
            "tool_name": "ruff format --check",
            "return_code": result.returncode,
            "stdout_summary": self._truncate(result.stdout),
        }
        if result.returncode == 0:
            details["message"] = "Formatting is clean."
            return STATUS_SUCCESS, details
        details["message"] = "File is not ruff-formatted."
        return STATUS_FAILURE, details

    def lint_code(self) -> tuple[str, StepOutputDetails]:
        result = self.agent.tool_runner.run_tool(
            ["ruff", "check", str(self.agent.target_file_path)],
            cwd=self.agent.target_dir,
            venv_path=self._venv_path_or_none(),
        )
        details: StepOutputDetails = {
            "tool_name": "ruff check",
            "return_code": result.returncode,
            "stdout_summary": self._truncate(result.stdout),
        }
        if result.returncode == 0:
            details["message"] = "Lint is clean."
            return STATUS_SUCCESS, details
        if result.returncode == 1:
            details["message"] = "Lint issues found."
            if self.agent.config.get_bool("fail_on_lint_critical", True):
                return STATUS_FAILURE, details
            return STATUS_WARNING, details
        raise ScribeToolError(
            f"ruff check crashed (RC={result.returncode}): {result.stderr.strip()[:500]}"
        )

    def type_check(self) -> tuple[str, StepOutputDetails]:
        result = self.agent.tool_runner.run_tool(
            ["mypy", str(self.agent.target_file_path)],
            cwd=self.agent.target_dir,
            venv_path=self._venv_path_or_none(),
        )
        details: StepOutputDetails = {
            "tool_name": "mypy",
            "return_code": result.returncode,
            "stdout_summary": self._truncate(result.stdout),
        }
        if result.returncode == 0:
            details["message"] = "Type check is clean."
            return STATUS_SUCCESS, details
        if result.returncode == 1:
            details["message"] = "Type errors found."
            if self.agent.config.get_bool("fail_on_mypy_error", True):
                return STATUS_FAILURE, details
            return STATUS_WARNING, details
        raise ScribeToolError(
            f"mypy crashed (RC={result.returncode}): {result.stderr.strip()[:500]}"
        )

    def _venv_path_or_none(self) -> Path | None:
        if "setup_environment" in self.agent.config.get_list("validation_steps"):
            return self.agent.env_manager.venv_path
        return None

    # ------------------------------------------------------------------
    # 9-12. AI test generation + execution
    # ------------------------------------------------------------------
    def extract_signatures(self) -> tuple[str, StepOutputDetails]:
        assert self.agent.target_file_path is not None
        code = self.agent.target_file_path.read_text(encoding="utf-8")
        tree = ast.parse(code)
        signatures: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                prefix = "async " if isinstance(node, ast.AsyncFunctionDef) else ""
                returns = f" -> {ast.unparse(node.returns)}" if node.returns else ""
                signatures.append(f"{prefix}def {node.name}({ast.unparse(node.args)}){returns}:")
            elif isinstance(node, ast.ClassDef):
                bases = (
                    "(" + ", ".join(ast.unparse(b) for b in node.bases) + ")" if node.bases else ""
                )
                signatures.append(f"class {node.name}{bases}:")
        return STATUS_SUCCESS, {
            "message": f"Extracted {len(signatures)} signatures.",
            "raw_output": "\n".join(signatures),
        }

    def generate_tests(
        self, sig_details: StepOutputDetails | None = None
    ) -> tuple[str, StepOutputDetails]:
        assert self.agent.target_file_path is not None
        code = self.agent.target_file_path.read_text(encoding="utf-8")
        signatures = (sig_details or {}).get("raw_output", "") or ""
        tests = self.agent.llm_client.generate_tests(
            code,
            self.agent.target_file_path.name,
            self._module_name(),
            signatures,
        )
        if tests:
            return STATUS_SUCCESS, {
                "message": "Test code generated.",
                "raw_output": tests,
                "llm_backend": self.agent.llm_client.backend.describe,
            }
        return STATUS_FAILURE, {"message": "AI test generation failed."}

    def save_tests(
        self, gen_details: StepOutputDetails | None = None
    ) -> tuple[str, StepOutputDetails]:
        code = (gen_details or {}).get("raw_output", "") or ""
        if not code:
            return STATUS_WARNING, {"message": "No generated test code to save."}
        assert self.agent.target_file_path is not None
        test_dir = self.agent.target_dir / TEST_DIR_NAME
        test_dir.mkdir(parents=True, exist_ok=True)
        safe_stem = "".join(c if c.isalnum() else "_" for c in self.agent.target_file_path.stem)
        path = test_dir / f"test_{safe_stem}_scribe.py"
        path.write_text(code, encoding="utf-8")
        return STATUS_SUCCESS, {
            "message": f"Saved generated tests to {path.name}.",
            "generated_content_path": str(path),
            "raw_output": str(path),
        }

    def execute_tests(
        self, saved_details: StepOutputDetails | None = None
    ) -> tuple[str, StepOutputDetails]:
        path_str = (saved_details or {}).get("raw_output", "") or ""
        if not path_str or not Path(path_str).is_file():
            return STATUS_FAILURE, {
                "message": f"Generated test file not found: {path_str or '(none)'}."
            }
        env = os.environ.copy()
        pythonpath_parts = [
            str(self.agent.target_dir.resolve()),
            str((self.agent.target_dir / "src").resolve()),
        ]
        if env.get("PYTHONPATH"):
            pythonpath_parts.append(env["PYTHONPATH"])
        env["PYTHONPATH"] = os.pathsep.join(pythonpath_parts)

        result = self.agent.tool_runner.run_tool(
            ["pytest", path_str, "-q"],
            cwd=self.agent.target_dir,
            venv_path=self._venv_path_or_none(),
            env_vars=env,
        )
        self.agent.report_data["test_results_summary"] = {
            "return_code": result.returncode,
            "stdout_tail": self._truncate(result.stdout, 4000),
        }
        details: StepOutputDetails = {
            "tool_name": "pytest",
            "return_code": result.returncode,
            "stdout_summary": self._truncate(result.stdout),
        }
        if result.returncode == 0:
            details["message"] = "All generated tests passed."
            return STATUS_SUCCESS, details
        if result.returncode == 1:
            details["message"] = "Generated tests failed."
            if self.agent.config.get_bool("fail_on_test_failure", True):
                return STATUS_FAILURE, details
            return STATUS_WARNING, details
        if result.returncode == 5:
            details["message"] = "Pytest collected no tests."
            return STATUS_WARNING, details
        raise ScribeToolError(
            f"pytest crashed (RC={result.returncode}): {result.stderr.strip()[:500]}"
        )

    # ------------------------------------------------------------------
    # 13. AI review (advisory — findings are reported, never auto-fixed)
    # ------------------------------------------------------------------
    def review_code(self) -> tuple[str, StepOutputDetails]:
        assert self.agent.target_file_path is not None
        code = self.agent.target_file_path.read_text(encoding="utf-8")
        findings = self.agent.llm_client.generate_review(code, self.agent.target_file_path.name)
        self.agent.report_data["ai_review_findings"] = findings
        if findings is None:
            return STATUS_WARNING, {
                "message": "AI review failed; continuing without findings.",
                "issues_found": [],
            }
        return STATUS_ADVISORY, {
            "message": f"AI review returned {len(findings)} finding(s).",
            "issues_found": findings,
            "llm_backend": self.agent.llm_client.backend.describe,
        }

    # ------------------------------------------------------------------
    # 14. pre-commit hooks
    # ------------------------------------------------------------------
    def run_precommit(self) -> tuple[str, StepOutputDetails]:
        config_file = self.agent.target_dir / ".pre-commit-config.yaml"
        if not config_file.is_file():
            return STATUS_SKIPPED, {"message": "No .pre-commit-config.yaml; skipped."}
        assert self.agent.target_file_path is not None
        result = self.agent.tool_runner.run_tool(
            ["pre-commit", "run", "--files", str(self.agent.target_file_path)],
            cwd=self.agent.target_dir,
            venv_path=self._venv_path_or_none(),
        )
        if result.returncode == 0:
            return STATUS_SUCCESS, {"message": "Pre-commit hooks passed."}
        return STATUS_FAILURE, {
            "message": f"Pre-commit hooks failed (RC={result.returncode}).",
            "stdout_summary": self._truncate(result.stdout),
        }

    # ------------------------------------------------------------------
    # 15. conditional commit (human approval gate)
    # ------------------------------------------------------------------
    def commit_changes(self) -> tuple[str, StepOutputDetails]:
        git_exe = self.agent.git_exe
        if not git_exe:
            return STATUS_WARNING, {"message": "git not found; commit skipped."}
        assert self.agent.target_file_path is not None

        if not self.agent.request_commit_approval():
            self.agent.report_data["commit_attempted"] = False
            return STATUS_WARNING, {"message": "Commit skipped: not approved by operator."}

        status = self.agent.tool_runner.run_tool(
            [git_exe, "status", "--porcelain", str(self.agent.target_file_path)],
            cwd=self.agent.target_dir,
        )
        if not status.stdout.strip():
            self.agent.report_data["commit_sha"] = "NO_CHANGES"
            return STATUS_SUCCESS, {"message": "Working tree clean; nothing to commit."}

        add = self.agent.tool_runner.run_tool(
            [git_exe, "add", str(self.agent.target_file_path)],
            cwd=self.agent.target_dir,
        )
        if add.returncode != 0:
            raise ScribeToolError(f"git add failed: {add.stderr.strip()[:500]}")

        template = self.agent.config.get_str("commit_message_template")
        try:
            message = template.format(target_file=self.agent.target_file_path.name)
        except KeyError:
            message = template
        commit = self.agent.tool_runner.run_tool(
            [git_exe, "commit", "-m", message], cwd=self.agent.target_dir
        )
        if commit.returncode != 0:
            combined = (commit.stdout + commit.stderr).lower()
            if "nothing to commit" in combined:
                self.agent.report_data["commit_sha"] = "NO_EFFECTIVE_CHANGES"
                return STATUS_SUCCESS, {"message": "Nothing effective to commit."}
            raise ScribeToolError(
                f"git commit failed (RC={commit.returncode}): {commit.stderr.strip()[:500]}"
            )
        sha = self.agent.tool_runner.run_tool(
            [git_exe, "rev-parse", "HEAD"], cwd=self.agent.target_dir
        )
        commit_sha = sha.stdout.strip()
        self.agent.report_data["commit_sha"] = commit_sha
        return STATUS_SUCCESS, {
            "message": f"Committed as {commit_sha}.",
            "commit_sha": commit_sha,
        }

    # ------------------------------------------------------------------
    # 16. report
    # ------------------------------------------------------------------
    def generate_report(self) -> tuple[str, StepOutputDetails]:
        return STATUS_SUCCESS, {"message": "Final report prepared."}
