"""
Targeted tests for spec-check exit codes and gate behavior.
Uses mocks; no network required.

Scenario matrix:
  1) no staged changes => exit 0
  2) missing SPEC.md => exit non-zero (block)
  3) git diff failure => exit non-zero
  4) OpenAI failure => exit non-zero (fail-closed)
  5) malformed model JSON => exit non-zero (fail-closed)
  6) deterministic critical hit => BLOCK / non-zero
  7) deterministic pass + LLM warn => exit 0 (advisory)
  8) warning-only mode => never block, prints warning
  9) enforcing + deterministic fail => block
 10) both entrypoints callable
"""

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Load scripts/Spec_check as a module (no package structure)
_script_path = Path(__file__).resolve().parent.parent / "scripts" / "Spec_check.py"
_spec = importlib.util.spec_from_file_location("spec_check", _script_path)
spec_check = importlib.util.module_from_spec(_spec)
sys.modules["spec_check"] = spec_check
_spec.loader.exec_module(spec_check)


def _run_main(expect_exit: int) -> None:
    """Run main() and assert exit code. Catches SystemExit."""
    with patch.object(sys, "argv", ["Spec_check.py"]):
        with pytest.raises(SystemExit) as excinfo:
            spec_check.main()
        assert excinfo.value.code == expect_exit


def _mock_subprocess_stdout(stdout: str, returncode: int = 0, stderr: str = "") -> MagicMock:
    result = MagicMock()
    result.stdout = stdout
    result.stderr = stderr
    result.returncode = returncode
    return result


# --- Fixtures ---

VALID_LLM_COMMIT = {
    "verdict": "COMMIT",
    "verdictReason": "All good",
    "checks": [{"category": "Security", "name": "Auth", "status": "pass", "notes": "OK"}],
    "overallNotes": "Looks fine.",
}

VALID_LLM_BLOCK_SPEC = {
    "verdict": "BLOCK",
    "verdictReason": "Scope drift",
    "checks": [{"category": "Scope", "name": "Out of scope", "status": "fail", "notes": "New feature."}],
    "overallNotes": "Fix before commit.",
}

VALID_LLM_BLOCK_NONSPEC = {
    "verdict": "BLOCK",
    "verdictReason": "Security concern",
    "checks": [{"category": "Security", "name": "Possible issue", "status": "fail", "notes": "Review needed."}],
    "overallNotes": "Review before merge.",
}

VALID_LLM_REVIEW = {
    "verdict": "REVIEW",
    "verdictReason": "Warnings",
    "checks": [{"category": "Structure", "name": "Style", "status": "warn", "notes": "Consider refactor."}],
    "overallNotes": "Review before merge.",
}


# --- 1) No staged changes => exit 0 ---

def test_no_staged_changes_exit_0():
    """Empty diff -> skip check, exit 0."""
    with patch.object(spec_check, "subprocess") as m_sub:
        m_sub.run.return_value = _mock_subprocess_stdout("")
        with patch.object(spec_check, "load_spec_check_config", return_value={}):
            _run_main(0)
            m_sub.run.assert_called()


# --- 2) Missing SPEC.md => behavior per policy (fail-closed, exit 1) ---

def test_missing_spec_md_exit_1():
    """No SPEC.md with require_spec (default) -> BLOCK, exit 1."""
    def _fake_get_spec_missing(config):
        print("[ERROR] SPEC.md not found.", file=sys.stderr)
        sys.exit(1)

    with patch.object(spec_check, "subprocess") as m_sub:
        m_sub.run.return_value = _mock_subprocess_stdout("+def foo(): pass")
        with patch.object(spec_check, "load_spec_check_config", return_value={}):
            with patch.dict("os.environ", {"SPEC_CHECK_USE_LLM": "1"}, clear=False):
                with patch.object(spec_check, "get_spec", side_effect=_fake_get_spec_missing):
                    _run_main(1)


def test_missing_spec_md_exit_1_when_llm_disabled():
    """No SPEC.md still blocks when require_spec is true, even with LLM disabled."""
    def _fake_get_spec_missing(config):
        print("[ERROR] SPEC.md not found.", file=sys.stderr)
        sys.exit(1)

    with patch.object(spec_check, "subprocess") as m_sub:
        m_sub.run.return_value = _mock_subprocess_stdout("+def foo(): pass")
        with patch.object(spec_check, "load_spec_check_config", return_value={"require_spec": True}):
            with patch.dict("os.environ", {"SPEC_CHECK_USE_LLM": "0"}, clear=False):
                with patch.object(spec_check, "get_spec", side_effect=_fake_get_spec_missing):
                    _run_main(1)


# --- 3) Git diff command failure => non-zero exit ---

