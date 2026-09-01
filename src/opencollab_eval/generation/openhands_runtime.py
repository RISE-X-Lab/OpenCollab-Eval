"""Apply OpenCollab runtime limits to the OpenHands CLI SDK objects."""

from __future__ import annotations

import functools
import json
import math
import os
import subprocess
import threading
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

if __package__:
    from .container_quiescence import (
        guarded_terminal_invocation as _guarded_terminal_invocation,
    )
    from .container_quiescence import (
        stop_guarded_terminal_session as _stop_guarded_terminal_session,
    )
else:
    from .container_quiescence import (
        guarded_terminal_invocation as _guarded_terminal_invocation,
    )
    from .container_quiescence import (
        stop_guarded_terminal_session as _stop_guarded_terminal_session,
    )


@dataclass(frozen=True)
class RuntimeSettings:
    context_window: int | None = None
    temperature: float | None = None
    top_p: float | None = None
    max_output_tokens: int | None = None
    token_budget: int | None = None
    max_steps: int | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> RuntimeSettings:
        values = os.environ if env is None else env

        def optional_int(name: str) -> int | None:
            raw = values.get(name)
            return int(raw) if raw not in (None, "") else None

        def optional_float(name: str) -> float | None:
            raw = values.get(name)
            return float(raw) if raw not in (None, "") else None

        return cls(
            context_window=optional_int("OPENHANDS_CONTEXT_WINDOW"),
            temperature=optional_float("OPENHANDS_TEMPERATURE"),
            top_p=optional_float("OPENHANDS_TOP_P"),
            max_output_tokens=optional_int("OPENHANDS_MAX_OUTPUT_TOKENS"),
            token_budget=optional_int("OPENHANDS_TOKEN_BUDGET"),
            max_steps=optional_int("OPENHANDS_MAX_STEPS"),
        )

    def llm_updates(self) -> dict[str, int | float]:
        candidates = {
            "max_input_tokens": self.context_window,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "max_output_tokens": self.max_output_tokens,
        }
        return {key: value for key, value in candidates.items() if value is not None}


def apply_agent_settings(agent: Any, settings: RuntimeSettings) -> Any:
    updates = settings.llm_updates()
    if not updates:
        return agent
    llm = agent.llm.model_copy(update=updates)
    condenser = getattr(agent, "condenser", None)
    if condenser is not None and getattr(condenser, "llm", None) is not None:
        condenser = condenser.model_copy(
            update={"llm": condenser.llm.model_copy(update=updates)}
        )
    return agent.model_copy(update={"llm": llm, "condenser": condenser})


def _usage_parts(llm: Any) -> tuple[int, int]:
    metrics = getattr(llm, "metrics", None)
    usage = getattr(metrics, "accumulated_token_usage", None)
    if usage is None:
        return 0, 0

    def nonnegative_int(value: Any) -> int:
        # Providers occasionally expose a missing/non-finite usage field while
        # still returning a valid response.  Treat that field as unknown
        # instead of turning accounting itself into a technical failure.  A
        # fractional value is rounded *up*: token counts are conceptually
        # integral, and truncating 1.9 to 1 would silently undercount spend
        # and allow a request to cross the hard budget.
        if value is None or isinstance(value, bool):
            return 0
        if isinstance(value, int):
            return max(value, 0)
        if isinstance(value, str):
            text = value.strip()
            signless = text[1:] if text[:1] in {"+", "-"} else text
            if signless.isdecimal():
                try:
                    return max(int(text), 0)
                except (TypeError, ValueError, OverflowError):
                    return 0
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            return 0
        if not math.isfinite(number) or number < 0:
            return 0
        try:
            return math.ceil(number)
        except (TypeError, ValueError, OverflowError):
            return 0

    return (
        nonnegative_int(getattr(usage, "prompt_tokens", 0)),
        nonnegative_int(getattr(usage, "completion_tokens", 0)),
    )


def _usage_total(llm: Any) -> int:
    prompt, completion = _usage_parts(llm)
    return prompt + completion


