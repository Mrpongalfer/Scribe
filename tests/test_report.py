"""Unit tests: final report rendering."""

import json

import pytest

from scribe.models import ScribeConfigurationError
from scribe.report import render


def _sample_report():
    return {
        "scribe_version": "0.1.0",
        "run_id": "scribe_test",
        "start_time": "2026-01-01T00:00:00+00:00",
        "end_time": "2026-01-01T00:00:01+00:00",
        "total_duration_seconds": 1.0,
        "overall_status": "SUCCESS",
        "target_project_dir": "/tmp/proj",
        "target_file_relative": "mod.py",
        "language": "python",
        "python_version": "3.12.0",
        "commit_attempted": False,
        "commit_sha": None,
        "steps": [
            {
                "name": "lint_code",
                "status": "SUCCESS",
                "start_time": "",
                "end_time": "",
                "duration_seconds": 0.4,
                "details": {"message": "Lint is clean."},
                "error_message": None,
            }
        ],
        "audit_findings": [],
        "ai_review_findings": [{"severity": "low", "description": "nit", "location": "general"}],
        "test_results_summary": {"return_code": 0},
        "error_message": None,
    }


def test_json_report_is_parseable_and_complete():
    parsed = json.loads(render(_sample_report(), "json"))
    assert parsed["overall_status"] == "SUCCESS"
    assert parsed["steps"][0]["name"] == "lint_code"
    assert parsed["ai_review_findings"][0]["severity"] == "low"


def test_text_report_is_human_readable():
    text = render(_sample_report(), "text")
    assert "SUCCESS" in text
    assert "lint_code" in text
    assert "nit" in text


def test_unknown_format_raises():
    with pytest.raises(ScribeConfigurationError):
        render(_sample_report(), "yaml")
