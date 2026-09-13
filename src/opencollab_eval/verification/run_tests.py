"""Evaluation-owned test verification using the public tool/runtime protocol."""

from __future__ import annotations

from typing import Any

from ._run_tests_django import InvalidDjangoTargetError, detect_django_runner
from ._run_tests_go import InvalidGoTargetError as _InvalidGoTargetError
from ._test_results import (
    DEFAULT_RUNNER,
    DEFAULT_TIMEOUT,
    ESCALATE_AFTER,
    MAX_TRACEBACK_CHARS,
    _build_command,
    _format_report,
    _is_green,
    _is_supported_runner,
    _native_fallback_candidate,
    _validate_go_target_before_execution,
    _verification_targets_overlap,
    require_positive_int,
)


class RunTestsTool:
    """Run tests and return a structured pass/fail summary (not raw output).

    Use this after an edit to VERIFY the fix before claiming success. Pass a
    specific path or node-id (e.g. ``tests/test_x.py::test_y``) to keep the run
    fast and focused.
    """

    name = "run_tests"
    default_timeout = DEFAULT_TIMEOUT
    disable_outer_timeout = False

    def to_openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {"name": self.name, "description": self.description, "parameters": self.parameters},
        }

    description = (
        "Run the project's test suite (pytest by default) and return a STRUCTURED "
        "result: pass/fail/error counts, the failing test node-ids, and the head of "
        "the first traceback — not raw stdout. Use it to VERIFY a fix instead of "
        "guessing. Pass `target` (a path or node-id like 'tests/test_x.py::test_y') "
        "to focus the run. "
        "Go projects are auto-detected when pytest is unavailable or collects no tests. "
        "Django source repositories use tests/runtests.py with dotted test labels. "
        "Other native runners return RED before execution until a proof parser exists. "
        "For Go, pass `target` "
        "like './internal/server' or './internal/server::TestEvaluate'. Read the "
        "final 'Verdict: GREEN|RED' line as the authoritative pass/fail signal. "
        "Prefer this over bash for running the test suite; it returns a structured "
        "pass/fail signal. Warnings are NOISE, not failures: the pass/fail decision "
        "is exit-code + failed/error counts only, and warnings are reported on a "
        "separate line. Failures are the signal — do not treat a warning as a "
        "regression."
    )
    parameters = {
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "description": "Test path or node-id (e.g. 'tests/test_x.py::test_y'). "
                "Set this to run just the relevant tests and stay fast; omit only "
                "when you truly need the whole suite.",
            },
            "runner": {
                "type": "string",
                "description": "Base test command (default 'python -m pytest').",
            },
            "extra_args": {
                "type": "string",
                "description": "Extra runner flags appended verbatim (e.g. '-k expr', '-x').",
            },
            "timeout": {
                "type": "number",
                "description": "Timeout in seconds (default 300).",
            },
        },
        "required": [],
    }

    def __init__(
        self,
        max_traceback_chars: int = MAX_TRACEBACK_CHARS,
        *,
        allow_runner_override: bool = True,
        allow_extra_args: bool = True,
        require_process_isolation: bool = False,
    ):
        self.max_traceback_chars = require_positive_int(max_traceback_chars, "max_traceback_chars")
        self.allow_runner_override = allow_runner_override
        self.allow_extra_args = allow_extra_args
        self.require_process_isolation = require_process_isolation
        self._verified_targets: set[str] = set()
        self._verification_records: list[dict[str, object]] = []
        # target -> consecutive RED count, for the escalation nudge. The tool
        # instance is shared across a task's workflow sessions (built once in
        # the eval toolset), so this survives across run_tests calls.
        self._consecutive_fail: dict[str, int] = {}

    def _invalidate_verification_scope(self, target: str) -> None:
        stale = {verified for verified in self._verified_targets if _verification_targets_overlap(target, verified)}
        self._verified_targets.difference_update(stale)

    async def execute_with_runtime(
        self,
        params: dict[str, Any],
        runtime: Any,
    ) -> str:
        target = params.get("target", "")
        self._invalidate_verification_scope(target)
        pinned_runner = params.get("runner")
        extra_args = params.get("extra_args", "")
        timeout = params.get("timeout", DEFAULT_TIMEOUT)
        env = runtime.environment
        safety_policy = runtime.safety_policy

        if env is None:
            return "Error: no execution environment available."
        if self.require_process_isolation and not getattr(env, "process_isolated", False):
            return (
                "Error: run_tests is disabled because this execution environment "
                "does not provide an OS process sandbox."
            )
        if pinned_runner and not self.allow_runner_override:
            return (
                "Error: runner override is disabled for this run_tests tool. "
                "Omit `runner`; run_tests auto-detects pytest and project-native "
                "runners such as Go go.mod, sympy bin/test, Django tests/runtests.py, manage.py, and "
                "tox. For Go, pass `target` like './internal/server' or "
                "'./internal/server::TestEvaluate'."
            )
        if extra_args and not self.allow_extra_args:
            return "Error: extra_args is disabled for this run_tests tool."

        runner = pinned_runner or await detect_django_runner(env) or DEFAULT_RUNNER
        if pinned_runner is not None and not _is_supported_runner(runner):
            return self._unsupported_runner_report(target, runner)
        try:
            # The public API accepts one exact selector. Reject an ambiguous
            # selector list before even the initial auto-detection probe; once
            # collapsed, no later proof parser can recover the original intent.
            _validate_go_target_before_execution(runner, pinned_runner, target)

            result, cmd, runner = await self._run(
                env,
                runner,
                target,
                extra_args,
                timeout,
                safety_policy,
                runtime.confirm_fn(),
            )
            combined = result.stdout + ("\n" + result.stderr if result.stderr else "")

            # Auto-fallback: if the caller did NOT pin a runner and pytest is absent
            # or unsuitable for a Go target, probe once and re-run on the native path.
            if pinned_runner is None:
                native = await _native_fallback_candidate(
                    env,
                    result.returncode,
                    combined,
                )
                if native:
                    if not _is_supported_runner(native):
                        return self._unsupported_runner_report(target, native)
                    result, cmd, runner = await self._run(
                        env,
                        native,
                        target,
                        extra_args,
                        timeout,
                        safety_policy,
                        runtime.confirm_fn(),
                    )
                    combined = result.stdout + ("\n" + result.stderr if result.stderr else "")
        except InvalidDjangoTargetError as exc:
            self._record(target, False)
            return f"Command: not executed\nError: {exc}\nVerdict: RED"
        except _InvalidGoTargetError as exc:
            return self._invalid_go_target_report(target, exc)

        green = _is_green(
            result.returncode,
            combined,
            runner=runner,
            target=target,
            output_truncated=bool(
                getattr(result, "stdout_truncated", False) or getattr(result, "stderr_truncated", False)
            ),
        )
        self._verification_records.append(
            {"target": target, "runner": runner, "command": cmd, "exit_code": result.returncode, "verified": green}
        )
        if target:
            if green:
                self._verified_targets.add(target)
            else:
                self._verified_targets.discard(target)
        streak = self._record(target, green)
        return _format_report(
            cmd,
            result.returncode,
            combined,
            target=target,
            runner=runner,
            green=green,
            fail_streak=streak,
            max_chars=self.max_traceback_chars,
        )

    async def _run(
        self,
        env: Any,
        runner: str,
        target: str,
        extra_args: str,
        timeout: float,
        safety_policy: Any,
        confirm_fn: Any,
    ) -> tuple[Any, str, str]:
        """Build + safety-check + exec one command. Returns (result, cmd, runner)."""
        cmd = _build_command(runner, target, extra_args)
        # Same safety handshake bash uses — a runner override could be anything.
        if safety_policy:
            await safety_policy.check_cmd_interactive(cmd, confirm_fn)
        result = await env.exec_cmd(cmd, timeout=timeout)
        return result, cmd, runner

    def _record(self, target: str, green: bool) -> int:
        """Update + return the consecutive-RED streak for ``target``."""
        if green:
            self._consecutive_fail.pop(target, None)
            return 0
        n = self._consecutive_fail.get(target, 0) + 1
        self._consecutive_fail[target] = n
        return n

    def _invalid_go_target_report(
        self,
        target: str,
        error: _InvalidGoTargetError,
    ) -> str:
        """Fail closed without retaining evidence from an earlier run."""
        if target:
            self._verified_targets.discard(target)
        streak = self._record(target, False)
        parts = [
            "Command: not executed",
            "Exit code: not applicable",
            f"Error: invalid Go target: {error}",
            "Summary: the requested tests cannot be proved by one Go test command.",
            "Verdict: RED",
        ]
        if streak >= ESCALATE_AFTER:
            parts.append(
                f"Escalation: target {target or '(suite)'} has failed "
                f"{streak} runs in a row — split the exact targets into "
                "separate run_tests calls."
            )
        return "\n".join(parts)

    def _unsupported_runner_report(self, target: str, runner: str) -> str:
        """Reject runners that cannot prove exact target execution."""
        if target:
            self._verified_targets.discard(target)
        streak = self._record(target, False)
        parts = [
            "Command: not executed",
            "Exit code: not applicable",
            f"Error: unsupported test runner without an executed-target proof parser: {runner}",
            "Summary: use pytest or Go, or add a parser-backed proof adapter for this runner.",
            "Verdict: RED",
        ]
        if streak >= ESCALATE_AFTER:
            parts.append(
                f"Escalation: target {target or '(suite)'} has failed "
                f"{streak} runs in a row — choose a supported runner or add its proof adapter."
            )
        return "\n".join(parts)

    @property
    def verification_records(self) -> tuple[dict[str, object], ...]:
        return tuple(dict(record) for record in self._verification_records)

    @property
    def verified_targets(self) -> frozenset[str]:
        """Exact requested targets whose latest parser-backed verdict was GREEN."""
        return frozenset(self._verified_targets)


def verification_run_tests_tool() -> RunTestsTool:
    """Build the model-facing verifier with command overrides disabled."""
    return RunTestsTool(allow_runner_override=False, allow_extra_args=False)