def _terminal_action_timeout(value: Any) -> float:
    """Return a finite action timeout without letting bad model input escape."""
    if isinstance(value, bool):
        raise ValueError("terminal action timeout must be finite and positive")
    if value in (None, 0, ""):
        value = 30.0
    try:
        timeout = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("terminal action timeout must be finite and positive") from exc
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("terminal action timeout must be finite and positive")
    return timeout


class TokenBudgetGuard:
    # The output cap is a real upper bound supplied to the provider.  For the
    # input side we learn the observed prompt delta after each response rather
    # than reserving the entire model context window, which is usually many
    # orders of magnitude larger than the actual request.  A small fallback is
    # used only when a caller omitted an output cap altogether.
    _UNKNOWN_OUTPUT_RESERVATION = 4_096

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.spent = 0
        self.reserved = 0
        self._seen_by_llm: dict[int, tuple[int, int]] = {}
        self._request_estimate_by_llm: dict[int, int] = {}
        self._lock = threading.Lock()

    def request_upper_bound(
        self,
        llm: Any,
        *,
        max_output_tokens: int | None = None,
    ) -> int:
        """Estimate one request from the provider output cap and real usage.

        ``max_input_tokens`` is a context-window ceiling, not a promise that
        every request consumes that many tokens.  Reserving it before every
        call made a 16M budget reject ordinary short conversations after only
        a handful of calls.  The first call reserves the configured output
        cap; subsequent calls add the largest prompt delta actually observed
        for this LLM instance.  Actual prompt+completion usage remains
        authoritative in :meth:`record`, so a genuinely over-budget request
        is still rejected after it is measured.
        """
        if max_output_tokens is None:
            max_output_tokens = getattr(llm, "max_output_tokens", None)
        try:
            output = int(max_output_tokens or 0)
        except (TypeError, ValueError, OverflowError):
            output = 0
        if output <= 0:
            output = self._UNKNOWN_OUTPUT_RESERVATION
        with self._lock:
            observed_request = self._request_estimate_by_llm.get(id(llm), 0)
            remaining = self.limit - self.spent - self.reserved
        # Once one response has supplied real usage, reserve that observed
        # request size rather than the provider's much larger output ceiling.
        # If a later request genuinely grows beyond the estimate, ``record``
        # measures the delta and enforces the hard aggregate budget then.
        estimate = observed_request if observed_request > 0 else min(output, self.limit)
        # A previous request can be much larger than the next one.  Never let
        # that historical estimate reject a call solely because less budget
        # remains: the provider's actual usage is authoritative and ``record``
        # still raises if the call crosses the hard aggregate limit.  Keep a
        # positive estimate when no budget remains so ``reserve`` preserves
        # its existing RuntimeError contract instead of failing with the
        # invalid-input ValueError used for non-positive reservations.
        if remaining > 0:
            estimate = min(estimate, remaining)
        return estimate

    def reserve(self, request_upper_bound: int) -> int:
        if isinstance(request_upper_bound, bool) or request_upper_bound <= 0:
            raise ValueError("request_upper_bound must be positive")
        with self._lock:
            projected = self.spent + self.reserved + request_upper_bound
            if projected > self.limit:
                raise RuntimeError(
                    "OpenHands token budget cannot cover the next request "
                    f"({self.spent}+{self.reserved}+{request_upper_bound}>{self.limit})"
                )
            self.reserved += request_upper_bound
        return request_upper_bound

    def record(
        self, llm: Any, *, reservation: int, enforce: bool = True
    ) -> None:
        current_prompt, current_completion = _usage_parts(llm)
        identity = id(llm)
        with self._lock:
            previous_prompt, previous_completion = self._seen_by_llm.get(
                identity, (0, 0)
            )
            prompt_delta = max(current_prompt - previous_prompt, 0)
            completion_delta = max(current_completion - previous_completion, 0)
            self.spent += prompt_delta + completion_delta
            self._seen_by_llm[identity] = (
                max(previous_prompt, current_prompt),
                max(previous_completion, current_completion),
            )
            request_delta = prompt_delta + completion_delta
            if request_delta:
                self._request_estimate_by_llm[identity] = max(
                    self._request_estimate_by_llm.get(identity, 0),
                    request_delta,
                )
            self.reserved = max(self.reserved - reservation, 0)
            exceeded = self.spent > self.limit
        if enforce and exceeded:
            raise RuntimeError(
                f"OpenHands token budget exceeded ({self.spent}/{self.limit})"
            )


