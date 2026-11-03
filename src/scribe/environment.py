"""Isolated virtualenv management for the validation gauntlet."""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from .config import DEFAULT_TOOL_TIMEOUT
from .models import ScribeEnvironmentError, ScribeToolError
from .runner import ToolRunner

if TYPE_CHECKING:  # pragma: no cover
    from .config import ScribeConfig

VENV_DIR_NAME = ".venv"


class EnvironmentManager:
    """Create and maintain the isolated venv the gauntlet tools run in."""

    def __init__(
        self,
        target_dir: Path,
        tool_runner: ToolRunner,
        config: ScribeConfig,
    ) -> None:
        self._logger = logging.getLogger("scribe.environment")
        self._target_dir = target_dir.resolve()
        self._tool_runner = tool_runner
        self._config = config
        self._venv_path = self._target_dir / VENV_DIR_NAME
        self._venv_python_path: Path | None = None
        self._pip_path: Path | None = None

    @property
    def venv_path(self) -> Path:
        return self._venv_path

    @property
    def python_executable(self) -> Path | None:
        return self._venv_python_path

    @property
    def pip_executable(self) -> Path | None:
        return self._pip_path

    def find_venv_executable(self, name: str) -> Path | None:
        if not self._venv_path.is_dir():
            return None
        bin_dir = self._venv_path / ("Scripts" if sys.platform == "win32" else "bin")
        suffixes = ("", ".exe") if sys.platform == "win32" else ("",)
        for suffix in suffixes:
            candidate = bin_dir / f"{name}{suffix}"
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate.resolve()
        return None

    def setup_venv(self) -> None:
        """Create the venv if missing; idempotent on repeat runs."""
        self._logger.info("Setting up venv at %s", self._venv_path)
        timeout = self._config.get_float("default_tool_timeout", DEFAULT_TOOL_TIMEOUT)
        if not self._venv_path.exists():
            self._logger.info("Creating new virtualenv...")
            try:
                result = self._tool_runner.run_tool(
                    [sys.executable, "-m", "venv", str(self._venv_path)],
                    cwd=self._target_dir,
                    timeout=timeout,
                )
            except ScribeToolError as exc:
                raise ScribeEnvironmentError(f"Could not launch venv creation: {exc}") from exc
            if result.returncode != 0:
                raise ScribeEnvironmentError(
                    f"venv creation failed (RC={result.returncode}): {result.stderr.strip()}"
                )
            self._logger.info("Virtualenv created.")
        else:
            self._logger.info("Reusing existing virtualenv.")

        self._venv_python_path = self.find_venv_executable("python")
        self._pip_path = self.find_venv_executable("pip")
        if not self._venv_python_path or not self._pip_path:
            raise ScribeEnvironmentError(f"python/pip not found inside venv {self._venv_path}")

        # Best-effort pip bootstrap; never fatal.
        try:
            self._logger.info("Upgrading pip/setuptools/wheel in venv...")
            result = self._tool_runner.run_tool(
                [
                    str(self._pip_path),
                    "install",
                    "--disable-pip-version-check",
                    "--upgrade",
                    "pip",
                    "setuptools",
                    "wheel",
                ],
                cwd=self._target_dir,
                venv_path=self._venv_path,
                timeout=timeout,
            )
            if result.returncode != 0:
                self._logger.warning(
                    "pip bootstrap failed (RC=%s): %s",
                    result.returncode,
                    result.stderr.strip()[:300],
                )
            else:
                self._logger.info("pip bootstrap complete.")
        except ScribeToolError as exc:
            self._logger.warning("pip bootstrap raised: %s", exc)

    def install_dependencies(self) -> None:
        """Install the target project's deps into the venv, if declared."""
        if not self._pip_path:
            raise ScribeEnvironmentError("pip is not available in the venv.")
        timeout = self._config.get_float("default_tool_timeout", DEFAULT_TOOL_TIMEOUT)
        pyproject = self._target_dir / "pyproject.toml"
        requirements = self._target_dir / "requirements.txt"

        if pyproject.is_file():
            self._logger.info("Installing from pyproject.toml...")
            for group in ("[dev,test,lint,format]", "[dev,test,lint]", "[dev,test]", "[dev]", ""):
                cmd = [
                    str(self._pip_path),
                    "install",
                    "--disable-pip-version-check",
                    "-e",
                    f".{group}",
                ]
                self._logger.debug("Trying: %s", " ".join(shlex.quote(s) for s in cmd))
                try:
                    result = self._tool_runner.run_tool(
                        cmd, self._target_dir, self._venv_path, timeout=timeout
                    )
                except ScribeToolError as exc:
                    self._logger.warning("Install attempt for group '%s' raised: %s", group, exc)
                    continue
                if result.returncode == 0:
                    self._logger.info("Installed editable group '%s'.", group or "(base)")
                    return
                self._logger.warning("Install group '%s' failed (RC=%s).", group, result.returncode)
            raise ScribeEnvironmentError("All pyproject.toml install attempts failed.")
        if requirements.is_file():
            self._logger.info("Installing from requirements.txt...")
            try:
                result = self._tool_runner.run_tool(
                    [
                        str(self._pip_path),
                        "install",
                        "--disable-pip-version-check",
                        "-r",
                        str(requirements),
                    ],
                    self._target_dir,
                    self._venv_path,
                    timeout=timeout,
                )
            except ScribeToolError as exc:
                raise ScribeEnvironmentError(f"requirements.txt install raised: {exc}") from exc
            if result.returncode != 0:
                raise ScribeEnvironmentError(
                    f"requirements.txt install failed (RC={result.returncode}): "
                    f"{result.stderr.strip()[:500]}"
                )
            self._logger.info("Installed from requirements.txt.")
            return
        self._logger.info("No pyproject.toml or requirements.txt; skipping install.")

    def run_pip_audit(self) -> subprocess.CompletedProcess:
        """Run pip-audit inside the venv; best-effort, never raises."""
        timeout = self._config.get_float("default_tool_timeout", DEFAULT_TOOL_TIMEOUT)
        pip = self._pip_path
        if pip is None:
            raise ScribeEnvironmentError("pip is not available in the venv.")
        try:
            installed = self._tool_runner.run_tool(
                [str(pip), "install", "--disable-pip-version-check", "pip-audit"],
                cwd=self._target_dir,
                venv_path=self._venv_path,
                timeout=timeout,
            )
            if installed.returncode != 0:
                self._logger.warning("Could not install pip-audit; audit may fail.")
        except ScribeToolError as exc:
            self._logger.warning("pip-audit install raised: %s", exc)
        return self._tool_runner.run_tool(
            [str(pip), "audit", "--format", "json", "--progress-spinner", "off"],
            cwd=self._target_dir,
            venv_path=self._venv_path,
            timeout=timeout,
        )
