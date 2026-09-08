"""Ask the endpoint the questions the tables only claim to answer.

Off by default, because every probe is a paid request. The tables audited by
``model_fork_audit`` are somebody's reading of a model card; these three probes
are the endpoint's own answer:

``context``
    How large an input this model really accepts. A long prompt carries a random
    code at *both ends* and the model is asked to echo both. Testing only the
    tail code cannot detect truncation: a provider that silently drops the head
    of an over-long prompt still returns the tail, so a tail-only probe reports
    success for a window the model does not have. Three outcomes are kept apart:
    the request was refused (input over the limit), it was answered with both
    codes (the window holds), and it was answered with the tail code only (the
    head was dropped -- the dangerous one). ``finish_reason == "length"`` is a
    fourth and unrelated thing: the *output* was cut, not the input.
``tool_choice``
    ``auto``, ``required`` and a named function, one request each, keeping the
    HTTP status and the endpoint's own wording. ``supports_forced_tool_choice``
    in OpenCollab's table is exactly this answer, written down by hand.
``max_tokens``
    ``max_tokens=32`` and ``max_completion_tokens=32``, one request each, keeping
    the completion tokens actually produced. Which field OpenCollab sends is
    decided by a regex on the model name; this says whether the endpoint honours
    the field it was sent, ignores it, or rejects it.

Credentials come from the environment (``OPENCOLLAB_API_KEY``, or a file named
by ``OPENCOLLAB_ENV_FILE``) and are never placed in argv, printed, or returned.
"""

from __future__ import annotations

import json
import os
import secrets
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from opencollab_eval.usage import pricing_for_model

PROBE_NAMES = ("context", "tool_choice", "max_tokens")
#: Requests each probe needs before it can say anything at all.
PROBE_COST_IN_REQUESTS = {"context": 3, "tool_choice": 3, "max_tokens": 2}
#: Whitespace words per request in the context probe are a rough token proxy;
#: the numbers below are reported as approximate for that reason.
_WORDS_PER_TOKEN = 1.0

Sender = Callable[[dict[str, Any]], "Response"]


@dataclass
class Response:
    """One endpoint answer, with the credential nowhere in it."""

    status: int
    body: dict[str, Any] = field(default_factory=dict)
    error_text: str = ""

    @property
    def text(self) -> str:
        if self.error_text:
            return self.error_text[:600]
        choices = self.body.get("choices") or [{}]
        message = choices[0].get("message") or {}
        return str(message.get("content") or "")[:600]

    @property
    def finish_reason(self) -> str | None:
        choices = self.body.get("choices") or [{}]
        return choices[0].get("finish_reason")

    @property
    def completion_tokens(self) -> int | None:
        usage = self.body.get("usage") or {}
        value = usage.get("completion_tokens")
        return int(value) if isinstance(value, int) else None

    @property
    def tool_calls(self) -> int:
        choices = self.body.get("choices") or [{}]
        message = choices[0].get("message") or {}
        return len(message.get("tool_calls") or [])


class ProbeUnavailable(RuntimeError):
    """No endpoint to probe, or no credential for it."""


def _read_env_file(name: str) -> str | None:
    """One value out of ``OPENCOLLAB_ENV_FILE``, without reading it aloud."""
    path = os.environ.get("OPENCOLLAB_ENV_FILE")
    if not path:
        return None
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("export "):
            stripped = stripped[len("export "):]
        key, _, value = stripped.partition("=")
        if key.strip() == name:
            return value.strip().strip("'\"") or None
    return None


