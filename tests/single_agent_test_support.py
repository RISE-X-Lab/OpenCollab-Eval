"""A deterministic two-request model used by agent budget tests."""

from types import SimpleNamespace


class Usage:
    def __init__(self, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.markup_recovered = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class TwoStepLLM:
    def __init__(self) -> None:
        self.calls = 0

    def context_window(self) -> int:
        return 4_000_000

    async def complete(self, messages, tools=None, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return SimpleNamespace(
                content=None,
                tool_calls=[
                    {
                        "id": "read-1",
                        "type": "function",
                        "function": {
                            "name": "file_read",
                            "arguments": '{"path":"marker.txt"}',
                        },
                    }
                ],
                usage=Usage(1_100_000, 10),
                finish_reason="tool_calls",
                reasoning=None,
                provider_items=[],
                provider_state=None,
            )
        return SimpleNamespace(
            content="finished",
            tool_calls=[],
            usage=Usage(20, 2),
            finish_reason="stop",
            reasoning=None,
            provider_items=[],
            provider_state=None,
        )

