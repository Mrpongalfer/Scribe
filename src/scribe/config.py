"""Configuration management for scribe-gate.

Loads ``.scribe.toml`` (project-local, then CWD, then explicit override),
merges it over sane defaults, and exposes typed accessors. Environment
variables override LLM settings so secrets never have to live in the file.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any

from .models import ScribeConfigurationError

# Guarded TOML import: stdlib on 3.11+, tomli backport below that.
if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised only on old interpreters
    try:
        import tomli as tomllib
    except ImportError:
        tomllib = None  # type: ignore[assignment]

DEFAULT_CONFIG_FILENAME = "scribe.toml"
LEGACY_CONFIG_FILENAME = ".scribe.toml"
DEFAULT_TOOL_TIMEOUT = 180.0
DEFAULT_LLM_BACKEND = "ollama"
DEFAULT_OLLAMA_BASE_URL = "http://localhost:11434"
DEFAULT_OLLAMA_MODEL = "gemma:2b"
DEFAULT_OPENAI_MODEL = "gpt-4o-mini"

TEST_GENERATION_PROMPT_TEMPLATE = (
    "You are an expert Python test generation assistant. Generate concise and effective "
    "pytest unit tests for the provided code. Focus on testing the public API based on the "
    "function and class signatures.\n\n"
    "The code under test lives in the module '{module_name}' (file: {target_file_path}). "
    "Your generated test code MUST import from that module, e.g. "
    "'from {module_name} import <name>'.\n\n"
    "Rules:\n"
    "- Output ONLY Python code inside a single ```python ... ``` block. No prose.\n"
    "- Begin the code with 'import pytest' and the needed imports from the target module.\n"
    "- Keep tests deterministic; no network, no sleeps, no randomness.\n\n"
    "Target File: {target_file_path}\n"
    "Module: {module_name}\n"
    "Code:\n```python\n{code_content}\n```\n"
    "Key Signatures for Test Focus:\n{signatures}\n\n"
    "Generated Pytest Code:"
)

REVIEW_PROMPT_TEMPLATE = (
    "You are an expert Python code reviewer. Review the following code for bugs, "
    "security issues, and correctness problems. (Style nits are out of scope; a "
    "formatter and linter already ran.)\n\n"
    "Return your findings as a raw JSON list of objects. Each object MUST have:\n"
    "- 'severity': one of 'critical', 'high', 'moderate', 'low', 'info'\n"
    "- 'description': a clear explanation of the issue\n"
    "- 'location': function/class name or line number, or 'general'\n\n"
    "Return ONLY the JSON list, no surrounding text or markdown.\n\n"
    "Target File: {target_file_path}\n"
    "Code:\n```python\n{code_content}\n```\n\n"
    "Review Findings:"
)

DEFAULT_VALIDATION_STEPS = [
    "validate_inputs",
    "setup_environment",
    "install_deps",
    "audit_deps",
    "apply_code",
    "format_code",
    "lint_code",
    "type_check",
    "extract_signatures",
    "generate_tests",
    "save_tests",
    "execute_tests",
    "review_code",
    "run_precommit",
    "commit_changes",
    "generate_report",
]


class ScribeConfig:
    """Load, validate, and serve scribe-gate configuration."""

    def __init__(
        self,
        config_path_override: str | None = None,
        project_dir_for_local_config: Path | None = None,
    ) -> None:
        self._logger = logging.getLogger("scribe.config")
        self._config_path: Path | None = self._locate_config(
            config_path_override, project_dir_for_local_config
        )
        self._config: dict[str, Any] = self._load_and_validate()

    # ------------------------------------------------------------------
    # Discovery / loading
    # ------------------------------------------------------------------
    @staticmethod
    def _locate_config(override: str | None, project_dir: Path | None) -> Path | None:
        candidates: list[Path] = []
        if override:
            candidates.append(Path(override).expanduser())
        if project_dir:
            base = project_dir.resolve()
            candidates.append(base / DEFAULT_CONFIG_FILENAME)
            candidates.append(base / LEGACY_CONFIG_FILENAME)
        cwd = Path.cwd()
        candidates.append(cwd / DEFAULT_CONFIG_FILENAME)
        candidates.append(cwd / LEGACY_CONFIG_FILENAME)
        for candidate in candidates:
            if candidate.is_file():
                return candidate.resolve()
        return None

    def _default_config(self) -> dict[str, Any]:
        return {
            "allowed_target_bases": [
                str(Path.home()),
                "/tmp",
                str(Path.cwd().resolve()),
            ],
            "fail_on_lint_critical": True,
            "fail_on_mypy_error": True,
            "fail_on_test_failure": True,
            "fail_on_audit_severity": "high",
            "llm_backend": os.environ.get("SCRIBE_LLM_BACKEND", DEFAULT_LLM_BACKEND),
            "llm_model": os.environ.get("SCRIBE_LLM_MODEL", ""),
            "llm_base_url": os.environ.get("SCRIBE_LLM_BASE_URL", ""),
            "llm_api_key": os.environ.get("SCRIBE_LLM_API_KEY", ""),
            "llm_request_timeout": 180.0,
            "llm_retries": 2,
            "llm_retry_delay": 5.0,
            "default_tool_timeout": DEFAULT_TOOL_TIMEOUT,
            "max_tool_output_chars": 200_000,
            "commit_message_template": "chore(scribe): validate {target_file}",
            "test_generation_prompt_template": TEST_GENERATION_PROMPT_TEMPLATE,
            "review_prompt_template": REVIEW_PROMPT_TEMPLATE,
            "validation_steps": list(DEFAULT_VALIDATION_STEPS),
            "tool_paths": {},
        }

    def _load_and_validate(self) -> dict[str, Any]:
        config = self._default_config()
        if self._config_path is not None:
            if tomllib is None:
                raise ScribeConfigurationError(
                    "A config file was found but no TOML parser is available. "
                    "Install 'tomli' (Python < 3.11)."
                )
            try:
                with open(self._config_path, "rb") as handle:
                    loaded = tomllib.load(handle)
            except OSError as exc:
                raise ScribeConfigurationError(
                    f"Cannot read config file '{self._config_path}': {exc}"
                ) from exc
            except Exception as exc:  # tomllib.TOMLDecodeError
                raise ScribeConfigurationError(
                    f"Cannot parse config file '{self._config_path}': {exc}"
                ) from exc
            if not isinstance(loaded, dict):
                raise ScribeConfigurationError(
                    f"Config file '{self._config_path}' must contain a TOML table."
                )
            config = self._merge(config, loaded)
            self._logger.info("Loaded config from %s", self._config_path)
        else:
            self._logger.info("No scribe.toml found; using defaults.")
        self._validate(config)
        return config

    @staticmethod
    def _merge(base: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
        merged = dict(base)
        for key, value in updates.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = ScribeConfig._merge(merged[key], value)
            else:
                merged[key] = value
        return merged

    @staticmethod
    def _validate(config: dict[str, Any]) -> None:
        if not isinstance(config.get("allowed_target_bases"), list):
            raise ScribeConfigurationError("'allowed_target_bases' must be a list.")
        steps = config.get("validation_steps")
        if not isinstance(steps, list) or not steps or not all(isinstance(s, str) for s in steps):
            raise ScribeConfigurationError(
                "'validation_steps' must be a non-empty list of step names."
            )
        for key in ("default_tool_timeout", "llm_request_timeout", "llm_retry_delay"):
            try:
                if float(config.get(key, 1)) <= 0:
                    raise ScribeConfigurationError(f"'{key}' must be positive.")
            except (TypeError, ValueError) as exc:
                raise ScribeConfigurationError(f"'{key}' must be a number.") from exc
        backend = str(config.get("llm_backend", DEFAULT_LLM_BACKEND)).lower()
        if backend not in ("ollama", "openai", "openai-compatible"):
            raise ScribeConfigurationError(
                f"'llm_backend' must be 'ollama' or 'openai'/'openai-compatible', got '{backend}'."
            )

    # ------------------------------------------------------------------
    # Typed accessors
    # ------------------------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        return self._config.get(key, default)

    def get_list(self, key: str, default: list[Any] | None = None) -> list[Any]:
        value = self._config.get(key, default if default is not None else [])
        return value if isinstance(value, list) else (default or [])

    def get_str(self, key: str, default: str = "") -> str:
        value = self._config.get(key, default)
        return str(value) if value is not None else default

    def get_bool(self, key: str, default: bool = False) -> bool:
        value = self._config.get(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ("true", "1", "yes", "on")
        return default

    def get_float(self, key: str, default: float = 0.0) -> float:
        value = self._config.get(key, default)
        try:
            return float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            self._logger.warning(
                "Config key '%s' value %r is not a float; using default %r.",
                key,
                value,
                default,
            )
            return default

    def get_int(self, key: str, default: int = 0) -> int:
        value = self._config.get(key, default)
        try:
            return int(float(value))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            self._logger.warning(
                "Config key '%s' value %r is not an int; using default %r.",
                key,
                value,
                default,
            )
            return default

    @property
    def config_path(self) -> Path | None:
        return self._config_path
