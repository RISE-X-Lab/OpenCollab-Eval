"""Parser-backed public-test evidence migrated from OpenCollab before #95."""

from __future__ import annotations

import posixpath
import re
import shlex
from typing import Any

from ._run_tests_django import django_command, django_evidence, django_has_pass_proof, is_django_runner
from ._run_tests_go import go_has_pass_proof as _go_has_pass_proof
from ._run_tests_go import go_runner_command as _go_runner_command
from ._run_tests_go import go_target_specs as _go_target_specs
from ._run_tests_go import has_multiple_go_selector_tokens as _has_multiple_go_selector_tokens
from ._run_tests_go import is_go_runner as _is_go_runner
from ._run_tests_go import translate_go_target_args as _translate_go_target_args

# Keep the traceback head bounded; full dumps explode the context (ref: bash.py).
MAX_TRACEBACK_CHARS = 6_000
DEFAULT_RUNNER = "python -m pytest"
DEFAULT_TIMEOUT = 300.0
# After this many consecutive failing runs of the SAME target, nudge the model
# to change approach instead of re-running the identical failing assertion.
ESCALATE_AFTER = 3
# Project-native runners, probed in order when the caller did not pin ``runner``
# (or when pytest is missing). Each entry: (probe-cmd that exits 0 iff present,
# base runner command). bin/test is sympy's; manage.py is Django's; tox is the
# generic multi-env runner. Pytest is always tried first via DEFAULT_RUNNER.
# go.mod is probed LAST on purpose: the pytest-collected-nothing fallback only
# trusts a Go runner, so returning "go test" first on a mixed Python+Go repo
# would green off unrelated Go tests while the intended Python suite never ran.
# Keeping go.mod last makes _detect_native_runner surface Go only when it is the
# sole native signal.
_NATIVE_PROBES: tuple[tuple[str, str], ...] = (
    ("test -x bin/test", "python bin/test"),
    ("test -f manage.py", "python manage.py test"),
    ("test -f tox.ini", "tox"),
    ("test -f go.mod", "go test"),
)

# Count tokens pytest prints in its final summary line, e.g.
# "===== 1 failed, 2 passed, 1 skipped in 0.12s =====".
_COUNT_RE = re.compile(r"(\d+)\s+(passed|failed|errors?|skipped|xfailed|xpassed|deselected|warnings?)")
_PYTEST_SUMMARY_RE = re.compile(
    r"(?:\d+\s+(?:passed|failed|errors?|skipped|xfailed|xpassed|deselected|warnings?)"
    r"(?:,\s*)?)+\s+in\s+\d+(?:\.\d+)?s(?:\s+\([^)]+\))?",
    re.IGNORECASE,
)
_PYTEST_NO_TESTS_RE = re.compile(
    r"no tests ran in \d+(?:\.\d+)?s(?:\s+\([^)]+\))?",
    re.IGNORECASE,
)
_PYTEST_MISSING_RE = re.compile(
    r"(?:(?:\S*/)?(?:python(?:\d+(?:\.\d+)*)?|pypy\d*): )?"
    r"No module named pytest"
)


def _normalize_verification_target(target: str) -> str:
    path, separator, selectors = target.partition("::")
    normalized_path = posixpath.normpath(path)
    if separator:
        return f"{normalized_path}::{selectors}"
    return normalized_path


def _verification_target_covers(parent: str, child: str) -> bool:
    normalized_parent = _normalize_verification_target(parent)
    normalized_child = _normalize_verification_target(child)
    if normalized_parent == ".":
        return True
    if normalized_parent == normalized_child:
        return True
    if normalized_child.startswith(f"{normalized_parent}::"):
        return True
    parent_path, parent_separator, _ = normalized_parent.partition("::")
    child_path, _, _ = normalized_child.partition("::")
    return not parent_separator and child_path.startswith(f"{parent_path}/")


def _verification_targets_overlap(left: str, right: str) -> bool:
    return _verification_target_covers(left, right) or _verification_target_covers(right, left)


