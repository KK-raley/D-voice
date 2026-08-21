"""EchoAgent: a zero-dependency demo agent.

It simulates a realistic multi-step agent workflow (analyze -> plan ->
execute -> verify) with progress events, so you can experience the full
D-VOICE loop - voice command, live narration, completion chime - before
wiring any external LLM.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from vocalis.agents.base import AgentConnector, TaskRecord


class EchoAgent(AgentConnector):
    name = "echo"
    description = "Built-in offline demo agent (no LLM required)"
    capabilities = ["demo", "simulate-workflow", "narration-test"]

    async def stream_run(
        self, instruction: str, record: TaskRecord, **_: Any
    ) -> AsyncIterator[float | str]:
        steps = [
            ("Analyzing the request", 0.15),
            ("Drafting an execution plan", 0.35),
            ("Executing the plan step by step", 0.65),
            ("Verifying the results", 0.85),
        ]
        for label, progress in steps:
            yield label
            await asyncio.sleep(1.2)  # simulate work
            yield progress

        record.output = (
            f"[echo] Received instruction: {instruction!r}. "
            "Simulated a full agent workflow with live progress reporting. "
            "Connect a real agent (claude-code / openai) to do actual work."
        )
        yield 1.0
