"""End-to-end gauntlet tests against fixture files (LLM stubbed).

Runs the real pipeline — validate, copy, ruff format check, ruff lint,
mypy, signature extraction, stubbed test generation, pytest execution,
stubbed review — and asserts the expected per-step statuses for each
fixture: clean passes, lint-broken fails lint, type-broken fails mypy,
logic-broken fails the generated tests.
"""

import json
from pathlib import Path

from scribe.agent import ScribeAgent
from scribe.cli import parse_args
from tests.conftest import StubBackend, needs_tools

FIXTURES = Path(__file__).resolve().parent / "fixtures"

E2E_STEPS = (
    'validation_steps = ["validate_inputs", "apply_code", "format_code", '
    '"lint_code", "type_check", "extract_signatures", "generate_tests", '
    '"save_tests", "execute_tests", "review_code", "generate_report"]\n'
)


def _run_gauntlet(tmp_path, monkeypatch, capsys, fixture_name):
    target = tmp_path / "proj"
    target.mkdir(exist_ok=True)
    (target / "scribe.toml").write_text(E2E_STEPS, encoding="utf-8")
    source = FIXTURES / fixture_name
    monkeypatch.chdir(target)
    args = parse_args(
        [
            ".",
            "--source-file",
            str(source),
            "--target-file",
            "module_under_test.py",
            "--config-file",
            "scribe.toml",
            "--log-level",
            "CRITICAL",
        ]
    )
    agent = ScribeAgent(args, llm_backend=StubBackend())
    exit_code = agent.run()
    report = json.loads(capsys.readouterr().out)
    statuses = {step["name"]: step["status"] for step in report["steps"]}
    return exit_code, report, statuses


@needs_tools
def test_clean_fixture_passes(tmp_path, monkeypatch, capsys):
    exit_code, report, statuses = _run_gauntlet(tmp_path, monkeypatch, capsys, "clean_module.py")
    assert exit_code == 0
    assert report["overall_status"] == "SUCCESS"
    assert statuses["format_code"] == "SUCCESS"
    assert statuses["lint_code"] == "SUCCESS"
    assert statuses["type_check"] == "SUCCESS"
    assert statuses["execute_tests"] == "SUCCESS"
    assert statuses["review_code"] == "ADVISORY"
    assert report["commit_attempted"] is False


@needs_tools
def test_lint_broken_fixture_fails_lint(tmp_path, monkeypatch, capsys):
    exit_code, report, statuses = _run_gauntlet(
        tmp_path, monkeypatch, capsys, "lint_broken_module.py"
    )
    assert exit_code == 1
    assert report["overall_status"] == "FAILURE"
    assert statuses["format_code"] == "SUCCESS"
    assert statuses["lint_code"] == "FAILURE"
    # Pipeline halts at the first FAILURE; later steps never run.
    assert "type_check" not in statuses


@needs_tools
def test_type_broken_fixture_fails_typecheck(tmp_path, monkeypatch, capsys):
    exit_code, report, statuses = _run_gauntlet(
        tmp_path, monkeypatch, capsys, "type_broken_module.py"
    )
    assert exit_code == 1
    assert report["overall_status"] == "FAILURE"
    assert statuses["lint_code"] == "SUCCESS"
    assert statuses["type_check"] == "FAILURE"


@needs_tools
def test_logic_broken_fixture_fails_generated_tests(tmp_path, monkeypatch, capsys):
    exit_code, report, statuses = _run_gauntlet(
        tmp_path, monkeypatch, capsys, "logic_broken_module.py"
    )
    assert exit_code == 1
    assert report["overall_status"] == "FAILURE"
    assert statuses["lint_code"] == "SUCCESS"
    assert statuses["type_check"] == "SUCCESS"
    assert statuses["generate_tests"] == "SUCCESS"
    assert statuses["execute_tests"] == "FAILURE"
    assert report["test_results_summary"]["return_code"] != 0