def test_git_diff_failure_exit_1():
    """Git diff returns non-zero -> exit 1."""
    with patch.object(spec_check, "subprocess") as m_sub:
        m_sub.run.return_value = _mock_subprocess_stdout("", returncode=1, stderr="fatal: not a git repo")
        with patch.object(spec_check, "load_spec_check_config", return_value={}):
            _run_main(1)


def test_git_diff_oserror_exit_1():
    """Git command not found (OSError) -> exit 1."""
    with patch.object(spec_check, "subprocess") as m_sub:
        m_sub.run.side_effect = OSError("git not found")
        with patch.object(spec_check, "load_spec_check_config", return_value={}):
            _run_main(1)


# --- 4) OpenAI failure => behavior per policy (fail-closed, exit 1) ---

def test_openai_failure_exit_1():
    """OpenAI request raises -> fail-closed, exit 1."""
    with patch.object(spec_check, "subprocess") as m_sub:
        m_sub.run.return_value = _mock_subprocess_stdout("+def bar(): pass")
        with patch.object(spec_check, "load_spec_check_config", return_value={}):
            with patch.dict("os.environ", {"SPEC_CHECK_USE_LLM": "1"}, clear=False):
                with patch.object(spec_check, "get_spec", return_value="# Spec\n## Overview\nTest"):
                    with patch.object(spec_check, "OpenAI") as m_openai:
                        m_openai.return_value.chat.completions.create.side_effect = Exception("API error")
                        _run_main(1)


# --- 5) Invalid/malformed JSON from model => behavior per policy (fail-closed, exit 1) ---

def test_invalid_json_from_model_exit_1():
    """LLM returns non-JSON (parse failure) -> fail-closed, exit 1."""
    with patch.object(spec_check, "subprocess") as m_sub:
        m_sub.run.return_value = _mock_subprocess_stdout("+x = 1")
        with patch.object(spec_check, "load_spec_check_config", return_value={}):
            with patch.dict("os.environ", {"SPEC_CHECK_USE_LLM": "1"}, clear=False):
                with patch.object(spec_check, "get_spec", return_value="# Spec"):
                    with patch.object(spec_check, "OpenAI") as m_openai:
                        resp = MagicMock()
                        resp.choices = [MagicMock()]
                        resp.choices[0].message.content = "This is not JSON at all"
                        m_openai.return_value.chat.completions.create.return_value = resp
                        _run_main(1)


def test_invalid_json_validation_failure_exit_1():
    """LLM returns invalid structure (validation failure) -> fail-closed, exit 1."""
    with patch.object(spec_check, "subprocess") as m_sub:
        m_sub.run.return_value = _mock_subprocess_stdout("+y = 2")
        with patch.object(spec_check, "load_spec_check_config", return_value={}):
            with patch.dict("os.environ", {"SPEC_CHECK_USE_LLM": "1"}, clear=False):
                with patch.object(spec_check, "get_spec", return_value="# Spec"):
                    with patch.object(spec_check, "OpenAI") as m_openai:
                        resp = MagicMock()
                        resp.choices = [MagicMock()]
                        resp.choices[0].message.content = '{"verdict":"COMMIT"}'  # missing required keys
                        m_openai.return_value.chat.completions.create.return_value = resp
                        _run_main(1)


# --- 6) BLOCK verdict => exit 1 ---

def test_block_verdict_exit_1_local_only():
    """Local fail (LLM disabled) + enforcing -> BLOCK -> exit 1."""
    with patch.object(spec_check, "subprocess") as m_sub:
        m_sub.run.return_value = _mock_subprocess_stdout('+api_key = "sk-xxx"')
        with patch.object(spec_check, "load_spec_check_config", return_value={"sign_off_enforcing": True, "enforcement": "enforcing"}):
            with patch.dict("os.environ", {"SPEC_CHECK_USE_LLM": "0"}, clear=False):
                _run_main(1)


def test_block_verdict_from_llm_spec_conflict_blocks_when_enforcing():
    """LLM BLOCK in spec-conflict categories should block even in advisory mode."""
    config = {"sign_off_enforcing": True, "enforcement": "enforcing"}
    with patch.object(spec_check, "subprocess") as m_sub:
        m_sub.run.return_value = _mock_subprocess_stdout("+safe change")
        with patch.object(spec_check, "load_spec_check_config", return_value=config):
            with patch.dict("os.environ", {"SPEC_CHECK_USE_LLM": "1"}, clear=False):
                with patch.object(spec_check, "get_spec", return_value="# Spec"):
                    with patch.object(spec_check, "run_matrix", return_value=VALID_LLM_BLOCK_SPEC):
                        _run_main(1)


