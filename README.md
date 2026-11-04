# scribe-gate

**A validation gauntlet and trust gate for agent-written Python code.**

Scribe sits between an agent writing code and that code being committed. It runs
a deterministic gauntlet — isolated environment, formatting, lint, type check,
AI-generated tests, AI code review, pre-commit hooks — and emits a structured
JSON report. It **validates; it never rewrites your code.** And it never commits
without explicit operator approval.

```
agent writes code ──▶ SCRIBE ──▶ ✅ report + optional commit
                              ──▶ ❌ report, pipeline halts
```
AI coding agents write code fast — and wrong in ways that look right. Existing
guardrails either auto-fix what they find (hiding the problem from you) or run
a single linter and call it done. Scribe is the gate between the agent and the
commit: a deterministic gauntlet that checks the code, reports honestly, and
never touches it. Built for anyone running coding agents — local or API — who
wants proof, not vibes, before code lands.


## Install

```bash
pip install scribe-gate
# with the gauntlet tools (ruff, mypy, pytest, pip-audit, pre-commit):
pip install "scribe-gate[tools]"
```

Requires Python 3.10+.

## Quickstart

**Review a file in place** (no changes, no commit — the safest first run):

```bash
scribe --review-only --files src/my_module.py
```

**Validate a new file an agent produced**, copying it into the project:

```bash
scribe /path/to/project \
  --source-file /tmp/agent_output.py \
  --target-file src/my_module.py
```

**Full run with a commit on success** (prompts for approval first):

```bash
scribe /path/to/project \
  --source-file /tmp/agent_output.py \
  --target-file src/my_module.py \
  --commit
```

**Use as a pre-commit hook** — add to `.pre-commit-config.yaml`:

```yaml
- repo: https://github.com/Mrpongalfer/scribe
  rev: v0.1.0
  hooks:
    - id: scribe-gate
```

(Requires `pip install "scribe-gate[tools]"` in the environment running pre-commit.)

## The gauntlet

Default `validation_steps` in order:

| Step | What it does |
|---|---|
| `validate_inputs` | Path safety checks (no escapes from the target dir) |
| `setup_environment` | Create/reuse an isolated `.venv` in the target dir |
| `install_deps` | Install the target project's declared dependencies |
| `audit_deps` | `pip-audit` vulnerability scan |
| `apply_code` | Copy the source file into the target location |
| `format_code` | `ruff format --check` (check only — never rewrites) |
| `lint_code` | `ruff check` (check only — never auto-fixes) |
| `type_check` | `mypy` |
| `extract_signatures` | AST signature extraction for test focus |
| `generate_tests` | LLM generates pytest tests (strict code-only contract) |
| `save_tests` | Write them to `tests/scribe_generated/` |
| `execute_tests` | Run them with `pytest` |
| `review_code` | LLM structured review → findings (advisory) |
| `run_precommit` | Project's own pre-commit hooks, if configured |
| `commit_changes` | `git add` + `git commit` — **only with `--commit` and operator approval** |
| `generate_report` | Emit the final JSON report |

Any `FAILURE` halts the pipeline. Exit code is `0` on success, `1` on validation
failure, `2` on configuration/initialization errors.

## LLM backends

Two backends ship; both go through the same `generate()` interface.

| Backend | Selection | Config |
|---|---|---|
| Ollama (default) | `SCRIBE_LLM_BACKEND=ollama` | `SCRIBE_LLM_BASE_URL` (default `http://localhost:11434`), `SCRIBE_LLM_MODEL` (default `gemma:2b`) |
| OpenAI-compatible | `SCRIBE_LLM_BACKEND=openai` | `SCRIBE_LLM_BASE_URL`, `SCRIBE_LLM_API_KEY`, `SCRIBE_LLM_MODEL` (default `gpt-4o-mini`) |

CLI flags `--llm-backend`, `--llm-model`, `--llm-base-url` override env/config.
LLM calls use bounded retries (`llm_request_timeout`, `llm_retries`,
`llm_retry_delay`). An LLM failure fails its step — it never silently passes.

## Configuration (`scribe.toml`)

Place `scribe.toml` in the target project dir (or pass `--config-file`).
All keys are optional; shown with defaults:

```toml
# Paths the target dir is allowed to live under (safety rail).
allowed_target_bases = ["~", "/tmp", "."]

# Gate strictness
fail_on_lint_critical = true
fail_on_mypy_error = true
fail_on_test_failure = true
fail_on_audit_severity = "high"   # low | medium | high | critical

# LLM
llm_backend = "ollama"            # or "openai" / "openai-compatible"
llm_model = ""                    # default: backend default
llm_base_url = ""                 # default: backend default
# llm_api_key: prefer SCRIBE_LLM_API_KEY env var over this file
llm_request_timeout = 180.0
llm_retries = 2
llm_retry_delay = 5.0

# Tools
default_tool_timeout = 180.0
max_tool_output_chars = 200000
tool_paths = {}                   # e.g. { ruff = "/opt/bin/ruff" }

commit_message_template = "chore(scribe): validate {target_file}"

# Subset/reorder the gauntlet, e.g. for fast CI runs:
# validation_steps = ["validate_inputs", "apply_code", "format_code",
#                     "lint_code", "type_check", "generate_report"]
```

## The report

`--report-format json` (default) prints a machine-readable report:

```json
{
  "scribe_version": "0.1.0",
  "run_id": "scribe_20261101_...",
  "overall_status": "SUCCESS",
  "target_project_dir": "/path/to/project",
  "target_file_relative": "src/my_module.py",
  "commit_attempted": false,
  "commit_sha": null,
  "steps": [
    {"name": "lint_code", "status": "SUCCESS", "duration_seconds": 0.42,
     "details": {"tool_name": "ruff check", "return_code": 0}, "error_message": null}
  ],
  "audit_findings": [],
  "ai_review_findings": [{"severity": "low", "description": "...", "location": "..."}],
  "test_results_summary": {"return_code": 0}
}
```

Step statuses: `SUCCESS | FAILURE | WARNING | SKIPPED | ADVISORY | PENDING`.
`--report-format text` prints a human-readable summary; `--report-file PATH`
also writes the report to disk.

## Design principles

- **Validate, don't fix.** Formatting and lint run in check mode. Fixing is the
  executor's job (or the human's) — Scribe is the gate, not the mechanic.
- **Safe defaults.** No commit unless `--commit` is passed, and even then the
  operator approves at an interactive prompt (or passes `--yes` explicitly for
  CI). Non-interactive runs without `--yes` decline the commit.
- **Idempotent.** Re-running against the same inputs is safe: venvs are reused,
  file writes are deterministic overwrites, reports are regenerated.
- **Typed config, structured output.** Every run ends in a schema-stable JSON
  report suitable for CI artifacts and audit logs.
- **Local-first.** The default backend is local Ollama; no cloud account needed.

## v1 scope

Python only. No auto-fix loop. No governance/policy engine — the goal-vector
experiment from the original prototype was cut as unproven. The self-improvement
loop is a separate project.

## License

MIT — see [LICENSE](LICENSE).
