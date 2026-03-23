"""
Spec Commit Matrix - Compares staged git diff against SPEC.md via GPT-4o.
Blocks commits that drift from the project spec.
"""

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

# Rough proxy for token budget (chars). Spec + diff combined.
CHAR_BUDGET = 8000

# Deterministic rules: critical => fail (blocks), medium => warn.
# rule_id used in severity_overrides for per-rule override.
DETERMINISTIC_RULES: list[dict[str, Any]] = [
    # Critical: hardcoded secrets/tokens
    {"rule_id": "hardcoded_secret", "pattern": r"(?:password|api_key|secret|token)\s*=\s*[\"']([^\"']{3,})[\"']", "name": "Hardcoded secret", "category": "Security", "severity": "critical"},
    # Critical: SQL injection - raw format/concatenation
    {"rule_id": "sql_format", "pattern": r"sql\.format\s*\(", "name": "SQL injection risk (sql.format)", "category": "Security", "severity": "critical"},
    {"rule_id": "sql_percent", "pattern": r"(?:execute|executemany)\s*\([^)]*%\s*%", "name": "SQL injection risk (%-format)", "category": "Security", "severity": "critical"},
    {"rule_id": "sql_concat", "pattern": r"(?:SELECT|INSERT|UPDATE|DELETE)\s+.*\s+\+\s*[\"'`]", "name": "SQL injection risk (string concat)", "category": "Security", "severity": "critical"},
    # Critical: eval/exec on untrusted input
    {"rule_id": "eval_usage", "pattern": r"\beval\s*\(", "name": "eval() usage", "category": "Security", "severity": "critical"},
    # Medium: exec can be legit (e.g. codegen); warn
    {"rule_id": "exec_usage", "pattern": r"\bexec\s*\(", "name": "exec() usage", "category": "Security", "severity": "medium"},
    # Critical: XSS sinks
    {"rule_id": "xss_innerHTML", "pattern": r"innerHTML\s*=", "name": "innerHTML assignment (XSS risk)", "category": "Security", "severity": "critical"},
    {"rule_id": "xss_document_write", "pattern": r"document\.write\s*\(", "name": "document.write (XSS risk)", "category": "Security", "severity": "critical"},
    # Medium: React/other XSS sinks
    {"rule_id": "xss_dangerouslySetInnerHTML", "pattern": r"dangerouslySetInnerHTML", "name": "dangerouslySetInnerHTML (XSS risk)", "category": "Security", "severity": "medium"},
    {"rule_id": "xss_outerHTML", "pattern": r"\.outerHTML\s*=", "name": "outerHTML assignment (XSS risk)", "category": "Security", "severity": "medium"},
    # Critical: auth bypass patterns
    {"rule_id": "auth_bypass", "pattern": r"(?:bypass|skip)\s*[-_]?\s*(?:auth|authz|authenticate)", "name": "Auth bypass pattern", "category": "Security", "severity": "critical"},
    {"rule_id": "auth_bypass_comment", "pattern": r"#\s*(?:bypass|skip)\s*auth|auth\s*=\s*false\s*#", "name": "Auth bypass comment", "category": "Security", "severity": "critical"},
]

SPEC_CONFLICT_CATEGORIES = {"Features", "Architecture", "API Contract", "Scope"}
LEGACY_FILE_REMAP = {
    "gitignore": ".gitignore",
    "pre-commit-config.yaml": ".pre-commit-config.yaml",
    "env.example": ".env.example",
}


def _ascii_safe(s: str) -> str:
    """Make string safe for cp1252/ASCII consoles. Replaces non-ASCII with '?'."""
    return s.encode("ascii", errors="replace").decode("ascii")


def _is_llm_enabled(config: dict[str, Any]) -> bool:
    """LLM is enabled if env or config says yes. Env overrides config. Default: True."""
    env_val = os.getenv("SPEC_CHECK_USE_LLM", "").strip().lower()
    if env_val in ("0", "false", "no"):
        return False
    if env_val in ("1", "true", "yes"):
        return True
    return config.get("use_llm", True)