def test_block_verdict_exit_1_from_llm_when_not_advisory():
    """LLM returns BLOCK + llm_advisory=false + enforcing -> exit 1."""
    config = {"llm_advisory": False, "sign_off_enforcing": True, "enforcement": "enforcing"}
    with patch.object(spec_check, "subprocess") as m_sub:
        m_sub.run.return_value = _mock_subprocess_stdout("+safe change")
        with patch.object(spec_check, "load_spec_check_config", return_value=config):
            with patch.dict("os.environ", {"SPEC_CHECK_USE_LLM": "1"}, clear=False):
                with patch.object(spec_check, "get_spec", return_value="# Spec"):
                    with patch.object(spec_check, "run_matrix", return_value=VALID_LLM_BLOCK_SPEC):
                        _run_main(1)


# --- 7) REVIEW/COMMIT verdict => exit 0 ---

def test_commit_verdict_exit_0_local_only():
    """Local only, no issues -> COMMIT -> exit 0."""
    with patch.object(spec_check, "subprocess") as m_sub:
        m_sub.run.return_value = _mock_subprocess_stdout("+# comment only")
        with patch.object(spec_check, "load_spec_check_config", return_value={}):
            with patch.dict("os.environ", {"SPEC_CHECK_USE_LLM": "0"}, clear=False):
                _run_main(0)


def test_commit_verdict_exit_0_from_llm():
    """LLM returns COMMIT -> exit 0."""
    with patch.object(spec_check, "subprocess") as m_sub:
        m_sub.run.return_value = _mock_subprocess_stdout("+valid change")
        with patch.object(spec_check, "load_spec_check_config", return_value={}):
            with patch.dict("os.environ", {"SPEC_CHECK_USE_LLM": "1"}, clear=False):
                with patch.object(spec_check, "get_spec", return_value="# Spec"):
                    with patch.object(spec_check, "run_matrix", return_value=VALID_LLM_COMMIT):
                        _run_main(0)


def test_review_verdict_exit_0():
    """LLM returns REVIEW -> exit 0."""
    with patch.object(spec_check, "subprocess") as m_sub:
        m_sub.run.return_value = _mock_subprocess_stdout("+change")
        with patch.object(spec_check, "load_spec_check_config", return_value={}):
            with patch.dict("os.environ", {"SPEC_CHECK_USE_LLM": "1"}, clear=False):
                with patch.object(spec_check, "get_spec", return_value="# Spec"):
                    with patch.object(spec_check, "run_matrix", return_value=VALID_LLM_REVIEW):
                        _run_main(0)


# --- 8) Local deterministic fail + LLM pass => BLOCK per precedence rule ---

def test_local_fail_overrides_llm_pass():
    """Local fail + LLM COMMIT -> BLOCK (local overrides) -> exit 1."""
    with patch.object(spec_check, "subprocess") as m_sub:
        m_sub.run.return_value = _mock_subprocess_stdout('+password = "secret123"')
        with patch.object(spec_check, "load_spec_check_config", return_value={"sign_off_enforcing": True, "enforcement": "enforcing"}):
            with patch.dict("os.environ", {"SPEC_CHECK_USE_LLM": "1"}, clear=False):
                with patch.object(spec_check, "get_spec", return_value="# Spec"):
                    with patch.object(spec_check, "run_matrix", return_value=VALID_LLM_COMMIT):
                        _run_main(1)


# --- 9) Warning-only mode: BLOCK verdict but never exit 1 ---

def test_warning_only_mode_block_exit_0():
    """BLOCK verdict + enforcement=warning_only (config) -> exit 0."""
    with patch.object(spec_check, "subprocess") as m_sub:
        m_sub.run.return_value = _mock_subprocess_stdout('+api_key = "x"')
        with patch.object(spec_check, "load_spec_check_config", return_value={"enforcement": "warning_only"}):
            with patch.dict("os.environ", {"SPEC_CHECK_USE_LLM": "0"}, clear=False):
                _run_main(0)


def test_warning_only_mode_env_exit_0():
    """BLOCK verdict + SPEC_CHECK_ENFORCE=0 -> exit 0."""
    with patch.object(spec_check, "subprocess") as m_sub:
        m_sub.run.return_value = _mock_subprocess_stdout('+api_key = "y"')
        with patch.object(spec_check, "load_spec_check_config", return_value={}):
            with patch.dict("os.environ", {"SPEC_CHECK_USE_LLM": "0", "SPEC_CHECK_ENFORCE": "0"}, clear=False):
                _run_main(0)


# --- Unit test for compute_final_verdict (explicit precedence) ---