@dataclass
class _RuntimeBindings:
    """Mutable values read by the one-time OpenHands monkey patches."""

    container_id: str = ""
    settings: RuntimeSettings = field(default_factory=RuntimeSettings)
    scope_key: tuple[Any, ...] | None = None
    guard: TokenBudgetGuard | None = None


_RUNTIME_BINDINGS = _RuntimeBindings()
# Kept as a compatibility/debug flag for callers that inspected the old
# module-level marker.  The wrappers themselves read ``_RUNTIME_BINDINGS`` so
# a later task can replace the container/settings without stacking wrappers.
_INSTALLED = False


def _isolated_agent_tools(agent: Any, terminal_name: str) -> list[Any]:
    safe_tools = [
        tool
        for tool in getattr(agent, "tools", [])
        if getattr(tool, "name", "") == terminal_name
    ]
    if not safe_tools:
        raise RuntimeError("OpenHands isolated agent is missing TerminalTool")
    return safe_tools


def _active_container_id() -> str:
    container_id = _RUNTIME_BINDINGS.container_id.strip()
    if not container_id:
        raise RuntimeError("OpenHands container isolation requires OPENHANDS_CONTAINER_ID")
    return container_id


def _install_isolated_terminal(container_id: str) -> None:
    """Bind every model-visible shell command to one pre-created container."""
    _RUNTIME_BINDINGS.container_id = container_id
    from openhands.sdk.tool import ToolExecutor
    from openhands.tools.terminal import impl as terminal_impl
    from openhands.tools.terminal.definition import TerminalObservation

    class ContainerTerminalExecutor(ToolExecutor):
        is_pooled = False
        _opencollab_isolated_terminal = True

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            del args
            self.full_output_save_dir = kwargs.get("full_output_save_dir") or os.environ.get(
                "OPENHANDS_OUTPUT_DIR"
            )

        def __call__(self, action: Any, conversation: Any = None) -> Any:
            del conversation
            if action.reset and not action.command.strip():
                return TerminalObservation.from_text(
                    text="Container terminal state reset.",
                    command="[RESET]",
                    exit_code=0,
                )
            if action.is_input:
                return TerminalObservation.from_text(
                    text="Interactive host terminal input is disabled.",
                    is_error=True,
                    command=action.command or "[INPUT]",
                    exit_code=2,
                )
            command = action.command.strip()
            if not command:
                return TerminalObservation.from_text(
                    text="",
                    command="",
                    exit_code=0,
                )
            try:
                timeout = _terminal_action_timeout(getattr(action, "timeout", None))
            except ValueError as exc:
                return TerminalObservation.from_text(
                    text=str(exc),
                    is_error=True,
                    command=command,
                    exit_code=2,
                )
            # Capture the binding for the whole command.  A later task may
            # reconfigure the process while this subprocess is running, but
            # cleanup must still target the container that started it.
            active_container_id = _active_container_id()
            invocation = _guarded_terminal_invocation(active_container_id, command)
            try:
                result = subprocess.run(
                    invocation.argv,
                    input=invocation.source,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=False,
                )
                output = result.stdout
                if result.stderr:
                    output += ("\n" if output else "") + result.stderr
                return TerminalObservation.from_text(
                    text=output,
                    is_error=result.returncode != 0,
                    command=command,
                    exit_code=result.returncode,
                )
            except subprocess.TimeoutExpired as exc:
                output = exc.stdout or ""
                if isinstance(output, bytes):
                    output = output.decode("utf-8", errors="replace")
                try:
                    cleanup = _stop_guarded_terminal_session(
                        active_container_id, invocation
                    )
                except (OSError, subprocess.TimeoutExpired) as cleanup_error:
                    cleanup_returncode = 125
                    cleanup_detail = f"{type(cleanup_error).__name__}: {cleanup_error}"
                else:
                    cleanup_returncode = cleanup.returncode
                    cleanup_detail = cleanup.stderr.strip()
                if cleanup_returncode != 0:
                    output += (
                        "\nContainer command cleanup could not be proven"
                        f" (exit {cleanup_returncode}): {cleanup_detail}"
                    )
                return TerminalObservation.from_text(
                    text=output + f"\nCommand timed out after {timeout:g}s.",
                    is_error=True,
                    command=command,
                    exit_code=-1 if cleanup_returncode == 0 else 125,
                )

        def close(self) -> None:
            return None

    terminal_impl.TerminalExecutor = ContainerTerminalExecutor


