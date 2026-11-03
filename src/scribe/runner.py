"""Subprocess execution for external gauntlet tools (ruff, mypy, pytest, git)."""

from __future__ import annotations

import logging
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from .config import DEFAULT_TOOL_TIMEOUT
from .models import ScribeToolError as _ScribeToolError

if TYPE_CHECKING:  # pragma: no cover - config does not import runner, so no cycle
    from .config import ScribeConfig


class ToolRunner:
    """Find and run external executables with timeouts and output caps."""

    def __init__(self, config: ScribeConfig) -> None:
        self._logger = logging.getLogger("scribe.runner")
        self._config = config
        self._max_output_chars = config.get_int("max_tool_output_chars", 200_000)

    # -- executable discovery -------------------------------------------
    def _find_executable(self, executable_name: str, venv_path: Path | None = None) -> Path:
        configured = self._config.get("tool_paths", {})
        if isinstance(configured, dict) and executable_name in configured:
            custom = Path(str(configured[executable_name])).expanduser()
            if custom.is_file() and os.access(custom, os.X_OK):
                self._logger.debug("Using configured path for '%s': %s", executable_name, custom)
                return custom.resolve()
            self._logger.warning(
                "Configured path for '%s' (%s) is not executable; falling back.",
                executable_name,
                custom,
            )
        if venv_path:
            resolved = venv_path.resolve()
            if resolved.is_dir():
                bin_dir = resolved / ("Scripts" if sys.platform == "win32" else "bin")
                suffixes = ("", ".exe") if sys.platform == "win32" else ("",)
                for suffix in suffixes:
                    candidate = bin_dir / f"{executable_name}{suffix}"
                    if candidate.is_file() and os.access(candidate, os.X_OK):
                        self._logger.debug("Found '%s' in venv: %s", executable_name, candidate)
                        return candidate.resolve()
        system_path = shutil.which(executable_name)
        if system_path:
            self._logger.debug("Found '%s' on PATH: %s", executable_name, system_path)
            return Path(system_path).resolve()
        raise _ScribeToolError(f"Executable '{executable_name}' not found.")

    # -- execution -------------------------------------------------------
    def _cap(self, text: str | None) -> str:
        if not text:
            return ""
        if len(text) > self._max_output_chars:
            half = self._max_output_chars // 2
            return (
                text[:half]
                + f"\n... [truncated {len(text) - self._max_output_chars} chars] ...\n"
                + text[-half:]
            )
        return text

    def run_tool(
        self,
        command_args: list[str],
        cwd: Path,
        venv_path: Path | None = None,
        env_vars: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess:
        """Run *command_args*; never raises on non-zero exit.

        Raises :class:`ScribeToolError` only when the executable cannot be
        found or the OS refuses to spawn it. Timeouts are reported as a
        ``CompletedProcess`` with ``returncode == -1``.
        """
        if not command_args:
            raise _ScribeToolError("run_tool called with empty command_args.")
        exec_name = command_args[0]
        try:
            exec_path = self._find_executable(exec_name, venv_path)
        except _ScribeToolError as exc:
            raise _ScribeToolError(
                f"Executable '{exec_name}' not found. Is it installed and on PATH? "
                f"(Or set tool_paths.{exec_name} in scribe.toml.)"
            ) from exc

        full_cmd = [str(exec_path)] + command_args[1:]
        cmd_str = " ".join(shlex.quote(str(a)) for a in full_cmd)
        self._logger.info("Running: %s (cwd=%s)", cmd_str, cwd)

        proc_env = os.environ.copy()
        if venv_path:
            resolved = venv_path.resolve()
            proc_env["VIRTUAL_ENV"] = str(resolved)
            venv_bin = resolved / ("Scripts" if sys.platform == "win32" else "bin")
            proc_env["PATH"] = f"{venv_bin}{os.pathsep}{proc_env.get('PATH', '')}"
            proc_env.pop("PYTHONHOME", None)
            proc_env.pop("PYTHONPATH", None)
        if env_vars:
            proc_env.update(env_vars)

        effective_timeout: float | None = (
            timeout
            if timeout is not None
            else self._config.get_float("default_tool_timeout", DEFAULT_TOOL_TIMEOUT)
        )
        if effective_timeout is not None and effective_timeout <= 0:
            effective_timeout = None

        try:
            result = subprocess.run(
                full_cmd,
                cwd=cwd,
                env=proc_env,
                capture_output=True,
                check=False,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=effective_timeout,
            )
            result.stdout = self._cap(result.stdout)
            result.stderr = self._cap(result.stderr)
            self._logger.debug(
                "Tool '%s' finished RC=%s (timeout=%ss).",
                exec_name,
                result.returncode,
                effective_timeout if effective_timeout else "none",
            )
            return result
        except subprocess.TimeoutExpired as exc:
            self._logger.error("Tool '%s' timed out after %ss.", exec_name, effective_timeout)
            stdout = (
                exc.stdout.decode("utf-8", "replace")
                if isinstance(exc.stdout, bytes)
                else (exc.stdout or "")
            )
            stderr = (
                exc.stderr.decode("utf-8", "replace")
                if isinstance(exc.stderr, bytes)
                else (exc.stderr or "")
            )
            note = f"TimeoutExpired: command exceeded {effective_timeout}s."
            return subprocess.CompletedProcess(
                args=exc.cmd or full_cmd,
                returncode=-1,
                stdout=self._cap(stdout),
                stderr=self._cap(f"{note}\n{stderr}".strip()),
            )
        except OSError as exc:
            raise _ScribeToolError(f"OS error executing '{exec_name}': {exc}") from exc
