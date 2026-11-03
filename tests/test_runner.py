"""Unit tests: ToolRunner execution, timeouts, output caps, error paths."""

import sys

import pytest

from scribe.config import ScribeConfig
from scribe.models import ScribeToolError
from scribe.runner import ToolRunner


@pytest.fixture()
def runner(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return ToolRunner(ScribeConfig())


def test_successful_command(runner, tmp_path):
    result = runner.run_tool([sys.executable, "-c", "print('hello')"], cwd=tmp_path)
    assert result.returncode == 0
    assert result.stdout.strip() == "hello"


def test_nonzero_exit_does_not_raise(runner, tmp_path):
    result = runner.run_tool([sys.executable, "-c", "import sys; sys.exit(3)"], cwd=tmp_path)
    assert result.returncode == 3


def test_missing_executable_raises(runner, tmp_path):
    with pytest.raises(ScribeToolError, match="not found"):
        runner.run_tool(["definitely-not-a-real-tool-xyz"], cwd=tmp_path)


def test_timeout_reports_returncode_minus_one(runner, tmp_path):
    result = runner.run_tool(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=tmp_path,
        timeout=0.3,
    )
    assert result.returncode == -1
    assert "TimeoutExpired" in result.stderr


def test_output_is_capped(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = ScribeConfig()
    config._config["max_tool_output_chars"] = 100
    runner = ToolRunner(config)
    result = runner.run_tool([sys.executable, "-c", "print('x' * 5000)"], cwd=tmp_path)
    assert result.returncode == 0
    assert len(result.stdout) < 5000
    assert "truncated" in result.stdout
