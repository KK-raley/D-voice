"""OpenAI-compatible API connector (works with any /v1/chat/completions endpoint).

Configure via environment variables:
    OPENAI_API_KEY   - required
    OPENAI_BASE_URL  - optional (default: https://api.openai.com/v1)
    OPENAI_MODEL     - optional (default: gpt-4o-mini)
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any

from vocalis.agents.base import AgentConnector, TaskRecord

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"


class OpenAIAgent(AgentConnector):
    name = "openai"
    description = "OpenAI-compatible chat API agent"
    capabilities = ["chat", "reasoning", "general-q&a"]

    def __init__(self, event_bus=None, api_key: str | None = None, base_url: str | None = None,
                 model: str | None = None) -> None:
        super().__init__(event_bus)
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self.base_url = (base_url or os.environ.get("OPENAI_BASE_URL", DEFAULT_BASE_URL)).rstrip("/")
        self.model = model or os.environ.get("OPENAI_MODEL", DEFAULT_MODEL)

    async def health_check(self) -> bool:
        return bool(self.api_key)

    async def stream_run(
        self, instruction: str, record: TaskRecord, **_: Any
    ) -> AsyncIterator[float | str]:
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY is not set")
        import httpx

        yield "Querying the model..."
        yield 0.3
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": self.model,
                    "messages": [
                        {
                            "role": "system",
                            "content": "You are an agent executing user instructions. Be concise.",
                        },
                        {"role": "user", "content": instruction},
                    ],
                },
            )
        yield 0.8
        resp.raise_for_status()
        record.output = resp.json()["choices"][0]["message"]["content"]
        yield 1.0
