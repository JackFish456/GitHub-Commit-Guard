# How to use Spec Commit Matrix

This guide is the **hands-on** companion to [README.md](README.md). The README explains policy, configuration tables, and rule reference; this file walks you through **setup, daily use, and common situations**.

---

## 1. What you are installing

After setup, every `git commit` runs a hook that:

1. Reads your **staged** changes (`git diff --cached`).
2. Runs **local** pattern checks (secrets, SQL injection hints, XSS sinks, and similar).
3. Optionally sends the diff and `SPEC.md` to the **OpenAI API** for an advisory comparison.

The hook exits with a non-zero code when a **blocking** condition applies (see README policy tables). That stops the commit until you fix the issue or adjust the spec.

---

## 2. Prerequisites

| Requirement | Notes |
|-------------|--------|
| Python | 3.9+ recommended; use a virtual environment when possible. |
| Git | Must be installed and on your `PATH`. Run all commands from the **repository root**. |
| pip | Used to install [requirements.txt](requirements.txt). |
| OpenAI API key | Required for LLM checks unless you turn the LLM off (see §7). |

---

## 3. First-time setup (this repository)

### 3.1 Clone and enter the repo

```bash
git clone https://github.com/JackFish456/GitHub-Commit-Guard.git
cd GitHub-Commit-Guard
```

### 3.2 Create a virtual environment (recommended)

**Windows (PowerShell)**

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

**macOS / Linux**

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 3.3 Install Python dependencies

```bash
pip install -r requirements.txt
```

### 3.4 Configure environment variables

Copy the example env file and add your key:

**Windows (PowerShell)**

```powershell
copy .env.example .env
notepad .env
```

**macOS / Linux**

```bash
cp .env.example .env
# edit .env — set OPENAI_API_KEY at minimum
```

Never commit `.env`. The project `.gitignore` should exclude it.

### 3.5 Install the Git hook

```bash
pre-commit install
```

From now on, `git commit` runs **Spec Commit Matrix** automatically.

### 3.6 Prepare `SPEC.md`

- Use the template already in [SPEC.md](SPEC.md), or
- Generate a draft: `python scripts/Spec_check.py --generate-spec` (see §6).

The more accurate `SPEC.md` is, the more useful the LLM comparison will be.

---

## 4. Day-to-day workflow

1. Edit files as usual.
2. Stage what you intend to commit: `git add …`
3. Commit: `git commit -m "Your message"`

The hook runs on the **staged** snapshot only. Unstaged edits are not part of the check.

### 4.1 If the commit succeeds

You may still see **REVIEW**-style output (warnings). Read the notes, fix if appropriate, or proceed depending on team policy.

### 4.2 If the commit is blocked

1. Read the printed **notes** and **checks** (local vs LLM).
2. Fix the code, or update `SPEC.md` if the change is intentional and should be part of the documented contract (see README *When the matrix blocks*).
3. Stage again and retry `git commit`.

---

## 5. Running the checker without committing

Useful for debugging or CI-style runs.

| Goal | Command |
|------|---------|
| Run the hook entrypoint directly (uses staged diff) | `python scripts/Spec_check.py` |
| Same via root shim | `python Spec_check.py` |
| Run the hook as pre-commit names it | `pre-commit run spec-matrix-check --all-files` |

**Note:** With nothing staged, a direct `python scripts/Spec_check.py` run may succeed quickly or have little to analyze. For a realistic check, stage your changes first.

---

## 6. CLI options

The script supports:

| Flag | Meaning |
|------|---------|
| `--generate-spec` | Scan the repo and print a **draft** spec to stdout. |
| `--write` | Use with `--generate-spec` to write to `SPEC.md` (confirmation prompt applies). |

Example:

```bash
python scripts/Spec_check.py --generate-spec
```

---

## 7. Turning the LLM off (local-only)

If you want **no network** and **no** `SPEC.md` requirement for the LLM path:

- Set in `.env`: `SPEC_CHECK_USE_LLM=0`  
  **or**
- In `.spec-check.yaml`, set `use_llm: false` (see README for precedence).

Local deterministic rules still run. Behavior details are in the README *Policy* and *Deterministic rules* sections.

---

## 8. Warning-only vs enforcing

Rollout is controlled mainly by `.spec-check.yaml` (`sign_off_enforcing`, `enforcement`, optional `rollout_warning_until`). You can override some behavior with `SPEC_CHECK_ENFORCE` (see README environment table).

Follow the **Rollout plan** in the README before switching a team to full enforcement.

---

## 9. Adding Spec Commit Matrix to another project

Use the checklist in README *Adding to a project*. In short:

1. Copy `scripts/Spec_check.py` (and the root `Spec_check.py` shim if you want the same layout).
2. Copy `.pre-commit-config.yaml` (adjust `entry` if your path differs).
3. Add `.env.example`, merge dependencies into `requirements.txt`, and add `.env` to `.gitignore`.
4. Add a real `SPEC.md` for that codebase.
5. Run `pip install -r requirements.txt` and `pre-commit install`.

---

## 10. Tests

From the repo root:

```bash
pip install -r requirements.txt
python -m pytest tests/ -v
```

Tests use mocks; no API key is required for them.

---

## 11. Emergency bypass

Use only when necessary: `git commit --no-verify`. This **skips all pre-commit hooks**, not just this one. See README *Emergency bypass*.

---

## 12. Where to look next

| Question | Document |
|----------|----------|
| Verdict meanings, fail-closed behavior, LLM advisory mode | [README.md](README.md) — *Policy*, *How it works* |
| Every `.spec-check.yaml` key and defaults | [README.md](README.md) — *Optional .spec-check.yaml* |
| Full list of local rules | [README.md](README.md) — *Deterministic rules* |
| Symptom → fix | [README.md](README.md) — *Troubleshooting* |
