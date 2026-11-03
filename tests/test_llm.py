"""Unit tests: backend selection and the structured LLM calls."""

import pytest

from scribe.config import ScribeConfig
from scribe.llm import (
    LLMClient,
    OllamaBackend,
    OpenAICompatibleBackend,
    build_backend,
)
from scribe.models import ScribeApiError, ScribeConfigurationError


@pytest.fixture()
def config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SCRIBE_LLM_BACKEND", raising=False)
    monkeypatch.delenv("SCRIBE_LLM_API_KEY", raising=False)
    return ScribeConfig()


def test_ollama_is_default_backend(config):
    backend = build_backend(config)
    assert isinstance(backend, OllamaBackend)
    assert "localhost:11434" in backend.describe


def test_openai_backend_needs_api_key(config):
    config._config["llm_backend"] = "openai"
    config._config["llm_base_url"] = "https://api.example.com/v1"
    with pytest.raises(ScribeApiError, match="API key"):
        build_backend(config)


def test_openai_backend_builds_with_key(config):
    config._config["llm_backend"] = "openai"
    config._config["llm_base_url"] = "https://api.example.com/v1"
    config._config["llm_api_key"] = "sk-test"
    config._config["llm_model"] = "test-model"
    backend = build_backend(config)
    assert isinstance(backend, OpenAICompatibleBackend)
    assert "test-model" in backend.describe


def test_unknown_backend_name_rejected(config):
    with pytest.raises(ScribeConfigurationError, match="Unknown LLM backend"):
        build_backend(config, backend="carrier-pigeon")


def test_generate_tests_returns_code(config, stub_backend):
    client = LLMClient(config, stub_backend)
    code = client.generate_tests("def add(a, b): return a + b", "mod.py", "mod", "")
    assert code is not None
    assert "def test_add" in code
    assert "from mod import add" in code


def test_generate_tests_returns_none_on_prose(config):
    class ProseBackend(OllamaBackend):
        def generate(self, prompt, *, system=None, json_mode=False):  # noqa: ANN001, ANN201
            return "Here are some thoughts about testing. No code, sorry."

    client = LLMClient(config, ProseBackend.__new__(ProseBackend))
    # Bypass __init__ (which needs httpx); the stub overrides generate anyway.
    assert client.generate_tests("x = 1", "m.py", "m", "") is None


def test_generate_review_parses_findings(config, stub_backend):
    client = LLMClient(config, stub_backend)
    findings = client.generate_review("x = 1", "m.py")
    assert findings == [
        {"severity": "info", "description": "stub review finding", "location": "general"}
    ]


def test_generate_review_sanitizes_bad_findings(config):
    class MessyBackend(OllamaBackend):
        def generate(self, prompt, *, system=None, json_mode=False):  # noqa: ANN001, ANN201
            import json as _json

            return _json.dumps(
                [
                    {"severity": "CRITICAL", "description": "bad", "location": "f()"},
                    {"severity": "nonsense", "description": "weird", "location": "g()"},
                    {"not": "a finding"},
                ]
            )

    backend = MessyBackend.__new__(MessyBackend)
    client = LLMClient(config, backend)
    findings = client.generate_review("x = 1", "m.py")
    assert findings is not None
    assert findings[0]["severity"] == "critical"
    assert findings[1]["severity"] == "info"  # unknown severity demoted
    assert len(findings) == 2  # malformed entry dropped
