"""Run OpenCollab's own model-keyed lookups, out of process.

Eval may not import ``opencollab.adapters`` (``tests/test_boundaries.py``), and
a re-implementation of these lookups here would be a second copy of exactly the
kind that ``model_forks`` exists to catch drifting. So the audit asks
OpenCollab itself, in a child interpreter, and compares the answer against what
it read out of the source.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from functools import cache
from typing import Any


class ForkResolutionError(RuntimeError):
    """The audit cannot be trusted: a file moved, or a rule drifted."""


@dataclass(frozen=True)
class ErrorSample:
    """A provider error, replayed against a classifier that has no model key.

    ``errors.py`` and ``retry.py`` branch on message *text*, and which text an
    endpoint produces is a property of the model behind it. So these are
    model-keyed forks in effect even though no model name appears in the code.
    """

    label: str
    message: str
    status: int | None = None
    class_name: str = "APIStatusError"


#: Replayed against ``is_context_overflow_error``. The last two are negative
#: controls: a classifier that answers True for everything would be useless, and
#: this list would not notice without something that must come back False.
DEFAULT_OVERFLOW_SAMPLES: tuple[ErrorSample, ...] = (
    ErrorSample(
        "openai-max-context",
        "This model's maximum context length is 128000 tokens. However, your messages "
        "resulted in 200000 tokens. Please reduce the length of the messages.",
        400,
    ),
    ErrorSample("anthropic-prompt-too-long", "prompt is too long: 250000 tokens > 200000 maximum", 400),
    # Quoted from the probe recorded in OpenCollab's types.py comment for
    # qwen3.8-flash: this is how DashScope words an over-long *input*.
    ErrorSample("dashscope-input-range", "Range of input length should be [1, 991808]", 400),
    # Same endpoint, the *output* limit. Must classify False: the prompt fits.
    ErrorSample("dashscope-output-range", "Range of max_tokens should be [1, 131072]", 400),
    # Must classify False: a bare 400 with no overflow wording.
    ErrorSample("plain-400", "Invalid tool schema for function 'apply_patch'", 400),
)

#: Replayed against ``is_retryable_error``. The last two must come back False.
DEFAULT_RETRY_SAMPLES: tuple[ErrorSample, ...] = (
    ErrorSample("http-429", "Too Many Requests", 429, "APIStatusError"),
    ErrorSample("http-503", "Service Unavailable", 503, "APIStatusError"),
    ErrorSample("gateway-concurrency", "Concurrency limit exceeded for user, please retry later", None, "APIError"),
    ErrorSample("gateway-stream-failed", "Upstream HTTP/2 stream failed", None, "APIError"),
    ErrorSample("transport-connection", "Connection error.", None, "APIConnectionError"),
    ErrorSample("http-400", "Invalid request payload", 400, "BadRequestError"),
    ErrorSample("app-valueerror", "connection error while parsing my own config", None, "ValueError"),
)


_RUNTIME_SNIPPET = r"""
import dataclasses, json, sys
from opencollab.adapters.llm import errors, retry, types, usage_ledger, openai_provider
from opencollab.application.shaping import pipeline

model = sys.argv[1]
samples = json.loads(sys.argv[2])


def build(sample):
    cls = type(sample["class_name"], (Exception,), {})
    err = cls(sample["message"])
    if sample["status"] is not None:
        err.status_code = sample["status"]
    return err


caps = types.model_capabilities(model)
trigger, target = pipeline.history_trigger_target(caps.context_window)
reasoning = openai_provider._uses_reasoning_request_fields(model)
print(json.dumps({
    "types_file": types.__file__,
    "capabilities": dataclasses.asdict(caps),
    "history": {
        "trigger": trigger,
        "target": target,
        "from": "context_window" if caps.context_window and caps.context_window > 0 else "fixed_default",
    },
    "reasoning_request_fields": reasoning,
    "max_output_token_field": "max_completion_tokens" if reasoning else "max_tokens",
    "sends_temperature": not reasoning,
    "sends_top_p": not reasoning,
    "pricing_mode": usage_ledger.pricing_for_model(model)["mode"],
    "overflow": {s["label"]: errors.is_context_overflow_error(build(s)) for s in samples["overflow"]},
    "retry": {s["label"]: retry.is_retryable_error(build(s)) for s in samples["retry"]},
}))
"""


@cache
def _runtime_json(
    model: str,
    overflow_samples: tuple[ErrorSample, ...],
    retry_samples: tuple[ErrorSample, ...],
) -> str:
    """One child process per (model, sample set); importing OpenCollab is slow."""
    payload = {
        "overflow": [vars(sample) for sample in overflow_samples],
        "retry": [vars(sample) for sample in retry_samples],
    }
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-c", _RUNTIME_SNIPPET, model, json.dumps(payload)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if completed.returncode != 0:
        raise ForkResolutionError(
            "could not run OpenCollab's own model lookups in a child process; "
            f"exit {completed.returncode}: {completed.stderr.strip()[-2000:]}"
        )
    return completed.stdout


def opencollab_runtime(
    model: str,
    *,
    overflow_samples: tuple[ErrorSample, ...],
    retry_samples: tuple[ErrorSample, ...],
) -> dict[str, Any]:
    """Run OpenCollab's model-keyed functions in a child process.

    A child rather than an import: Eval's import boundary (``test_boundaries``)
    keeps ``opencollab.adapters`` out of this package, and the alternative --
    re-implementing the classifiers here -- would be exactly the duplicated
    table this tool exists to catch.
    """
    return json.loads(_runtime_json(model, tuple(overflow_samples), tuple(retry_samples)))