def test_compute_final_verdict_local_fail_overrides_llm():
    """Pure function: local fail + LLM COMMIT -> BLOCK."""
    local_checks = [{"category": "Security", "name": "Secret", "status": "fail", "notes": "x", "source": "local"}]
    llm_result = VALID_LLM_COMMIT
    result = spec_check.compute_final_verdict(local_checks, llm_result, llm_advisory=True)
    assert result["verdict"] == "BLOCK"
    assert result["verdictReason"] == "Local checks found critical issues."


def test_compute_final_verdict_llm_block_advisory():
    """Pure function: spec-conflict LLM BLOCK should still BLOCK in advisory mode."""
    local_checks = [{"category": "Security", "name": "Auth", "status": "pass", "notes": "OK", "source": "local"}]
    llm_result = VALID_LLM_BLOCK_SPEC
    result = spec_check.compute_final_verdict(local_checks, llm_result, llm_advisory=True)
    assert result["verdict"] == "BLOCK"
    assert "spec-conflict" in result["verdictReason"].lower()


def test_compute_final_verdict_llm_block_nonspec_stays_advisory():
    """Pure function: non-spec LLM BLOCK remains REVIEW in advisory mode."""
    local_checks = [{"category": "Security", "name": "Auth", "status": "pass", "notes": "OK", "source": "local"}]
    llm_result = VALID_LLM_BLOCK_NONSPEC
    result = spec_check.compute_final_verdict(local_checks, llm_result, llm_advisory=True)
    assert result["verdict"] == "REVIEW"
    assert "advisory" in result["verdictReason"].lower()


# --- 7) Deterministic pass + LLM warn => exit 0 (advisory) ---

def test_deterministic_pass_llm_warn_exit_0():
    """Local pass + LLM REVIEW (warn) -> exit 0; LLM is advisory."""
    with patch.object(spec_check, "subprocess") as m_sub:
        m_sub.run.return_value = _mock_subprocess_stdout("+# safe comment")
        with patch.object(spec_check, "load_spec_check_config", return_value={}):
            with patch.dict("os.environ", {"SPEC_CHECK_USE_LLM": "1"}, clear=False):
                with patch.object(spec_check, "get_spec", return_value="# Spec"):
                    with patch.object(spec_check, "run_matrix", return_value=VALID_LLM_REVIEW):
                        _run_main(0)


# --- 8) Warning-only mode: never block, prints warning ---

def test_warning_only_prints_warning(capsys):
    """Warning-only mode: BLOCK would occur but exit 0 + prints [WARN]."""
    with patch.object(spec_check, "subprocess") as m_sub:
        m_sub.run.return_value = _mock_subprocess_stdout('+api_key = "sk-xxx123"')  # 3+ chars to trigger rule
        with patch.object(spec_check, "load_spec_check_config", return_value={"enforcement": "warning_only"}):
            with patch.dict("os.environ", {"SPEC_CHECK_USE_LLM": "0", "SPEC_CHECK_ENFORCE": "0"}, clear=False):
                _run_main(0)
    captured = capsys.readouterr()
    assert "[WARN]" in (captured.out + captured.err) or "Warning-only" in (captured.out + captured.err) or "sign_off_enforcing" in (captured.out + captured.err)


# --- 9) Enforcing mode + deterministic fail => block ---

def test_enforcing_mode_deterministic_fail_blocks():
    """Enforcing + local critical hit -> BLOCK -> exit 1."""
    with patch.object(spec_check, "subprocess") as m_sub:
        m_sub.run.return_value = _mock_subprocess_stdout('+eval(user_input)')
        with patch.object(spec_check, "load_spec_check_config", return_value={"sign_off_enforcing": True, "enforcement": "enforcing"}):
            with patch.dict("os.environ", {"SPEC_CHECK_USE_LLM": "0"}, clear=False):
                _run_main(1)


# --- 10) Both entrypoints callable ---

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_entrypoint_canonical_callable():
    """scripts/Spec_check.py is callable; runs and exits (no network)."""
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "Spec_check.py")],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env={**os.environ, "SPEC_CHECK_USE_LLM": "0"},
        timeout=5,
    )
    # Empty diff -> 0; git failure -> 1. Either is valid (no mock in subprocess).
    assert result.returncode in (0, 1)


def test_entrypoint_shim_callable():
    """Root Spec_check.py shim delegates to scripts/Spec_check.py."""
    shim = REPO_ROOT / "Spec_check.py"
    if not shim.exists():
        pytest.skip("Root Spec_check.py shim not found")
    result = subprocess.run(
        [sys.executable, str(shim)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env={**os.environ, "SPEC_CHECK_USE_LLM": "0"},
        timeout=15,
    )
    assert result.returncode in (0, 1)
