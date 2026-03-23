# Spec Commit Matrix

A pre-commit hook that compares staged code against `SPEC.md` via local checks and optional LLM. Blocks commits that drift from the spec.

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env          # PowerShell: copy .env.example .env
# Edit .env: add OPENAI_API_KEY
pre-commit install
```

**Verification commands:**

```bash
# Dry run (no staged changes → exit 0)
python scripts/Spec_check.py

# Or via root shim (same behavior)
python Spec_check.py

# Run pre-commit on all files (e.g. after adding hook)
pre-commit run spec-matrix-check --all-files
```

Exit 0 = OK. Stage changes first to run the full diff check.

## Purpose

This tool enforces spec compliance at commit time. It runs automatically when you `git commit`, comparing your staged diff against `SPEC.md` via an AI review. Commits that misalign with the spec (e.g. out-of-scope features, broken contracts, security issues) are blocked until you fix or adjust.

## How it works

```
git commit → staged diff extracted → GPT-4o compares diff vs SPEC.md → verdict
```

| Verdict | Label | Meaning | Commit allowed? |
|---------|-------|---------|-----------------|
| COMMIT | [OK] | All checks pass | Yes |
| REVIEW | [WARN] | Warnings only | Yes (with notes) |
| BLOCK | [BLOCK] | Critical misalignment | No |

## Policy

**Failure mode: FAIL CLOSED** — If checker runtime, API, or parse fails, block commit (non-zero exit).

### Truth table

| Scenario | Outcome | Exit code |
|----------|---------|-----------|
| API failure (key missing, network, rate limit) | BLOCK | 1 |
| JSON parse failure (invalid LLM response) | BLOCK | 1 |
| Missing `SPEC.md` | BLOCK | 1 |
| Local deterministic critical hit (`fail`) | BLOCK | 1 |
| LLM-only warning/fail (`llm_advisory: true`) | Surface only; never blocks | 0 |
| All pass | COMMIT | 0 |
| Warnings only (no critical local fail) | REVIEW | 0 |

**LLM role: ADVISORY** — LLM findings inform review, but deterministic critical checks are authoritative. When `llm_advisory: true`, only local rule-based failures block; LLM verdict is surfaced for review but does not block the commit.

### Verdicts

- **COMMIT** — All checks pass; commit proceeds.
- **REVIEW** — Warnings only; commit allowed. Notes surface suggestions; consider addressing before merging.
- **BLOCK** — One or more local critical failures; commit blocked. Fix the issues and retry.

## Operating Modes

**Minimal mode (core gate only)**

- Staged diff extracted via `git diff --cached`
- `SPEC.md` compared against diff via GPT-4o
- Built-in rule-based pre-checks (secrets, SQL injection, XSS patterns)
- No config file required

**Extended mode (optional extras)**

- `.spec-check.yaml` for `skip_patterns`, `custom_rules`, `severity_overrides`
- `--generate-spec` to bootstrap `SPEC.md` from repo scan
- Custom base URL via `OPENAI_BASE_URL`

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `OPENAI_API_KEY` | Yes (if LLM enabled) | API key for GPT-4o |
| `OPENAI_BASE_URL` | No | Custom API endpoint |
| `SPEC_CHECK_USE_LLM` | No | Set to `0` to disable LLM (local checks only) |
| `SPEC_CHECK_ENFORCE` | No | `0` = warning_only, `1` = enforcing. Overrides config. |

```bash
cp .env.example .env
# Edit .env and add your API key
```

### 3. Hook wiring

The hook runs `python scripts/Spec_check.py` from the repo root. Uses forward slashes for cross-platform compatibility (PowerShell, cmd, bash).

```bash
pre-commit install
```

### 4. Create SPEC.md

See [Initial spec setup](#initial-spec-setup) below.

### 5. Commit

```bash
git add .
git commit -m "your message"
```

## Adding to a project

You must have these files in your project repo. **There is no remote pre-commit source** — everything is local.

### Step 1: Copy the script
Copy `scripts/Spec_check.py` into your project's `scripts/` folder (or another location; adjust the path in `.pre-commit-config.yaml` if needed).

### Step 2: Add pre-commit config
Copy the file below to `.pre-commit-config.yaml` in your repo root:

```yaml
repos:
  - repo: local
    hooks:
      - id: spec-matrix-check
        name: Spec Commit Matrix
        entry: python scripts/Spec_check.py
        language: system
        always_run: true
        pass_filenames: false