def install_runtime_overrides(settings: RuntimeSettings | None = None) -> RuntimeSettings:
    global _INSTALLED
    settings = settings or RuntimeSettings.from_env()

    container_id = os.environ.get("OPENHANDS_CONTAINER_ID", "").strip()
    if not container_id:
        raise RuntimeError("OpenHands container isolation requires OPENHANDS_CONTAINER_ID")

    # Update mutable bindings before the idempotence check.  The original
    # implementation returned here and left every closure pointing at the
    # first task's container/settings, which made subsequent in-process tasks
    # run in the wrong workspace or use the wrong limits.
    bindings = _RUNTIME_BINDINGS
    bindings.container_id = container_id
    bindings.settings = settings
    scope_key = (
        container_id,
        settings,
        os.environ.get("OPENHANDS_INSTANCE_ID", ""),
        os.environ.get("OPENHANDS_OUTPUT_DIR", ""),
    )
    if bindings.scope_key != scope_key:
        bindings.scope_key = scope_key
        bindings.guard = (
            TokenBudgetGuard(settings.token_budget)
            if settings.token_budget is not None
            else None
        )
    output_dir = os.environ.get("OPENHANDS_OUTPUT_DIR")
    if output_dir:
        path = Path(output_dir) / "runtime_config.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(asdict(settings), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    if _INSTALLED:
        return settings

    from openhands.sdk import LLM
    from openhands.tools.terminal import TerminalTool
    from openhands_cli import setup as setup_module
    from openhands_cli.stores.agent_store import AgentStore

    _install_isolated_terminal(container_id)

    original_load_or_create = AgentStore.load_or_create

    @functools.wraps(original_load_or_create)
    def load_or_create(self: Any, *args: Any, **kwargs: Any) -> Any:
        agent = original_load_or_create(self, *args, **kwargs)
        if agent is None:
            return None
        agent = apply_agent_settings(agent, _RUNTIME_BINDINGS.settings)
        safe_tools = _isolated_agent_tools(agent, TerminalTool.name)
        return agent.model_copy(update={"tools": safe_tools})

    AgentStore.load_or_create = load_or_create

    original_conversation = setup_module.Conversation

    @functools.wraps(original_conversation)
    def conversation(*args: Any, **kwargs: Any) -> Any:
        max_steps = _RUNTIME_BINDINGS.settings.max_steps
        if max_steps is not None:
            kwargs["max_iteration_per_run"] = max_steps
        return original_conversation(*args, **kwargs)

    setup_module.Conversation = conversation

    # Install dynamic wrappers even when the first task omitted a token
    # budget; a later in-process task can then enable one without reloading
    # the SDK modules.
    for method_name in ("completion", "responses"):
        original_method = getattr(LLM, method_name)

        @functools.wraps(original_method)
        def guarded_call(
            self: Any,
            *args: Any,
            __original: Any = original_method,
            **kwargs: Any,
        ) -> Any:
            active_guard = _RUNTIME_BINDINGS.guard
            if active_guard is None:
                return __original(self, *args, **kwargs)
            request_upper_bound = active_guard.request_upper_bound(self)
            reservation = active_guard.reserve(request_upper_bound)
            try:
                result = __original(self, *args, **kwargs)
            except BaseException:
                active_guard.record(self, reservation=reservation, enforce=False)
                raise
            active_guard.record(self, reservation=reservation)
            return result

        setattr(LLM, method_name, guarded_call)

    _INSTALLED = True
    return settings
