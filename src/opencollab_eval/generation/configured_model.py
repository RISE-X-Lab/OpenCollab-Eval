"""Configure and observe the model through OpenCollab.agent's public llm argument."""

from __future__ import annotations

import json
import time
from pathlib import Path


class ConfiguredModel:
    def __init__(self, client, *, reasoning_effort="max", observations=None):
        self.client = client
        self.reasoning_effort = reasoning_effort
        self.observations = Path(observations) if observations is not None else None
        self.observation_errors = []
        self.call_count = 0

    @property
    def supports_response_session_identity(self):
        return self.client.supports_response_session_identity

    def context_window(self):
        return self.client.context_window()

    def _record(self, value):
        if self.observations is None:
            return
        try:
            with self.observations.open("a", encoding="utf-8") as output:
                output.write(json.dumps(value, ensure_ascii=False) + "\n")
        except OSError as error:
            self.observation_errors.append(type(error).__name__)

    async def complete(self, messages, tools=None, **kwargs):
        kwargs.setdefault("reasoning_effort", self.reasoning_effort)
        self.call_count += 1
        request = {
            "call": self.call_count,
            "time": time.time(),
            "model": self.client.model,
            "wire_protocol": self.client.wire_protocol,
            "context_window": self.context_window(),
            "reasoning_effort": kwargs.get("reasoning_effort"),
            "max_output_tokens": kwargs.get("max_output_tokens"),
        }
        self._record({**request, "event": "request"})
        started = time.monotonic()
        try:
            result = await self.client.complete(messages, tools=tools, **kwargs)
        except BaseException as error:
            self._record(
                {
                    **request,
                    "event": "exception",
                    "elapsed_seconds": time.monotonic() - started,
                    "error_type": type(error).__name__,
                    "http_status": getattr(error, "status_code", None),
                    "usage_available": False,
                }
            )
            raise
        self._record(
            {
                **request,
                "event": "response",
                "elapsed_seconds": time.monotonic() - started,
                "provider_model": result.provider_model,
                "finish_reason": result.finish_reason,
                "input_tokens": result.usage.input_tokens,
                "output_tokens": result.usage.output_tokens,
                "usage_estimated": result.usage.estimated,
            }
        )
        return result

    async def close(self):
        await self.client.close()