```

### Step 3: Create SPEC.md
Copy `SPEC.md` from this repo (or create from the [template](#spec-template) below), or run `python scripts/Spec_check.py --generate-spec` to auto-generate a draft. Fill it in with your project's features, architecture, and requirements.

### Step 4: Set up environment
Copy `.env.example` from this repo to `.env` in your project root, then add your API key:
```bash
cp .env.example .env
# Edit .env and add OPENAI_API_KEY
```

### Step 5: Install dependencies
Add to your `requirements.txt` (or install directly):

```
openai>=1.0.0
pre-commit>=3.0.0
python-dotenv>=1.0.0
PyYAML>=6.0
```

Then run:
```bash
pip install -r requirements.txt
```

### Step 6: Install the hook
```bash
pre-commit install
```

Also add `.env` to `.gitignore` so the API key is never committed.

### File structure

```
your-project/
├── scripts/
│   └── Spec_check.py        # matrix logic
├── .pre-commit-config.yaml  # wires hook into git
├── .spec-check.yaml         # optional (skip_patterns, custom_rules, severity)
├── .env                     # your API key (never committed)
├── .env.example             # template for collaborators
├── .gitignore               # includes .env
├── requirements.txt        # openai, pre-commit, python-dotenv, PyYAML
└── SPEC.md                  # your project spec — the source of truth
```

## SPEC template

Use this structure for `SPEC.md`. Replace the placeholders with your project details:

```markdown
# Project Spec

## Overview
Brief description of what this project does and its primary purpose.

## Features
- List every user-facing feature here
- One feature per bullet
- Be specific — vague features produce vague checks

## Architecture
- Language / runtime: e.g. Python 3.11
- Framework: e.g. FastAPI
- Database: e.g. PostgreSQL
- Key libraries: e.g. SQLAlchemy, Pydantic

## API Endpoints
| Method | Path | Description |
|--------|------|-------------|
| GET | /example | Returns example data |

## Security Requirements
- Authentication method: e.g. JWT / OAuth2 / API key
- Rate limiting: e.g. 100 requests/min per IP
- Input validation: e.g. all inputs validated with Pydantic
- Secrets: never hardcoded, always from environment variables