def require_positive_int(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def truncate(text: str, max_chars: int, label: str | None = None) -> str:
    """Keep head + tail, drop the middle to avoid context explosion."""
    require_positive_int(max_chars, "max_chars")
    if len(text) <= max_chars:
        return text
    dropped = len(text) - max_chars
    marker = (
        f"\n\n... [{dropped} chars of {label} truncated] ...\n\n"
        if label is not None
        else f"\n\n... [{dropped} chars truncated] ...\n\n"
    )
    if len(marker) >= max_chars:
        return marker[:max_chars]
    source_budget = max_chars - len(marker)
    head = (source_budget + 1) // 2
    tail = source_budget - head
    suffix = text[-tail:] if tail else ""
    result = text[:head] + marker + suffix
    assert len(result) <= max_chars
    return result


_ENV_ASSIGNMENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*", re.DOTALL)
_PYTHON_EXECUTABLE_RE = re.compile(r"(?:python(?:\d+(?:\.\d+)*)?|pypy\d*)")
_PYTHON_FLAG_OPTIONS = frozenset({"-B", "-E", "-I", "-O", "-OO", "-P", "-q", "-s", "-S", "-u", "-v"})


def _python_invokes_pytest(parts: list[str]) -> bool:
    executable = parts[0].rsplit("/", 1)[-1]
    if _PYTHON_EXECUTABLE_RE.fullmatch(executable) is None:
        return False
    index = 1
    while index < len(parts):
        part = parts[index]
        if part == "-m":
            return index + 1 < len(parts) and parts[index + 1] == "pytest"
        if part in {"-W", "-X", "--check-hash-based-pycs"}:
            index += 2
            continue
        if part.startswith(("-W", "-X")) and len(part) > 2:
            index += 1
            continue
        if part in _PYTHON_FLAG_OPTIONS:
            index += 1
            continue
        return False
    return False


def _parts_invoke_pytest(parts: list[str], *, depth: int = 0) -> bool:
    """Recognize supported direct wrappers without accepting shell interpreters."""
    if not parts or depth > 3:
        return False
    executable = parts[0].rsplit("/", 1)[-1]
    if executable == "pytest":
        return True
    if _python_invokes_pytest(parts):
        return True
    if executable == "env":
        index = 1
        while index < len(parts) and _ENV_ASSIGNMENT_RE.fullmatch(parts[index]):
            index += 1
        if index < len(parts) and parts[index] == "--":
            index += 1
        return _parts_invoke_pytest(parts[index:], depth=depth + 1)
    if executable in {"uv", "poetry", "pipenv"} and len(parts) >= 3 and parts[1] == "run":
        index = 3 if parts[2] == "--" else 2
        return _parts_invoke_pytest(parts[index:], depth=depth + 1)
    return executable in {"coverage", "coverage3"} and len(parts) >= 4 and parts[1:4] == ["run", "-m", "pytest"]


def _is_pytest_runner(runner: str) -> bool:
    """Whether ``runner`` directly invokes pytest (so pytest flags are safe)."""
    if any(token in runner for token in ("\n", "\r", ";", "&", "|", "<", ">", "`", "$(")):
        return False
    try:
        parts = shlex.split(runner)
    except ValueError:
        return False
    return _parts_invoke_pytest(parts)


def _is_supported_runner(runner: str) -> bool:
    return _is_pytest_runner(runner) or _is_go_runner(runner) or is_django_runner(runner)


def _validate_go_target_before_execution(
    runner: str,
    pinned_runner: str | None,
    target: str,
) -> None:
    """Reject unprovable Go target lists before safety checks or execution."""
    if _is_go_runner(runner) or (pinned_runner is None and _has_multiple_go_selector_tokens(target)):
        _go_target_specs(target)


def _build_command(runner: str, target: str, extra_args: str) -> str:
    # --tb=short keeps tracebacks compact; -rfE forces a failed/error summary
    # block even under -q so we can list failing node-ids reliably. -rA adds a
    # per-test short summary (incl. PASSED) so a downstream gate can confirm a
    # NAMED test went green; -p no:cacheprovider makes runs deterministic.
    if _is_pytest_runner(runner):
        parts = [runner, "--tb=short", "-rfE", "-rA", "-p", "no:cacheprovider", "-q"]
        if target:
            parts.append(shlex.quote(target))
    elif is_django_runner(runner):
        parts = [django_command(runner, target)]
    elif _is_go_runner(runner):
        parts = [_go_runner_command(runner), "-json"]
        parts.extend(_translate_go_target_args(target))
    else:
        raise ValueError(f"unsupported test runner without proof parser: {runner}")
    if extra_args:
        parts.append(extra_args)
    return " ".join(parts)


async def _detect_native_runner(env: Any) -> str | None:
    """Probe the workspace for a project-native runner; None if none found."""
    for probe, runner in _NATIVE_PROBES:
        try:
            result = await env.exec_cmd(probe, timeout=10.0)
        except Exception:
            continue
        if getattr(result, "returncode", 1) == 0:
            return runner
    return None


async def _native_fallback_candidate(
    env: Any,
    returncode: int,
    output: str,
) -> str | None:
    if _pytest_missing(returncode, output):
        return await _detect_native_runner(env)
    if _pytest_no_tests(returncode, output):
        native = await _detect_native_runner(env)
        if native and _is_go_runner(native):
            return native
    return None


def _pytest_missing(returncode: int, output: str) -> bool:
    """Whether the run failed because pytest itself is absent."""
    if returncode == 0:
        return False
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    return len(lines) == 1 and _PYTEST_MISSING_RE.fullmatch(lines[0]) is not None


def _pytest_no_tests(returncode: int, output: str) -> bool:
    """Whether pytest ran successfully enough to report an empty selection."""
    return returncode == 5 and "no tests ran" in output.lower()


def _summary_lines(output: str) -> list[str]:
    """Return every complete pytest result summary in emission order."""
    summaries = []
    for line in output.splitlines():
        candidate = line.strip().strip("= ").strip()
        if _PYTEST_SUMMARY_RE.fullmatch(candidate) or _PYTEST_NO_TESTS_RE.fullmatch(candidate):
            summaries.append(candidate)
    return summaries


def _summary_line(output: str) -> str | None:
    """The last complete pytest result summary, with or without ``====``."""
    summaries = _summary_lines(output)
    return summaries[-1] if summaries else None


def _parse_counts(summary: str | None) -> tuple[dict[str, int], int]:
    """Parse the summary line into (decision-relevant counts, warnings).

    Warnings are kept parseable but pulled OUT of the returned counts: the
    pass/fail decision is exit-code + failed/error counts only, so a noisy
    warning must never masquerade as a regression. They are returned separately
    for a 'Warnings: N (not failures)' line.
    """
    counts: dict[str, int] = {}
    warnings = 0
    if not summary:
        return counts, warnings
    for m in _COUNT_RE.finditer(summary):
        token = m.group(2)
        if token.startswith("warning"):
            warnings += int(m.group(1))
            continue
        key = token.rstrip("s") if token.startswith("error") else token
        counts[key] = counts.get(key, 0) + int(m.group(1))
    return counts, warnings


def _target_has_pass_proof(target: str, passed_lines: list[str]) -> bool:
    """Whether pytest's per-test summary proves the requested target ran."""
    if not target:
        return True
    normalized = target.removeprefix("./")
    target_path, has_selector, _selector = normalized.partition("::")
    target_path = target_path.rstrip("/")
    for line in passed_lines:
        node_id = line.removeprefix("PASSED ").strip()
        candidate = node_id.removeprefix("./")
        if has_selector and not target_path:
            candidate_selector = candidate.partition("::")[2]
            if candidate_selector == _selector or candidate_selector.startswith(_selector + "["):
                return True
            continue
        if candidate == normalized or candidate.startswith(normalized + "::"):
            return True
        if has_selector and candidate.startswith(normalized + "["):
            return True
        candidate_path = candidate.partition("::")[0]
        if not has_selector and (
            target_path in {"", "."} or candidate_path == target_path or candidate_path.startswith(target_path + "/")
        ):
            return True
    return False


def _is_green(
    returncode: int,
    output: str,
    *,
    runner: str = DEFAULT_RUNNER,
    target: str = "",
    output_truncated: bool = False,
) -> bool:
    """The GREEN verdict's positive-proof specification.

    A run is GREEN only on *positive evidence* that a requested test actually
    executed and passed — never on a bare exit code, which a no-op command or a
    zero-test mode can forge. Each runner family plugs in its own proof adapter
    (pytest: exactly one summary line carrying a passing count plus per-target
    pass proof; go: parsed ``PASS`` lines); a runner with no parser-backed
    adapter cannot authorize GREEN and returns ``False`` by construction.
    """
    if output_truncated:
        # A bounded capture may have dropped the only pass/failure evidence;
        # a retained summary cannot certify the complete run.
        return False
    if is_django_runner(runner):
        return returncode == 0 and django_has_pass_proof(target, output)
    summaries = _summary_lines(output)
    if _is_pytest_runner(runner) and len(summaries) != 1:
        # One tool invocation represents one pytest session. Multiple result
        # summaries are ambiguous and let an appended passing summary hide an
        # earlier failure; no summary proves no pytest session completed.
        return False
    summary = summaries[0] if summaries else None
    counts, _ = _parse_counts(summary)
    if counts.get("failed", 0) or counts.get("error", 0):
        return False
    if returncode != 0:
        return False
    if _is_pytest_runner(runner):
        if counts.get("passed", 0) <= 0:
            return False
        return _target_has_pass_proof(target, _passed_tests(output))
    if _is_go_runner(runner):
        return _go_has_pass_proof(target, output)
    # Native runners need an explicit, parser-backed proof adapter before their
    # output can authorize a GREEN verdict. A bare exit code is forgeable via
    # no-op commands and zero-test modes.
    return False


def _missing_substring_hint(output: str) -> str | None:
    """Best-effort 'expected X, got Y' hint from the first assertion diff."""
    for line in output.splitlines():
        s = line.strip()
        if s.startswith("E   ") and "assert" in s:
            return s[len("E   ") :].strip()
        if s.startswith("assert "):
            return s
    return None


def _failed_tests(output: str) -> list[str]:
    fails = []
    for line in output.splitlines():
        s = line.strip()
        if s.startswith("FAILED ") or s.startswith("ERROR "):
            fails.append(s)
    return fails


def _passed_tests(output: str) -> list[str]:
    """Node-ids reported as PASSED in the -rA short summary (default pytest)."""
    passes = []
    for line in output.splitlines():
        s = line.strip()
        if s.startswith("PASSED "):
            passes.append(s)
    return passes


def _traceback_head(output: str, max_chars: int = MAX_TRACEBACK_CHARS) -> str:
    """Head of the first FAILURES/ERRORS section (or a raw Python traceback)."""
    for marker in ("= FAILURES =", "= ERRORS =", "Traceback (most recent call last)"):
        idx = output.find(marker)
        if idx != -1:
            section = output[idx:]
            end = section.find("= short test summary info =")
            if end != -1:
                section = section[:end]
            return truncate(section.strip(), max_chars)
    return ""


def _format_report(
    cmd: str,
    returncode: int,
    output: str,
    target: str = "",
    runner: str = DEFAULT_RUNNER,
    green: bool | None = None,
    fail_streak: int = 0,
    max_chars: int = MAX_TRACEBACK_CHARS,
) -> str:
    if green is None:
        green = _is_green(returncode, output, runner=runner, target=target)
    if _is_pytest_runner(runner) and _pytest_missing(returncode, output):
        # Still emit a parseable verdict so the gate/model is never left without
        # a signal. pytest-missing is RED (the named tests could not run) and
        # points the model at auto-detected native runners.
        return (
            f"Command: {cmd}\nExit code: {returncode}\n"
            "Error: pytest not found and no project-native runner detected. "
            "Omit `runner` so run_tests can auto-detect Go go.mod, sympy "
            "bin/test, Django manage.py, or tox. For Go, pass `target` like "
            "'./internal/server' or './internal/server::TestEvaluate'.\n"
            "Verdict: RED (tests could not run)\n"
            f"{truncate(output.strip(), 1_000)}"
        )

    summary = _summary_line(output)
    counts, warnings = _parse_counts(summary)
    failed = _failed_tests(output)
    passed = _passed_tests(output)
    if is_django_runner(runner):
        counts, passed, failed, total, _valid = django_evidence(output)
        summary = f"Django unittest runner executed {total} tests" if total is not None else None

    parts = [f"Command: {cmd}", f"Exit code: {returncode}"]
    if counts:
        # Warnings are deliberately excluded — the pass/fail decision is
        # exit-code + failed/error counts only, never a warning count.
        parts.append("Counts: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    if warnings:
        parts.append(f"Warnings: {warnings} (not failures)")
    if summary:
        parts.append(f"Summary: {summary}")
    else:
        verdict_word = "GREEN" if green else "RED"
        parts.append(f"Summary: no parser-backed executed-test proof; exit code {returncode} -> {verdict_word}")

    if failed:
        shown = failed[:25]
        parts.append("Failed/errored tests:")
        parts.extend(f"  - {line}" for line in shown)
        if len(failed) > len(shown):
            parts.append(f"  ... and {len(failed) - len(shown)} more")

    # List PASSED node-ids only for a focused run (a named target was requested
    # or the -rA summary is present). For a full-suite run the PASSED list is
    # suppressed to protect context — the aggregate count is enough. This lets a
    # downstream gate confirm a NAMED test went green.
    if passed and target:
        shown_pass = passed[:25]
        parts.append("Passed tests:")
        parts.extend(f"  - {line}" for line in shown_pass)
        if len(passed) > len(shown_pass):
            parts.append(f"  ... and {len(passed) - len(shown_pass)} more")

    head = _traceback_head(output, max_chars)
    if head:
        parts.append("First failure detail:\n" + head)

    # No structured signal at all (e.g. collection crash) — fall back to output.
    if not counts and not failed and not head:
        parts.append("Output:\n" + truncate(output.strip(), max_chars))

    # Always emit an authoritative one-line verdict, even with no summary line.
    parts.append(f"Verdict: {'GREEN' if green else 'RED'}")
    if not green:
        hint = _missing_substring_hint(output)
        if hint:
            parts.append(f"Hint (expected vs got): {hint}")
        if fail_streak >= ESCALATE_AFTER:
            parts.append(
                f"Escalation: target {target or '(suite)'} has failed "
                f"{fail_streak} runs in a row — stop re-running the same "
                "assertion and try a different fix or approach."
            )

    return "\n".join(parts)
