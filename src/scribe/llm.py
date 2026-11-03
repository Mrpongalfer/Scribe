"""Pluggable LLM backends for scribe-gate.

The gauntlet talks to LLMs through the small :class:`LLMBackend` interface
(``generate(prompt, system=..., json_mode=...) -> str``). Two backends ship:

* :class:`OllamaBackend` — local Ollama, default ``http://localhost:11434``.
* :class:`OpenAICompatibleBackend` — any OpenAI-style ``/chat/completions``
  endpoint, configured via base URL + API key.

Backend selection: ``SCRIBE_LLM_BACKEND`` (``ollama`` | ``openai``),
``SCRIBE_LLM_MODEL``, ``SCRIBE_LLM_BASE_URL``, ``SCRIBE_LLM_API_KEY`` —
or the matching CLI flags, which take precedence.
"""

from __future__ import annotations

import abc
import json
import logging
import re
import time
from typing import Any

try:
    import httpx
except ImportError:  # pragma: no cover - handled with a clear error at runtime
    httpx = None  # type: ignore[assignment]

from .config import (
    DEFAULT_OLLAMA_BASE_URL,
    DEFAULT_OLLAMA_MODEL,
    DEFAULT_OPENAI_MODEL,
)
from .models import ScribeApiError, ScribeConfigurationError

logger = logging.getLogger("scribe.llm")

_VALID_SEVERITIES = {"critical", "high", "moderate", "low", "info"}
_REVIEW_FINDING_KEYS = ("severity", "description", "location")


# ---------------------------------------------------------------------------
# Backend interface
# ---------------------------------------------------------------------------
class LLMBackend(abc.ABC):
    """Minimal text-generation interface used by the gauntlet."""

    name: str = "base"

    @abc.abstractmethod
    def generate(
        self,
        prompt: str,
        *,
        system: str | None = None,
        json_mode: bool = False,
    ) -> str:
        """Return the model's raw text response for *prompt*."""
        raise NotImplementedError

    @property
    def describe(self) -> str:
        return f"{self.name}"


def _post_json(
    client: httpx.Client,
    url: str,
    payload: dict[str, Any],
    *,
    retries: int,
    retry_delay: float,
) -> dict[str, Any]:
    """POST JSON with bounded retries; raise ScribeApiError on final failure."""
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            response = client.post(url, json=payload)
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise ScribeApiError("LLM API returned a non-JSON-object response.")
            if data.get("error"):
                raise ScribeApiError(f"LLM API returned an error: {data['error']}")
            return data
        except ScribeApiError:
            raise
        except Exception as exc:  # network, timeout, bad JSON, HTTP error
            last_error = exc
            logger.warning(
                "LLM request attempt %d/%d failed: %s",
                attempt + 1,
                retries + 1,
                exc,
            )
        if attempt < retries:
            time.sleep(retry_delay)
    raise ScribeApiError(f"LLM request failed after {retries + 1} attempts: {last_error}")


# ---------------------------------------------------------------------------
# Ollama backend
# ---------------------------------------------------------------------------
class OllamaBackend(LLMBackend):
    """Local Ollama via ``POST {base_url}/api/generate``."""

    name = "ollama"

    def __init__(
        self,
        base_url: str = DEFAULT_OLLAMA_BASE_URL,
        model: str = DEFAULT_OLLAMA_MODEL,
        timeout: float = 180.0,
        retries: int = 2,
        retry_delay: float = 5.0,
    ) -> None:
        if httpx is None:
            raise ScribeApiError(
                "The 'httpx' package is required for LLM backends. "
                "Install it with: pip install scribe-gate"
            )
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout
        self._retries = retries
        self._retry_delay = retry_delay
        # trust_env=False: proxy env vars (often containing IPv6 literals
        # like [::1]) must never break client construction; localhost LLM
        # traffic should not be proxied anyway.
        self._client = httpx.Client(base_url=self._base_url, timeout=timeout, trust_env=False)
        logger.info("OllamaBackend ready: url=%s model=%s", self._base_url, self._model)

    @property
    def describe(self) -> str:
        return f"ollama(model={self._model}, url={self._base_url})"

    def generate(
        self,
        prompt: str,
        *,
        system: str | None = None,
        json_mode: bool = False,
    ) -> str:
        payload: dict[str, Any] = {
            "model": self._model,
            "prompt": prompt,
            "stream": False,
        }
        if system:
            payload["system"] = system
        if json_mode:
            payload["format"] = "json"
        data = _post_json(
            self._client,
            "/api/generate",
            payload,
            retries=self._retries,
            retry_delay=self._retry_delay,
        )
        if "response" not in data:
            raise ScribeApiError("Ollama response did not contain 'response'.")
        return str(data["response"])