## Out of Scope
List anything explicitly NOT part of this project so the matrix can flag scope creep.
```

## Initial spec setup

**Option A — Generate from repo:** Run `python scripts/Spec_check.py --generate-spec` to scan the repo and produce a draft. Copy the output into `SPEC.md`, or use `--write` to overwrite (you'll be prompted to confirm). This is useful to bootstrap a new project.

**Option B — Manual:** Create `SPEC.md` using the [SPEC template](#spec-template) above. Fill in Overview, Features, Architecture, API Endpoints (if applicable), Security Requirements, and Out of Scope.

## When the matrix blocks

When the check returns BLOCK or REVIEW, the notes describe what drifted from the spec. **You must update SPEC.md manually** to reflect the changes, then re-stage and commit. There is no auto-update; you are responsible for keeping the spec accurate. Typical flow:

1. Read the `overallNotes` and `checks` to see what changed and what section of SPEC.md to update
2. Edit SPEC.md to add or revise the relevant sections
3. Run `git add SPEC.md` (and your code if not already staged)
4. Run `git commit` again — the matrix will re-run with the updated spec

## Input size limit

Spec and diff are sent to the API as one payload. If the combined length exceeds ~8,000 characters (a rough token proxy), the content is truncated with `[truncated - first N chars shown]`. Truncated content still produces a useful verdict, but very large changes may lose some context.

## Optional .spec-check.yaml

If `.spec-check.yaml` exists in the repo root, it is read. The file is optional; all keys have defaults.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `mode` | `minimal` \| `extended` | `extended` | `extended` = skip_patterns, custom_rules, severity_overrides, --generate-spec |
| `sign_off_enforcing` | bool | `false` | Must be `true` to allow enforcing; explicit sign-off. |
| `rollout_warning_until` | `YYYY-MM-DD` | `""` | Stay in warning-only until this date (optional). |
| `enforcement` | `warning_only` \| `enforcing` | `warning_only` | Use enforcing only after `sign_off_enforcing: true`. |
| `fail_closed` | bool | `true` | Runtime/API/parse failures block commit (non-zero exit). |
| `require_spec` | bool | `true` | Missing SPEC.md blocks commit. |
| `llm_advisory` | bool | `true` | LLM findings inform review; only local critical checks block. |
| `skip_patterns` | list of globs | `[]` | File paths to exclude from the diff (e.g. `*.min.js`, `vendor/**`) |
| `custom_rules` | list | `[]` | Extra regex rules: `pattern`, `name`, `category`, `severity` |
| `severity_overrides` | map | `{}` | Override rule severity: `"rule_id": "warn"` or `"fail"` |
| `disabled_rules` | list | `[]` | Rule IDs to skip (e.g. `["exec_usage"]`) |
| `use_llm` | bool | `true` | When false, local checks only. Overridden by `SPEC_CHECK_USE_LLM` env. |
| `enforce` | bool | — | Deprecated. Use `enforcement`. When false = warning_only. |

Example:

```yaml
mode: extended
sign_off_enforcing: false
rollout_warning_until: ""   # "2025-03-24" = warning-only until that date
enforcement: warning_only
fail_closed: true
require_spec: true
llm_advisory: true

skip_patterns:
  - "*.min.js"
  - "vendor/**"
severity_overrides:
  exec_usage: "warn"
custom_rules:
  - pattern: "TODO: bypass"
    name: "Bypass comment"
    category: "Security"
    severity: "warn"
```

## Deterministic rules (local, no network)

Rules run locally before any API call. Severity mapping: **critical** => fail (blocks), **medium** => warn.

| rule_id | Severity | Description |
|---------|----------|-------------|
| `hardcoded_secret` | critical | `password=`, `api_key=`, `secret=`, `token=` in string literals |
| `sql_format` | critical | `sql.format(` - SQL injection risk |
| `sql_percent` | critical | `execute(..., % %)` - %-format in queries |
| `sql_concat` | critical | `SELECT/INSERT/UPDATE/DELETE ... + "` - string concatenation |
| `eval_usage` | critical | `eval(` on potentially untrusted input |
| `exec_usage` | medium | `exec(` - can be legit; warn |
| `xss_innerHTML` | critical | `innerHTML=` - XSS sink |
| `xss_document_write` | critical | `document.write(` - XSS sink |
| `xss_dangerouslySetInnerHTML` | medium | React `dangerouslySetInnerHTML` |
| `xss_outerHTML` | medium | `.outerHTML=` - XSS risk |
| `auth_bypass` | critical | `bypass/skip auth` patterns |
| `auth_bypass_comment` | critical | `# bypass auth`, `auth=false #` |

Override via `severity_overrides` (e.g. `exec_usage: "fail"`). Disable via `disabled_rules: [rule_id]`.

### Merge precedence (authoritative)

1. **Any deterministic `fail` => final BLOCK** (local rules are authoritative).
2. **LLM output remains advisory** — LLM findings inform review but do not block when `llm_advisory: true`.
3. If no local fail: LLM verdict applies (or local `warn` => REVIEW, else COMMIT).
4. Checks list: local first, then LLM (`source: "local"` or `"llm"`).

**Disable LLM (local-only mode):** Set `SPEC_CHECK_USE_LLM=0` or `use_llm: false` in config. Local checks run without network; SPEC.md is not required.

## Rollout plan

| Phase | Duration | Config | Behavior |
|-------|----------|--------|----------|
| **Day 0–1: Warning-only** | 1 day | `sign_off_enforcing: false` (default) | Full findings printed; commit never blocked. Gather feedback, tune SPEC.md. |
| **Post sign-off: Enforcing** | After explicit sign-off | `sign_off_enforcing: true` + `enforcement: enforcing` | BLOCK verdict blocks commit. |
| **`main` branch** | Always | — | Protected via branch rules; enforcing applies. |

**Steps:**

1. **Day 0–1:** Run with defaults. Optional: set `rollout_warning_until: "YYYY-MM-DD"` (tomorrow) to lock warning-only for 1 day.
2. **Sign-off (single config change):** In `.spec-check.yaml`, set `sign_off_enforcing: true` and `enforcement: enforcing`. No env vars or commands required.
3. **Branch protection:** Configure `main` (or default branch) to require passing checks and reject direct pushes. See [Branch protection](#branch-protection) below.

### Sign-off step (switch to enforcing)

To enable blocking after the warning-only period, edit `.spec-check.yaml`:

```yaml
sign_off_enforcing: true
enforcement: enforcing
```

That is the only change required. No command or env var needed.

### Branch protection

Always protect the default branch (e.g. `main`). Use your host’s branch protection or rules:

- **GitHub:** Settings → Branches → Add rule for `main` → Require status checks, restrict direct push.
- **GitLab:** Settings → Repository → Protected branches → Add `main`, set roles.
- **Azure DevOps:** Repo Settings → Policies → Branch policies → Add `main`.
- **Bitbucket:** Repository settings → Branch restrictions → Add rule for `main`.
- **Generic:** Configure your CI and require passing `python scripts/Spec_check.py` (or equivalent) before merge.

Direct pushes to `main` should be blocked or restricted; enforce the spec check in CI or pre-commit for all contributors.

## API configuration

The script supports flexible API config via environment variables:

- **API key**: `OPENAI_API_KEY`
- **Base URL** (optional): `OPENAI_BASE_URL` for non-standard endpoints

Values are passed into the OpenAI client when provided.

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `[ERROR] git diff failed` | Not a git repo, `git` not in PATH, or staged files in submodule | Run from repo root; ensure `git` is installed and in PATH. If using submodules, check paths. |
| `[ERROR] Failed to run git diff` | `git` command not found (OSError) | Install Git; ensure it is on your system PATH. |
| `[ERROR] OpenAI request failed` | Missing API key, invalid key, network error, or rate limit | Set `OPENAI_API_KEY` in `.env`; verify key is valid and network is reachable. |
| `[ERROR] Could not parse/validate model output` | LLM returned invalid JSON or malformed structure | Retry; if persistent, the model may be returning markdown fences or extra text. |
| `Missing keys: verdict, ...` | LLM response missing required fields (`verdict`, `verdictReason`, `checks`, `overallNotes`) | Retry; the model sometimes omits fields. |
| `[ERROR] Could not read SPEC.md` | File locked or permissions issue | Ensure `SPEC.md` exists and is readable; close editors that may lock it. |

## Running tests

Tests use mocks; no network required. From the repo root:

```bash
pip install -r requirements.txt
python -m pytest tests/ -v
```

## Emergency bypass

The check **cannot and should not be skipped** in normal workflow. It exists to prevent spec drift and catch issues before they reach the repo. Teams should discourage bypassing.

**Last resort only:** Use `git commit --no-verify` when the hook is broken or in exceptional circumstances (e.g. emergency hotfix, recovery from CI failure). **Bypassing removes the safeguard.** You risk committing out-of-spec code, secrets, or security issues. Use only when necessary and follow up with a proper fix.
