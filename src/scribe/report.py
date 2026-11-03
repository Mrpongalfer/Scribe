"""Final report rendering for scribe-gate."""

from __future__ import annotations

import json

from .models import FinalReport, ScribeConfigurationError


def _text_summary(report: FinalReport) -> str:
    lines = [
        f"Scribe run {report.get('run_id')} -> {report.get('overall_status')}",
        f"Target: {report.get('target_project_dir')} :: {report.get('target_file_relative')}",
        f"Duration: {report.get('total_duration_seconds')}s",
        "",
        "Steps:",
    ]
    for step in report.get("steps", []):
        status = step.get("status", "?")
        name = step.get("name", "?")
        duration = step.get("duration_seconds", 0.0)
        lines.append(f"  [{status:8}] {name} ({duration:.2f}s)")
        details = step.get("details") or {}
        message = details.get("message") if isinstance(details, dict) else None
        if message:
            lines.append(f"             {message}")
        if step.get("error_message"):
            lines.append(f"             ERROR: {step['error_message']}")
    findings = report.get("ai_review_findings") or []
    if findings:
        lines.append("")
        lines.append(f"AI review findings ({len(findings)}):")
        for finding in findings:
            lines.append(
                f"  [{finding.get('severity', '?')}] "
                f"{finding.get('location', '?')}: {finding.get('description', '')}"
            )
    return "\n".join(lines)


def render(report: FinalReport, fmt: str = "json") -> str:
    """Render the final report as JSON (default) or plain text."""
    fmt = (fmt or "json").lower()
    if fmt == "json":
        return json.dumps(report, indent=2, default=str)
    if fmt == "text":
        return _text_summary(report)  # type: ignore[arg-type]
    raise ScribeConfigurationError(f"Unknown report format '{fmt}'. Choose 'json' or 'text'.")
