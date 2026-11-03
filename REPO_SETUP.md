# scribe-gate — new repo setup runbook

Takes the `scribe-v1/` package directory and publishes it as a new standalone
public repo (e.g. `Mrpongalfer/scribe`). Run these on your own machine, in
order. Nothing here is destructive; each step has a check.

## 0. What you're publishing

- `src/scribe/` — the cleaned v1 package (config, runner, environment,
  pluggable LLM backends, gauntlet steps, orchestrator, CLI, JSON reports)
- `tests/` — 34 tests, all passing (unit + stubbed-LLM end-to-end gauntlet)
- `pyproject.toml` (`scribe-gate`, MIT), `.pre-commit-hooks.yaml`,
  `.github/workflows/ci.yml` (dogfoods Scribe on itself), `README.md`, `LICENSE`

Verified before handoff: `ruff check` clean, `ruff format --check` clean,
`mypy src` clean, `pytest` 34/34, `pip install .` works, both console scripts
(`scribe`, `scribe-gate`) respond, and Scribe validated its own source 10/10.

## 1. Create the repo (2 min)

1. Go to https://github.com/new
2. Name: `scribe`, description: "Validation gauntlet and trust gate for agent-written Python code", **Public**, no README/gitignore/license (they're already in the package).
3. Create. Copy the repo URL: `git@github.com:Mrpongalfer/scribe.git`
   (or the `https://` variant if you prefer).

## 2. Push (3 min)

```bash
cd ~/workspace/scribe-v1

# sanity: nothing private in here
grep -ri "sk-\|api[_-]key\|password\|token" src tests README.md pyproject.toml || echo "no secrets found"

git init -b main
git add .
git status --short          # review what goes in; .build-venv/ must NOT appear
git commit -m "feat: scribe-gate v0.1.0 — standalone trust gate extracted from DUO"
git remote add origin git@github.com:Mrpongalfer/scribe.git
git push -u origin main
```

Check: `git status` shows the venv dir is ignored. If `.build-venv/` shows up,
stop — the `.gitignore` entry isn't matching — and tell me before pushing.

## 3. Verify on GitHub (2 min)

- Repo page shows the README with install instructions.
- Actions tab: the `scribe-ci` workflow runs on the push — lint, mypy, pytest,
  then Scribe dogfooding itself, report uploaded as an artifact.
- Confirm the workflow is green before sharing the link anywhere.

## 4. First real run (5 min, needs Ollama or an API key)

```bash
pip install "scribe-gate[tools]"   # ruff, mypy, pytest, pip-audit, pre-commit

# safest first run: review-only, in place, no commit possible
scribe . --review-only --files src/scribe/cli.py

# full gauntlet on an agent-produced file (still no commit without --commit)
scribe /path/to/project --source-file /tmp/agent_out.py --target-file src/new_mod.py
```

For an OpenAI-compatible backend instead of local Ollama:

```bash
export SCRIBE_LLM_BACKEND=openai
export SCRIBE_LLM_BASE_URL="https://api.openai.com/v1"
export SCRIBE_LLM_API_KEY="..."
export SCRIBE_LLM_MODEL="gpt-4o-mini"
```

## 5. Optional: try the pre-commit hook on another repo

```bash
pip install "scribe-gate[tools]"
# in the target repo's .pre-commit-config.yaml:
# - repo: https://github.com/Mrpongalfer/scribe
#   rev: v0.1.0
#   hooks:
#     - id: scribe-gate
```

Tag the release after the first green CI run:

```bash
git tag -a v0.1.0 -m "scribe-gate v0.1.0"
git push origin v0.1.0
```

## Rollback

Haven't pushed yet and something looks wrong? `rm -rf .git` in the copy and
start step 2 over — the package files are untouched. Pushed something you
regret? Delete the repo at github.com/Mrpongalfer/scribe/settings and redo
from step 1; nothing else references it yet.