def _is_fail_closed(config: dict[str, Any]) -> bool:
    """Fail-closed blocks on LLM/runtime failures. Default: True."""
    return bool(config.get("fail_closed", True))


def _is_enforcing(config: dict[str, Any]) -> bool:
    """
    Enforcing mode blocks commits on BLOCK verdict.
    Gates: SPEC_CHECK_ENFORCE env > rollout_warning_until > sign_off_enforcing > enforcement.
    """
    env_val = os.getenv("SPEC_CHECK_ENFORCE", "").strip().lower()
    if env_val in ("0", "false", "no"):
        return False
    if env_val in ("1", "true", "yes"):
        return True

    # Optional: stay in warning-only until this date (ISO YYYY-MM-DD)
    until = config.get("rollout_warning_until", "")
    if until:
        try:
            limit = date.fromisoformat(str(until).strip())
            if date.today() < limit:
                return False
        except (ValueError, TypeError):
            pass

    # Explicit sign-off required before enforcing
    if not config.get("sign_off_enforcing", False):
        return False

    enforcement = config.get("enforcement", "warning_only")
    if enforcement == "enforcing":
        return True
    if enforcement == "warning_only":
        return False
    return config.get("enforce", False)


def _warn_legacy_files() -> None:
    """Warn when legacy duplicate filenames are present in repo root."""
    root = Path.cwd()
    for legacy, canonical in LEGACY_FILE_REMAP.items():
        if (root / legacy).exists():
            print(
                f"[WARN] Found legacy file '{legacy}'. Prefer '{canonical}' to avoid config drift.",
                file=sys.stderr,
            )

SYSTEM_PROMPT = """You are a senior software architect performing a spec compliance review before a git commit.
Compare a project spec (markdown) against a git diff and return structured JSON.

Return ONLY valid JSON - no markdown, no explanation outside the JSON.

Shape:
{
  "verdict": "COMMIT" | "BLOCK" | "REVIEW",
  "verdictReason": "one sentence summary",
  "checks": [
    {
      "category": "Features" | "Architecture" | "API Contract" | "Security" | "Scope" | "Structure",
      "name": "short check name",
      "status": "pass" | "fail" | "warn",
      "notes": "specific, concrete note referencing actual code in the diff"
    }
  ],
  "overallNotes": "2-3 sentence assessment with actionable recommendations"
}

Rules:
- BLOCK = one or more critical fails -> exit code 1 (prevents commit)
- REVIEW = warnings only -> exit code 0 (allows commit, surfaces notes)
- COMMIT = all pass -> exit code 0
- Be specific: reference actual function names, endpoints, or patterns seen in the diff
- Scope check: flag anything in the code NOT mentioned in the spec
- When drift is found: specify exactly what section of SPEC.md should be updated and what to add. In overallNotes and in each failing check's notes, state: (1) what changed in the code, (2) which SPEC.md section to update, (3) the exact text or structure to add.

Security / "vibe coding" focus - explicitly flag:
- Missing or weak auth checks (e.g. endpoints without auth, hardcoded admin bypasses)
- Missing input validation (user input used without sanitization)
- SQL injection risk (raw string concatenation, .format() for queries, unfiltered params)
- XSS risk (innerHTML, document.write, unsanitized output to DOM)
- Secret handling (secrets in code, logs, or URLs)
- Code that skips validation "for speed" or "temporarily" without a documented exception"""

GENERATE_SPEC_PROMPT = """You are a senior software architect. Given a scan of a project repository, produce a draft SPEC.md that will serve as the source of truth for spec compliance checks.

The draft must be valid markdown and follow this structure:
- Overview (brief project description and purpose)
- Features (user-facing features, one per bullet)
- Architecture (language, framework, database, key libraries)
- API Endpoints (table: Method | Path | Description) if applicable
- Security Requirements (auth, rate limiting, validation, secrets handling)
- Out of Scope (what is explicitly NOT in scope)

Infer from the repo structure and files. Be specific; vague specs produce vague checks. If information is missing, note assumptions or use placeholders like [fill in]."""