def credentials() -> tuple[str, str]:
    base_url = os.environ.get("OPENCOLLAB_BASE_URL") or _read_env_file("OPENCOLLAB_BASE_URL")
    api_key = (
        os.environ.get("OPENCOLLAB_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or _read_env_file("OPENCOLLAB_API_KEY")
    )
    if not base_url:
        raise ProbeUnavailable("no OPENCOLLAB_BASE_URL in the environment; nothing to probe")
    if not api_key:
        raise ProbeUnavailable("no OPENCOLLAB_API_KEY in the environment or OPENCOLLAB_ENV_FILE")
    return base_url.rstrip("/"), api_key


def http_sender(base_url: str, api_key: str, *, timeout: float = 600.0) -> Sender:
    """A sender that posts to ``/chat/completions``; the key stays in a header."""

    def send(payload: dict[str, Any]) -> Response:
        request = urllib.request.Request(
            f"{base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed https endpoint
                return Response(status=response.status, body=json.loads(response.read().decode("utf-8")))
        except urllib.error.HTTPError as error:
            return Response(status=error.code, error_text=error.read().decode("utf-8", "replace"))
        except urllib.error.URLError as error:
            return Response(status=0, error_text=f"{type(error).__name__}: {error.reason}")

    return send


def _filler(words: int, head: str, tail: str) -> str:
    """A prompt of roughly ``words`` words with a random code at each end."""
    body = " ".join(f"w{index % 997}" for index in range(max(0, words - 40)))
    return (
        f"HEAD-CODE {head}\n"
        f"{body}\n"
        f"TAIL-CODE {tail}\n"
        "Reply with exactly the two codes above, HEAD first, separated by one space. "
        "Nothing else."
    )


def _classify_context(response: Response, head: str, tail: str) -> str:
    if response.status != 200:
        return "refused"
    text = response.text
    if head in text and tail in text:
        return "both-sentinels"
    if tail in text and head not in text:
        return "head-dropped"
    if head in text and tail not in text:
        return "tail-dropped"
    if response.finish_reason == "length":
        return "output-truncated"
    return "no-sentinel"


def probe_context(model: str, send: Sender, *, budget: int, ceiling_tokens: int) -> dict[str, Any]:
    """Bisect for the largest input this model answers with both codes intact."""
    attempts: list[dict[str, Any]] = []
    low, high = 1_000, max(2_000, ceiling_tokens)
    largest_ok: int | None = None
    smallest_bad: int | None = None
    while budget > 0 and low <= high:
        size = (low + high) // 2
        head, tail = secrets.token_hex(4), secrets.token_hex(4)
        response = send(
            {
                "model": model,
                "messages": [{"role": "user", "content": _filler(int(size * _WORDS_PER_TOKEN), head, tail)}],
                "max_tokens": 64,
            }
        )
        budget -= 1
        verdict = _classify_context(response, head, tail)
        attempts.append(
            {
                "approx_input_tokens": size,
                "http_status": response.status,
                "verdict": verdict,
                "finish_reason": response.finish_reason,
                "endpoint_said": response.text if response.status != 200 else "",
            }
        )
        if verdict == "both-sentinels":
            largest_ok = size
            low = size + 1
        else:
            smallest_bad = size
            high = size - 1
    return {
        "largest_input_answered_with_both_sentinels": largest_ok,
        "smallest_input_that_failed": smallest_bad,
        "requests_spent": len(attempts),
        "attempts": attempts,
        "note": "sizes are approximate: one whitespace word is counted as one token",
    }


_PROBE_TOOL = {
    "type": "function",
    "function": {
        "name": "echo_probe",
        "description": "Echo one string back.",
        "parameters": {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        },
    },
}


def probe_tool_choice(model: str, send: Sender, *, budget: int) -> dict[str, Any]:
    results: dict[str, Any] = {}
    choices: list[tuple[str, Any]] = [
        ("auto", "auto"),
        ("required", "required"),
        ("named", {"type": "function", "function": {"name": "echo_probe"}}),
    ]
    for label, choice in choices:
        if budget <= 0:
            results[label] = {"skipped": "request budget exhausted"}
            continue
        response = send(
            {
                "model": model,
                "messages": [{"role": "user", "content": "Call echo_probe with the value 'ping'."}],
                "tools": [_PROBE_TOOL],
                "tool_choice": choice,
                "max_tokens": 64,
            }
        )
        budget -= 1
        results[label] = {
            "http_status": response.status,
            "tool_calls": response.tool_calls,
            "endpoint_said": response.text if response.status != 200 else "",
        }
    return results


def probe_max_tokens(model: str, send: Sender, *, budget: int) -> dict[str, Any]:
    results: dict[str, Any] = {}
    for field_name in ("max_tokens", "max_completion_tokens"):
        if budget <= 0:
            results[field_name] = {"skipped": "request budget exhausted"}
            continue
        response = send(
            {
                "model": model,
                "messages": [{"role": "user", "content": "Count slowly from 1 to 500, one number per line."}],
                field_name: 32,
            }
        )
        budget -= 1
        results[field_name] = {
            "http_status": response.status,
            "completion_tokens": response.completion_tokens,
            "finish_reason": response.finish_reason,
            "endpoint_said": response.text if response.status != 200 else "",
        }
    return results


def estimate_cost_usd(model: str, *, requests: int, approx_input_tokens: int) -> dict[str, Any]:
    pricing = pricing_for_model(model)
    input_rate = float(pricing["input_usd_per_mtok"])
    output_rate = float(pricing["output_usd_per_mtok"])
    usd = requests * (approx_input_tokens / 1_000_000 * input_rate + 64 / 1_000_000 * output_rate)
    return {
        "pricing_mode": pricing["mode"],
        "requests": requests,
        "approx_usd": round(usd, 4),
        "caveat": (
            "pricing mode 'unset': every rate is 0.00, so this estimate is 0 and means nothing"
            if pricing["mode"] == "unset"
            else "estimate only; the endpoint's own accounting is authoritative"
        ),
    }


def run_probes(
    model: str,
    *,
    requested: str,
    max_requests: int = 10,
    dry_run: bool = False,
    sender: Sender | None = None,
    ceiling_tokens: int = 1_000_000,
) -> dict[str, Any]:
    """Run the named probes under a hard request ceiling."""
    names = [name.strip() for name in requested.split(",") if name.strip()]
    unknown = [name for name in names if name not in PROBE_NAMES]
    if unknown:
        raise ProbeUnavailable(f"unknown probe(s) {unknown}; known probes are {list(PROBE_NAMES)}")

    planned = sum(PROBE_COST_IN_REQUESTS[name] for name in names)
    report: dict[str, Any] = {
        "model": model,
        "probes": names,
        "max_requests": max_requests,
        "planned_requests": planned,
        "cost_estimate": estimate_cost_usd(model, requests=planned, approx_input_tokens=250_000),
    }
    if planned > max_requests:
        report["warning"] = (
            f"{planned} requests planned against a ceiling of {max_requests}; "
            "later probes will be skipped rather than exceed it"
        )
    if dry_run:
        report["dry_run"] = True
        return report

    if sender is None:
        base_url, api_key = credentials()
        sender = http_sender(base_url, api_key)
        report["base_url"] = base_url  # the key is never recorded

    budget = max_requests
    # ``context`` bisects, so it would happily spend the whole ceiling. Give it
    # only what the fixed-cost probes do not need, or asking for all three would
    # silently return nothing for the other two.
    reserved = sum(PROBE_COST_IN_REQUESTS[name] for name in names if name != "context")
    results: dict[str, Any] = {}
    for name in names:
        share = min(budget, budget - reserved if name == "context" else PROBE_COST_IN_REQUESTS[name])
        if share <= 0:
            results[name] = {"skipped": "request budget exhausted"}
            continue
        if name == "context":
            results[name] = probe_context(model, sender, budget=share, ceiling_tokens=ceiling_tokens)
            budget -= int(results[name]["requests_spent"])
        elif name == "tool_choice":
            results[name] = probe_tool_choice(model, sender, budget=share)
            budget -= sum(1 for row in results[name].values() if "skipped" not in row)
        else:
            results[name] = probe_max_tokens(model, sender, budget=share)
            budget -= sum(1 for row in results[name].values() if "skipped" not in row)
    report["results"] = results
    report["requests_remaining"] = budget
    return report
