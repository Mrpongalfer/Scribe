"""Shared pytest fixtures: import path, tool discovery, stub LLM backend."""

import json
import os
import re
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# Make ruff / mypy / pytest / git discoverable for the gauntlet steps.
_venv_bin = str(Path(sys.executable).parent)
os.environ["PATH"] = _venv_bin + os.pathsep + os.environ.get("PATH", "")

from scribe.llm import LLMBackend  # noqa: E402


class StubBackend(LLMBackend):
    """Deterministic stand-in for a real LLM backend.

    Returns canned pytest code for test-generation prompts (tailored to the
    target module named in the prompt) and canned JSON findings for review
    prompts (json_mode=True).
    """

    name = "stub"

    def __init__(self, test_body: str = "assert add(2, 3) == 5") -> None:
        self._test_body = test_body

    @property
    def describe(self) -> str:
        return "stub"

    def generate(self, prompt, *, system=None, json_mode=False):  # noqa: ANN001, ANN201
        if json_mode:
            return json.dumps(
                [
                    {
                        "severity": "info",
                        "description": "stub review finding",
                        "location": "general",
                    }
                ]
            )
        match = re.search(r"Target File:\s*(\S+)", prompt)
        module = Path(match.group(1)).stem if match else "module_under_test"
        return (
            "```python\n"
            "import pytest\n"
            f"from {module} import add\n"
            "\n\n"
            "def test_add():\n"
            f"    {self._test_body}\n"
            "```\n"
        )


@pytest.fixture()
def stub_backend():
    return StubBackend()


def tools_available(*names):
    return all(shutil.which(n) for n in names)


needs_tools = pytest.mark.skipif(
    not tools_available("ruff", "mypy", "pytest"),
    reason="e2e gauntlet tests need ruff, mypy and pytest on PATH",
)
