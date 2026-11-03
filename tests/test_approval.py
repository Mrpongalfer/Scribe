"""Unit tests: the human commit-approval gate."""

import io
import sys

from scribe.agent import ScribeAgent
from scribe.cli import parse_args


def _agent(tmp_path, monkeypatch, extra_args=(), stub=None):
    from tests.conftest import StubBackend

    monkeypatch.chdir(tmp_path)
    args = parse_args([".", "--source-file", "a.py", "--target-file", "b.py", *extra_args])
    return ScribeAgent(args, llm_backend=stub or StubBackend())


def test_yes_flag_approves_without_prompt(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch, ["--commit", "--yes"])
    assert agent.request_commit_approval() is True


def test_non_tty_without_yes_declines_safely(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch, ["--commit"])

    class NonTty(io.StringIO):
        def isatty(self):  # noqa: ANN201
            return False

    monkeypatch.setattr(sys, "stdin", NonTty())
    assert agent.request_commit_approval() is False


def test_interactive_yes_approves(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch, ["--commit"])

    class Tty(io.StringIO):
        def isatty(self):  # noqa: ANN201
            return True

    monkeypatch.setattr(sys, "stdin", Tty("y\n"))
    assert agent.request_commit_approval() is True


def test_interactive_empty_declines(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch, ["--commit"])

    class Tty(io.StringIO):
        def isatty(self):  # noqa: ANN201
            return True

    monkeypatch.setattr(sys, "stdin", Tty("\n"))
    assert agent.request_commit_approval() is False


def test_commit_step_skipped_by_default(tmp_path, monkeypatch, capsys):
    """Without --commit, the commit step is SKIPPED and never attempted."""

    target = tmp_path / "proj"
    target.mkdir()
    (target / "a.py").write_text("x = 1\n", encoding="utf-8")
    (target / "scribe.toml").write_text(
        'validation_steps = ["validate_inputs", "apply_code", "commit_changes", '
        '"generate_report"]\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(target)
    args = parse_args(
        [
            ".",
            "--source-file",
            "a.py",
            "--target-file",
            "b.py",
            "--config-file",
            "scribe.toml",
            "--log-level",
            "CRITICAL",
        ]
    )
    from tests.conftest import StubBackend

    agent = ScribeAgent(args, llm_backend=StubBackend())
    assert agent.run() == 0
    report = capsys.readouterr().out
    import json

    parsed = json.loads(report)
    commit_step = next(s for s in parsed["steps"] if s["name"] == "commit_changes")
    assert commit_step["status"] == "SKIPPED"
    assert parsed["commit_attempted"] is False
