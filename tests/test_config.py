"""Unit tests: configuration loading, validation, typed accessors."""

import pytest

from scribe.config import ScribeConfig
from scribe.models import ScribeConfigurationError


def test_defaults_load_without_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SCRIBE_LLM_BACKEND", raising=False)
    config = ScribeConfig()
    assert config.get_str("llm_backend") == "ollama"
    assert config.get_bool("fail_on_lint_critical") is True
    steps = config.get_list("validation_steps")
    assert steps[0] == "validate_inputs"
    assert steps[-1] == "generate_report"
    assert config.config_path is None


def test_toml_file_is_loaded_and_merged(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "scribe.toml").write_text(
        'fail_on_mypy_error = false\nllm_model = "qwen:7b"\n', encoding="utf-8"
    )
    config = ScribeConfig()
    assert config.get_bool("fail_on_mypy_error") is False
    assert config.get_str("llm_model") == "qwen:7b"
    # Untouched defaults survive the merge.
    assert config.get_bool("fail_on_lint_critical") is True
    assert config.config_path is not None


def test_invalid_toml_raises(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "scribe.toml").write_text("this is = [not valid", encoding="utf-8")
    with pytest.raises(ScribeConfigurationError):
        ScribeConfig()


def test_unknown_backend_raises(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "scribe.toml").write_text('llm_backend = "telepathy"\n', encoding="utf-8")
    with pytest.raises(ScribeConfigurationError):
        ScribeConfig()


def test_env_overrides_backend(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SCRIBE_LLM_BACKEND", "openai")
    monkeypatch.setenv("SCRIBE_LLM_MODEL", "gpt-4o")
    config = ScribeConfig()
    assert config.get_str("llm_backend") == "openai"
    assert config.get_str("llm_model") == "gpt-4o"


def test_typed_getters_handle_garbage(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = ScribeConfig()
    assert config.get_bool("missing", True) is True
    assert config.get_float("missing", 1.5) == 1.5
    assert config.get_int("missing", 3) == 3
    assert config.get_list("missing") == []
    assert config.get_str("missing", "dflt") == "dflt"
    # Non-list value for a list getter falls back safely.
    config._config["validation_steps"] = "not-a-list"
    assert config.get_list("validation_steps") == []
