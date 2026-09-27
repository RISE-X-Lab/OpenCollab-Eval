"""Run OpenCollab's own model-keyed lookups, out of process.

The public ``opencollab.models`` facade supplies model behavior facts.
The audit runs that facade in a child interpreter and compares its output with
the attributed lines read by the source archaeology tool.
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
import json, sys
from opencollab.models import inspect_model_runtime

samples = json.loads(sys.argv[2])
print(json.dumps(inspect_model_runtime(
    sys.argv[1],
    overflow_samples=samples["overflow"],
    retry_samples=samples["retry"],
)))
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

    The child calls the public model inspection facade. Source attribution
    remains a separate AST-based operation over an explicitly located checkout.
    """
    return json.loads(_runtime_json(model, tuple(overflow_samples), tuple(retry_samples)))