# ---------------------------------------------------------------------------
# OpenAI-compatible backend
# ---------------------------------------------------------------------------
class OpenAICompatibleBackend(LLMBackend):
    """Any OpenAI-style ``/chat/completions`` endpoint."""

    name = "openai"

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str = DEFAULT_OPENAI_MODEL,
        timeout: float = 180.0,
        retries: int = 2,
        retry_delay: float = 5.0,
    ) -> None:
        if httpx is None:
            raise ScribeApiError(
                "The 'httpx' package is required for LLM backends. "
                "Install it with: pip install scribe-gate"
            )
        if not base_url:
            raise ScribeApiError(
                "OpenAI-compatible backend needs a base URL "
                "(SCRIBE_LLM_BASE_URL or --llm-base-url)."
            )
        if not api_key:
            raise ScribeApiError(
                "OpenAI-compatible backend needs an API key (SCRIBE_LLM_API_KEY env var)."
            )
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._retries = retries
        self._retry_delay = retry_delay
        self._client = httpx.Client(
            base_url=self._base_url,
            timeout=timeout,
            trust_env=False,
            headers={"Authorization": f"Bearer {api_key}"},
        )
        logger.info(
            "OpenAICompatibleBackend ready: url=%s model=%s",
            self._base_url,
            self._model,
        )

    @property
    def describe(self) -> str:
        return f"openai-compatible(model={self._model}, url={self._base_url})"

    def generate(
        self,
        prompt: str,
        *,
        system: str | None = None,
        json_mode: bool = False,
    ) -> str:
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        payload: dict[str, Any] = {"model": self._model, "messages": messages}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        data = _post_json(
            self._client,
            "/chat/completions",
            payload,
            retries=self._retries,
            retry_delay=self._retry_delay,
        )
        try:
            return str(data["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as exc:
            raise ScribeApiError(f"Unexpected chat-completions response shape: {exc}") from exc


# ---------------------------------------------------------------------------
# Backend factory
# ---------------------------------------------------------------------------
def build_backend(
    config: Any,
    *,
    backend: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
) -> LLMBackend:
    """Select and construct the configured LLM backend.

    Precedence: explicit arguments > config file > environment > defaults.
    """
    name = (backend or config.get_str("llm_backend") or "ollama").strip().lower()
    timeout = config.get_float("llm_request_timeout", 180.0)
    retries = config.get_int("llm_retries", 2)
    retry_delay = config.get_float("llm_retry_delay", 5.0)

    if name == "ollama":
        return OllamaBackend(
            base_url=base_url or config.get_str("llm_base_url") or DEFAULT_OLLAMA_BASE_URL,
            model=model or config.get_str("llm_model") or DEFAULT_OLLAMA_MODEL,
            timeout=timeout,
            retries=retries,
            retry_delay=retry_delay,
        )
    if name in ("openai", "openai-compatible"):
        return OpenAICompatibleBackend(
            base_url=base_url or config.get_str("llm_base_url"),
            api_key=config.get_str("llm_api_key"),
            model=model or config.get_str("llm_model") or DEFAULT_OPENAI_MODEL,
            timeout=timeout,
            retries=retries,
            retry_delay=retry_delay,
        )
    raise ScribeConfigurationError(f"Unknown LLM backend '{name}'. Choose 'ollama' or 'openai'.")


# ---------------------------------------------------------------------------
# High-level client: the two structured gauntlet calls
# ---------------------------------------------------------------------------
class LLMClient:
    """Structured test-generation and code-review calls over any backend."""

    def __init__(self, config: Any, backend: LLMBackend) -> None:
        self._logger = logging.getLogger("scribe.llm.client")
        self._config = config
        self._backend = backend

    @property
    def backend(self) -> LLMBackend:
        return self._backend

    # -- helpers ---------------------------------------------------------
    @staticmethod
    def _extract_code(response: str) -> str:
        python_blocks: list[str] = re.findall(
            r"```python\n(.*?)\n```", response, re.DOTALL | re.IGNORECASE
        )
        if python_blocks:
            if len(python_blocks) > 1:
                logger.warning("LLM returned %d python blocks; concatenating.", len(python_blocks))
            return "\n\n".join(block.strip() for block in python_blocks).strip()
        generic_blocks: list[str] = re.findall(
            r"```(?:[a-zA-Z0-9_.-]*)?\n(.*?)\n```", response, re.DOTALL
        )
        if generic_blocks:
            logger.warning("No ```python block found; using first fenced block.")
            return generic_blocks[0].strip()
        logger.warning("No fenced block found; treating whole response as code.")
        return response.strip()

    @staticmethod
    def _parse_findings(raw: str) -> list[dict[str, str]] | None:
        try:
            findings = json.loads(raw)
        except json.JSONDecodeError:
            # Tolerate a single object instead of a list, and stray fences.
            cleaned = raw.strip()
            if cleaned.startswith("```"):
                cleaned = re.sub(r"^```[a-zA-Z]*\n|\n```$", "", cleaned).strip()
            try:
                findings = json.loads(cleaned)
            except json.JSONDecodeError:
                logger.error("Could not parse review findings as JSON.")
                return None
        if isinstance(findings, dict):
            findings = [findings]
        if not isinstance(findings, list):
            logger.error("Review findings were not a JSON list.")
            return None
        valid: list[dict[str, str]] = []
        for item in findings:
            if isinstance(item, dict) and all(key in item for key in _REVIEW_FINDING_KEYS):
                severity = str(item["severity"]).strip().lower()
                valid.append(
                    {
                        "severity": severity if severity in _VALID_SEVERITIES else "info",
                        "description": str(item["description"]).strip(),
                        "location": str(item["location"]).strip(),
                    }
                )
            else:
                logger.warning("Skipping malformed review finding: %r", item)
        return valid

    # -- gauntlet calls --------------------------------------------------
    def generate_tests(
        self, code: str, file_name: str, module_name: str, signatures: str
    ) -> str | None:
        """Ask the LLM for pytest code; return it or None on any failure."""
        self._logger.info("Requesting AI test generation for '%s'.", file_name)
        template = self._config.get_str("test_generation_prompt_template")
        code_snippet = code[:7000] + ("..." if len(code) > 7000 else "")
        sig_snippet = signatures[:1000] + ("..." if len(signatures) > 1000 else "")
        try:
            prompt = template.format(
                code_content=code_snippet,
                target_file_path=file_name,
                module_name=module_name,
                signatures=sig_snippet,
            )
        except KeyError as exc:
            raise ScribeConfigurationError(
                f"test_generation_prompt_template is missing placeholder {exc}"
            ) from exc
        try:
            raw = self._backend.generate(prompt)
            if not raw.strip():
                self._logger.warning("LLM returned empty test response.")
                return None
            extracted = self._extract_code(raw)
            if not extracted:
                self._logger.warning("Could not extract test code from response.")
                return None
            compile(extracted, "<scribe-generated-tests>", "exec")
            self._logger.info("Generated test code compiles cleanly.")
            return extracted
        except ScribeApiError as exc:
            self._logger.error("LLM API error during test generation: %s", exc)
            return None
        except SyntaxError as exc:
            self._logger.error("Generated test code has a syntax error: %s", exc)
            return None
        except Exception:  # noqa: BLE001 - never let the LLM break the gate
            self._logger.error("Unexpected error during test generation.", exc_info=True)
            return None

    def generate_review(self, code: str, file_name: str) -> list[dict[str, str]] | None:
        """Ask the LLM for structured review findings; None on failure."""
        self._logger.info("Requesting AI code review for '%s'.", file_name)
        template = self._config.get_str("review_prompt_template")
        code_snippet = code[:8000] + ("..." if len(code) > 8000 else "")
        try:
            prompt = template.format(code_content=code_snippet, target_file_path=file_name)
        except KeyError as exc:
            raise ScribeConfigurationError(
                f"review_prompt_template is missing placeholder {exc}"
            ) from exc
        try:
            raw = self._backend.generate(prompt, json_mode=True)
            if not raw.strip():
                self._logger.warning("LLM returned empty review response.")
                return []
            findings = self._parse_findings(raw)
            if findings is None:
                return None
            self._logger.info("Parsed %d review findings.", len(findings))
            return findings
        except ScribeApiError as exc:
            self._logger.error("LLM API error during code review: %s", exc)
            return None
        except Exception:  # noqa: BLE001 - never let the LLM break the gate
            self._logger.error("Unexpected error during code review.", exc_info=True)
            return None