def scan_repo(root: Path, max_file_chars: int = 2000) -> str:
    """Scan repo for key files and structure. Returns a string for AI context."""
    parts: list[str] = []

    # Top-level structure (directories and key files)
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return "Could not read repo directory."

    dirs = [e.name for e in entries if e.is_dir() and not e.name.startswith(".")]
    files = [e.name for e in entries if e.is_file()]
    parts.append(f"## Structure\nDirectories: {', '.join(dirs) or '(none)'}\nTop-level files: {', '.join(files) or '(none)'}\n")

    # requirements.txt
    req_path = root / "requirements.txt"
    if req_path.exists():
        try:
            content = req_path.read_text(encoding="utf-8")[:max_file_chars]
            parts.append(f"## requirements.txt\n```\n{content}\n```\n")
        except OSError:
            pass

    # README
    readme_path = root / "README.md"
    if readme_path.exists():
        try:
            content = readme_path.read_text(encoding="utf-8")[:max_file_chars]
            parts.append(f"## README.md (excerpt)\n```\n{content}\n```\n")
        except OSError:
            pass

    # Key Python files (main entry points)
    key_names = {"main.py", "app.py", "__main__.py", "manage.py"}
    for path in root.rglob("*.py"):
        if path.name in key_names or (path.parent == root and path.suffix == ".py"):
            try:
                content = path.read_text(encoding="utf-8")[:max_file_chars]
                rel = path.relative_to(root)
                parts.append(f"## {rel}\n```\n{content}\n```\n")
            except (OSError, ValueError):
                pass
        if len(parts) > 8:  # Limit number of files
            break

    return "\n".join(parts) if parts else "Repo appears empty or inaccessible."


def run_generate_spec(repo_context: str) -> str:
    """Call AI to generate draft SPEC.md. Returns the draft text."""
    api_key = os.getenv("OPENAI_API_KEY")
    base_url = os.getenv("OPENAI_BASE_URL")
    client_kwargs: dict[str, str] = {}
    if api_key:
        client_kwargs["api_key"] = api_key
    if base_url:
        client_kwargs["base_url"] = base_url

    try:
        client = OpenAI(**client_kwargs)
        response = client.chat.completions.create(
            model="gpt-4o",
            max_tokens=2000,
            messages=[
                {"role": "system", "content": GENERATE_SPEC_PROMPT},
                {"role": "user", "content": f"Repo scan:\n\n{repo_context}"},
            ],
        )
    except Exception as exc:
        print(f"[ERROR] API request failed: {_ascii_safe(str(exc))}. Fix: set OPENAI_API_KEY, check network.", file=sys.stderr)
        sys.exit(1)

    text = response.choices[0].message.content or ""
    # Strip markdown code fence if present
    if text.strip().startswith("```"):
        lines = text.strip().splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)
    return text.strip()


def cmd_generate_spec(write: bool) -> None:
    """Generate draft SPEC.md from repo scan."""
    root = Path.cwd()
    print("[INFO] Scanning repo...", file=sys.stderr)
    context = scan_repo(root)
    print("[INFO] Generating draft SPEC.md...", file=sys.stderr)
    draft = run_generate_spec(context)

    if write:
        spec_path = root / "SPEC.md"
        if spec_path.exists():
            try:
                reply = input("SPEC.md exists. Overwrite? (y/N): ").strip().lower()
                if reply not in ("y", "yes"):
                    print("Skipped. Draft printed below.\n", file=sys.stderr)
                    print(draft)
                    return
            except EOFError:
                print("Skipped (no TTY). Draft printed below.\n", file=sys.stderr)
                print(draft)
                return
        try:
            spec_path.write_text(draft, encoding="utf-8")
            print(f"[OK] Wrote {spec_path}", file=sys.stderr)
        except OSError as exc:
            print(f"[ERROR] Could not write SPEC.md: {_ascii_safe(str(exc))}", file=sys.stderr)
            sys.exit(1)
    else:
        print(draft)


