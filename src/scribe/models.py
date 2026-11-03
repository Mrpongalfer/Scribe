"""Shared data models, status constants, and the error hierarchy for scribe-gate."""

from __future__ import annotations

from typing import Any, TypedDict

# ---------------------------------------------------------------------------
# Step status constants
# ---------------------------------------------------------------------------
STATUS_SUCCESS = "SUCCESS"
STATUS_FAILURE = "FAILURE"
STATUS_WARNING = "WARNING"
STATUS_SKIPPED = "SKIPPED"
STATUS_ADVISORY = "ADVISORY"
STATUS_PENDING = "PENDING"

ALL_STATUSES = (
    STATUS_SUCCESS,
    STATUS_FAILURE,
    STATUS_WARNING,
    STATUS_SKIPPED,
    STATUS_ADVISORY,
    STATUS_PENDING,
)


# ---------------------------------------------------------------------------
# Report data models
# ---------------------------------------------------------------------------
class StepOutputDetails(TypedDict, total=False):
    tool_name: str | None
    return_code: int | None
    message: str | None
    stdout_summary: str | None
    stderr_summary: str | None
    llm_backend: str | None
    llm_model: str | None
    prompt_type: str | None
    generated_content_path: str | None
    vulnerability_count: int | None
    vulnerabilities: list[dict[str, Any]] | None
    highest_severity: str | None
    configured_fail_severity: str | None
    issues_found: list[dict[str, Any]] | None
    notes: list[str] | None
    bytes_written: int | None
    commit_sha: str | None
    raw_output: str | None
    traceback: str | None


class StepResult(TypedDict):
    name: str
    status: str
    start_time: str
    end_time: str
    duration_seconds: float
    details: dict[str, Any] | None
    error_message: str | None


class FinalReport(TypedDict):
    scribe_version: str
    run_id: str
    start_time: str
    end_time: str
    total_duration_seconds: float
    overall_status: str
    target_project_dir: str
    target_file_relative: str
    language: str
    python_version: str
    commit_attempted: bool
    commit_sha: str | None
    steps: list[StepResult]
    audit_findings: list[dict[str, Any]] | None
    ai_review_findings: list[dict[str, Any]] | None
    test_results_summary: dict[str, Any] | None
    error_message: str | None


# ---------------------------------------------------------------------------
# Error hierarchy
# ---------------------------------------------------------------------------
class ScribeError(Exception):
    """Base exception for all scribe-gate errors."""

    def __init__(self, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.details: dict[str, Any] = details if details is not None else {}


class ScribeConfigurationError(ScribeError):
    """Raised for invalid or unloadable configuration."""


class ScribeInputError(ScribeError):
    """Raised for invalid user inputs (paths, arguments)."""


class ScribeEnvironmentError(ScribeError):
    """Raised for environment setup failures (venv, dependency install)."""


class ScribeToolError(ScribeError):
    """Raised when an external tool cannot be executed."""


class ScribeApiError(ScribeError):
    """Raised for LLM backend communication failures."""


class ScribeFileSystemError(ScribeError):
    """Raised for file-system operation failures."""
