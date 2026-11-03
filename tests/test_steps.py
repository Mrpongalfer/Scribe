"""Unit tests: gauntlet step edge cases (audit inconclusive vs. clean)."""

import json
import subprocess

from scribe.config import ScribeConfig
from scribe.models import STATUS_FAILURE, STATUS_SUCCESS, STATUS_WARNING
from scribe.steps import WorkflowSteps


class _FakeAgent:
    def __init__(self, config, env_manager):
        self.config = config
        self.env_manager = env_manager
        self.report_data = {"audit_findings": "unsetinel"}


class _FakeEnv:
    def __init__(self, result):
        self._result = result

    def run_pip_audit(self):
        return self._result


def _agent_with_audit_result(tmp_path, monkeypatch, stdout, stderr, returncode):
    monkeypatch.chdir(tmp_path)
    config = ScribeConfig()
    result = subprocess.CompletedProcess(
        args=["pip", "audit"], returncode=returncode, stdout=stdout, stderr=stderr
    )
    return WorkflowSteps(_FakeAgent(config, _FakeEnv(result)))


def test_audit_unavailable_is_warning_not_success(tmp_path, monkeypatch):
    """pip-audit missing -> inconclusive WARNING; never a clean SUCCESS."""
    steps = _agent_with_audit_result(
        tmp_path,
        monkeypatch,
        stdout="",
        stderr="ERROR: unknown command",
        returncode=1,
    )
    status, details = steps.audit_deps()
    assert status == STATUS_WARNING
    assert "inconclusive" in details["message"]
    assert steps.agent.report_data["audit_findings"] is None


def test_audit_with_vulnerabilities_fails(tmp_path, monkeypatch):
    payload = {
        "dependencies": [],
        "vulnerabilities": [
            {
                "spec": "vuln-pkg==1.0",
                "vulns": [
                    {
                        "id": "PYSEC-0001",
                        "aliases": ["CVE-2026-0001"],
                        "fix_versions": ["1.1"],
                        "description": "test vuln",
                    }
                ],
            }
        ],
    }
    steps = _agent_with_audit_result(
        tmp_path,
        monkeypatch,
        stdout=json.dumps(payload),
        stderr="",
        returncode=0,
    )
    status, details = steps.audit_deps()
    assert status == STATUS_FAILURE  # unknown severity treated as high by default
    assert details["vulnerability_count"] == 1
    assert steps.agent.report_data["audit_findings"][0]["id"] == "PYSEC-0001"


def test_audit_clean_is_success(tmp_path, monkeypatch):
    payload = {"dependencies": [], "vulnerabilities": []}
    steps = _agent_with_audit_result(
        tmp_path,
        monkeypatch,
        stdout=json.dumps(payload),
        stderr="",
        returncode=0,
    )
    status, details = steps.audit_deps()
    assert status == STATUS_SUCCESS
    assert details["vulnerability_count"] == 0