def load_spec_check_config() -> dict[str, Any]:
    """Load optional .spec-check.yaml from repo root. Returns empty dict if missing."""
    config_path = Path.cwd() / ".spec-check.yaml"
    if not config_path.exists():
        return {}
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def get_staged_diff(config: dict[str, Any] | None = None) -> str:
    config = config or {}
    skip_patterns: list[str] = config.get("skip_patterns") or []

    if not skip_patterns:
        try:
            result = subprocess.run(
                ["git", "diff", "--cached", "--unified=3"],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as exc:
            print(f"[ERROR] Git not found: {_ascii_safe(str(exc))}. Fix: install Git, ensure it is on PATH.", file=sys.stderr)
            sys.exit(1)

        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            detail = _ascii_safe(stderr.splitlines()[0][:80] if stderr else "")
            print(f"[ERROR] git diff failed. {detail}. Fix: run from repo root, ensure you are in a git repo.", file=sys.stderr)
            sys.exit(1)

        return result.stdout

    try:
        names_result = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        print(f"[ERROR] Git not found: {_ascii_safe(str(exc))}. Fix: install Git, ensure it is on PATH.", file=sys.stderr)
        sys.exit(1)

    if names_result.returncode != 0:
        stderr = (names_result.stderr or "").strip()
        detail = _ascii_safe(stderr.splitlines()[0][:80] if stderr else "")
        print(f"[ERROR] git diff --name-only failed. {detail}. Fix: run from repo root.", file=sys.stderr)
        sys.exit(1)

    paths = [p.strip() for p in names_result.stdout.splitlines() if p.strip()]
    norm = lambda p: p.replace("\\", "/")
    included = []
    for path in paths:
        skip = False
        p = norm(path)
        for pattern in skip_patterns:
            if fnmatch.fnmatch(p, pattern):
                skip = True
                break
            if pattern.endswith("/**"):
                prefix = pattern[:-3].rstrip("/")
                if p == prefix or p.startswith(prefix + "/"):
                    skip = True
                    break
        if not skip:
            included.append(path)

    if not included:
        return ""

    try:
        result = subprocess.run(
            ["git", "diff", "--cached", "--unified=3", "--", *included],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        print(f"[ERROR] Git not found: {_ascii_safe(str(exc))}. Fix: install Git, ensure it is on PATH.", file=sys.stderr)
        sys.exit(1)

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        detail = _ascii_safe(stderr.splitlines()[0][:80] if stderr else "")
        print(f"[ERROR] git diff failed. {detail}. Fix: run from repo root.", file=sys.stderr)
        sys.exit(1)

    return result.stdout


def _severity_to_status(severity: str) -> str:
    """Map critical => fail, medium => warn. Invalid => warn."""
    return "fail" if severity == "critical" else "warn"


def run_local_checks(diff: str, config: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Run deterministic checks locally (no network). Returns checks with severity.
    Each check has: category, name, status (pass|fail|warn), notes, source="local".
    Severity mapping: critical => fail (blocks), medium => warn.
    """
    checks: list[dict[str, Any]] = []
    severity_overrides: dict[str, str] = config.get("severity_overrides") or {}
    custom_rules: list[dict[str, Any]] = config.get("custom_rules") or []
    disabled_rules: set[str] = set(config.get("disabled_rules") or [])

    def resolve_status(rule_id: str, default_severity: str) -> str:
        override = severity_overrides.get(rule_id)
        if override in ("fail", "warn"):
            return override
        return _severity_to_status(default_severity)

    def add_check(category: str, name: str, status: str, notes: str) -> None:
        checks.append({
            "category": category,
            "name": name,
            "status": status,
            "notes": notes,
            "source": "local",
        })

    # Built-in deterministic rules (no network)
    for rule in DETERMINISTIC_RULES:
        rule_id = rule.get("rule_id", "")
        if rule_id in disabled_rules:
            continue
        pat = rule.get("pattern")
        name = rule.get("name", rule_id)
        category = rule.get("category", "Security")
        sev = rule.get("severity", "medium")
        if not pat:
            continue
        try:
            for m in re.finditer(pat, diff, re.IGNORECASE):
                snippet = m.group(0)[:60] + "..." if len(m.group(0)) > 60 else m.group(0)
                status = resolve_status(rule_id, sev)
                add_check(
                    category,
                    name,
                    status,
                    f"Matched: {snippet}. Review for security impact.",
                )
                break  # One check per rule per diff
        except re.error:
            pass

    # Custom rules from config
    for rule in custom_rules:
        if not isinstance(rule, dict):
            continue
        pat = rule.get("pattern")
        rule_name = rule.get("name", "custom")
        cat = rule.get("category", "Structure")
        default = rule.get("severity", "warn")
        rule_id = rule.get("rule_id", rule_name.lower().replace(" ", "_"))
        if rule_id in disabled_rules:
            continue
        if pat:
            try:
                if re.search(pat, diff, re.IGNORECASE):
                    override = severity_overrides.get(rule_id)
                    status = override if override in ("fail", "warn") else _severity_to_status(default)
                    add_check(
                        cat,
                        rule_name,
                        status,
                        rule.get("notes", f"Matched custom rule: {rule_name}"),
                    )
            except re.error:
                pass

    return checks


def get_spec(config: dict[str, Any] | None = None) -> str:
    """Load SPEC.md. When require_spec is True, missing file blocks (exit 1)."""
    config = config or {}
    require_spec = config.get("require_spec", True)
    spec_path = Path.cwd() / "SPEC.md"
    if not spec_path.exists():
        if require_spec:
            msg = "[ERROR] SPEC.md not found. Create SPEC.md in repo root (e.g. run: python scripts/Spec_check.py --generate-spec --write). Policy: require_spec blocks commit."
            print(msg, file=sys.stderr)
            sys.exit(1)
        return ""
    try:
        return spec_path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"[ERROR] Could not read SPEC.md: {_ascii_safe(str(exc))}. Fix: ensure file exists and is readable.", file=sys.stderr)
        sys.exit(1)


def validate_matrix_payload(data: dict[str, Any]) -> None:
    if not isinstance(data, dict):
        raise ValueError("Response is not a JSON object.")

    required_keys = {"verdict", "verdictReason", "checks", "overallNotes"}
    missing = required_keys - set(data.keys())
    if missing:
        raise ValueError(f"Missing keys: {', '.join(sorted(missing))}")

    if data["verdict"] not in {"COMMIT", "BLOCK", "REVIEW"}:
        raise ValueError("Invalid verdict value.")

    if not isinstance(data["checks"], list):
        raise ValueError("'checks' must be a list.")

    for idx, check in enumerate(data["checks"], start=1):
        if not isinstance(check, dict):
            raise ValueError(f"Check #{idx} is not an object.")
        for key in ("category", "name", "status", "notes"):
            if key not in check:
                raise ValueError(f"Check #{idx} missing key '{key}'.")
        if check["status"] not in {"pass", "fail", "warn"}:
            raise ValueError(f"Check #{idx} has invalid status '{check['status']}'.")


def truncate_for_budget(spec: str, diff: str, budget: int = CHAR_BUDGET) -> tuple[str, str]:
    """Truncate spec and diff so combined length is within budget. Returns (spec, diff)."""
    combined = len(spec) + len(diff)
    if combined <= budget:
        return spec, diff

    # Reserve space for separator and truncation notes
    reserve = 120
    available = budget - reserve

    # Split roughly 40% spec / 60% diff (diff often needs more context)
    spec_budget = int(available * 0.4)
    diff_budget = available - spec_budget

    out_spec = spec
    if len(spec) > spec_budget:
        out_spec = spec[:spec_budget] + f"\n\n[truncated - first {spec_budget} chars shown]"

    out_diff = diff
    if len(diff) > diff_budget:
        out_diff = diff[:diff_budget] + f"\n\n[truncated - first {diff_budget} chars shown]"

    return out_spec, out_diff


def _make_llm_failure_review(reason: str) -> dict[str, Any]:
    """Structured fallback used only when fail_closed is disabled."""
    return {
        "verdict": "REVIEW",
        "verdictReason": "LLM check failed; continuing in fail-open mode.",
        "checks": [
            {
                "category": "Structure",
                "name": "LLM runtime failure",
                "status": "warn",
                "notes": reason,
                "source": "llm",
            }
        ],
        "overallNotes": "LLM check could not complete. Local checks still applied.",
    }


def run_matrix(spec: str, diff: str, config: dict[str, Any] | None = None) -> dict[str, Any]:
    config = config or {}
    spec, diff = truncate_for_budget(spec, diff)
    user_prompt = f"SPEC:\n{spec}\n\n---\n\nDIFF:\n{diff}"

    api_key = os.getenv("OPENAI_API_KEY")
    base_url = os.getenv("OPENAI_BASE_URL")
    client_kwargs = {}
    if api_key:
        client_kwargs["api_key"] = api_key
    if base_url:
        client_kwargs["base_url"] = base_url

    try:
        client = OpenAI(**client_kwargs)
        response = client.chat.completions.create(
            model="gpt-4o",
            max_tokens=1000,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
        )
    except Exception as exc:
        message = f"[ERROR] API request failed: {_ascii_safe(str(exc))}. Fix: set OPENAI_API_KEY in .env, check network."
        if _is_fail_closed(config):
            print(message, file=sys.stderr)
            sys.exit(1)
        return _make_llm_failure_review(message)

    try:
        text = response.choices[0].message.content or ""
        clean = text.replace("```json", "").replace("```", "").strip()
        parsed = json.loads(clean)
        validate_matrix_payload(parsed)
        return parsed
    except json.JSONDecodeError as exc:
        message = f"[ERROR] Invalid model response (JSON parse failed): {_ascii_safe(str(exc))}. Fix: retry; if persistent, try SPEC_CHECK_USE_LLM=0 for local-only."
        if _is_fail_closed(config):
            print(message, file=sys.stderr)
            sys.exit(1)
        return _make_llm_failure_review(message)
    except ValueError as exc:
        message = f"[ERROR] Invalid model response (validation failed): {_ascii_safe(str(exc))}. Fix: retry; if persistent, try SPEC_CHECK_USE_LLM=0 for local-only."
        if _is_fail_closed(config):
            print(message, file=sys.stderr)
            sys.exit(1)
        return _make_llm_failure_review(message)


def print_results(data: dict[str, Any]) -> None:
    icons = {"pass": "[OK]", "fail": "[ERROR]", "warn": "[WARN]"}
    verdict_icons = {"COMMIT": "[OK]", "BLOCK": "[BLOCK]", "REVIEW": "[WARN]"}

    verdict = data["verdict"]
    reason = _ascii_safe(str(data.get("verdictReason", "")))
    notes = _ascii_safe(str(data.get("overallNotes", "")))

    print("\n--- Spec Commit Matrix ------------------------------")
    print(f"{verdict_icons[verdict]}  {verdict}: {reason}")
    print("---------------------------------------------------")

    for check in data["checks"]:
        icon = icons.get(check.get("status", "pass"), "[OK]")
        cat = _ascii_safe(str(check.get("category", "")))
        name = _ascii_safe(str(check.get("name", "")))
        check_notes = _ascii_safe(str(check.get("notes", "")))
        print(f"\n{icon}  [{cat}] {name}")
        print(f"   {check_notes}")

    print("\n---------------------------------------------------")
    print(f"[INFO] {notes}")
    print("---------------------------------------------------\n")


def compute_final_verdict(
    local_checks: list[dict[str, Any]],
    llm_result: dict[str, Any] | None,
    llm_advisory: bool = True,
) -> dict[str, Any]:
    """
    Merge local + LLM results with explicit precedence. Pure function for testability.

    Precedence rules:
    1. Any local check with status "fail" -> BLOCK (local critical is authoritative)
    2. LLM BLOCK for spec-conflict categories (Features/Architecture/API Contract/Scope) -> BLOCK
    3. Else if llm_advisory and LLM would BLOCK -> REVIEW (surface only; never block)
    4. Else if LLM result present -> use LLM verdict
    5. Else (no LLM): local "warn" -> REVIEW, else COMMIT

    Checks: local first, then LLM (each with source="local" or "llm").
    """
    all_checks: list[dict[str, Any]] = list(local_checks)
    verdict = "COMMIT"
    verdict_reason = "All checks passed."
    overall_notes = ""

    if llm_result:
        for c in llm_result.get("checks", []):
            all_checks.append({**c, "source": c.get("source", "llm")})
        verdict = llm_result.get("verdict", "COMMIT")
        verdict_reason = llm_result.get("verdictReason", verdict_reason)
        overall_notes = llm_result.get("overallNotes", "")

    llm_spec_conflict_block = False
    if llm_result and llm_result.get("verdict") == "BLOCK":
        for c in llm_result.get("checks", []):
            if c.get("status") != "fail":
                continue
            if c.get("category") in SPEC_CONFLICT_CATEGORIES:
                llm_spec_conflict_block = True
                break

    if any(c.get("status") == "fail" for c in local_checks):
        verdict = "BLOCK"
        verdict_reason = "Local checks found critical issues."
    elif llm_spec_conflict_block:
        verdict = "BLOCK"
        verdict_reason = "LLM found spec-conflict blocking issues."
    elif llm_advisory and verdict == "BLOCK":
        verdict = "REVIEW"
        verdict_reason = "LLM suggested BLOCK (advisory); local checks passed. Review findings above."
    elif not llm_result:
        if any(c.get("status") == "warn" for c in local_checks):
            verdict = "REVIEW"
            verdict_reason = "Local checks found warnings."
            overall_notes = ""
        else:
            verdict = "COMMIT"
            verdict_reason = "All local checks passed."
            overall_notes = "LLM disabled; only local checks ran."

    return {
        "verdict": verdict,
        "verdictReason": verdict_reason,
        "checks": all_checks,
        "overallNotes": overall_notes or verdict_reason,
    }


def main():
    parser = argparse.ArgumentParser(description="Spec Commit Matrix - spec compliance check.")
    parser.add_argument("--generate-spec", action="store_true", help="Generate a draft SPEC.md from repo scan.")
    parser.add_argument("--write", action="store_true", help="With --generate-spec, write to SPEC.md (with confirmation).")
    args = parser.parse_args()

    if args.generate_spec:
        cmd_generate_spec(write=args.write)
        return

    _warn_legacy_files()
    config = load_spec_check_config()
    diff = get_staged_diff(config)
    if not diff.strip():
        sys.exit(0)

    local_checks = run_local_checks(diff, config)
    require_spec = bool(config.get("require_spec", True))
    spec_text = get_spec(config) if (require_spec or _is_llm_enabled(config)) else ""

    llm_result: dict[str, Any] | None = None
    use_llm = _is_llm_enabled(config)

    if use_llm:
        llm_result = run_matrix(spec_text, diff, config=config)

    llm_advisory = config.get("llm_advisory", True)
    data = compute_final_verdict(local_checks, llm_result, llm_advisory=llm_advisory)
    print_results(data)

    if data["verdict"] == "BLOCK" and _is_enforcing(config):
        print("[ERROR] Commit blocked. Fix issues above, then git add and commit again.\n", file=sys.stderr)
        sys.exit(1)
    elif data["verdict"] == "BLOCK":
        print("[WARN] Warning-only: commit allowed. Fix issues or set sign_off_enforcing: true to block.\n", file=sys.stderr)

    sys.exit(0)


if __name__ == "__main__":
    main()
