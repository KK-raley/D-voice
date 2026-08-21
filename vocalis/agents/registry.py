"""Agent registry: registration, discovery, and task dispatch."""

from __future__ import annotations

import asyncio
from typing import Any

from vocalis.agents.base import AgentConnector, AgentStatus, TaskRecord
from vocalis.server.events import EventBus, EventType, bus


class AgentRegistry:
    def __init__(self, event_bus: EventBus | None = None) -> None:
        self.bus = event_bus or bus
        self.connectors: dict[str, AgentConnector] = {}
        self.history: list[TaskRecord] = []

    # -- registration --------------------------------------------------
    def register(self, connector: AgentConnector) -> AgentConnector:
        self.connectors[connector.name] = connector
        return connector

    def get(self, name: str) -> AgentConnector:
        if name not in self.connectors:
            raise KeyError(
                f"agent '{name}' not registered - available: {sorted(self.connectors)}"
            )
        return self.connectors[name]

    def list(self) -> list[dict[str, Any]]:
        return [
            {
                "name": c.name,
                "description": c.description,
                "capabilities": c.capabilities,
                "status": c.status.value,
            }
            for c in self.connectors.values()
        ]

    def default_agent(self) -> str:
        return next(iter(self.connectors), "echo")

    # -- dispatch ------------------------------------------------------
    async def dispatch(self, agent: str | None, instruction: str, **kw: Any) -> TaskRecord:
        agent = agent or self.default_agent()
        connector = self.get(agent)
        task_id_hint = TaskRecord(agent=agent, instruction=instruction).id
        await self.bus.publish(
            EventType.TASK_QUEUED, id=task_id_hint, agent=agent, instruction=instruction
        )
        record = await connector.run(instruction, **kw)
        self.history.append(record)
        self.history = self.history[-100:]
        return record

    async def dispatch_many(
        self, assignments: list[tuple[str, str]]
    ) -> list[TaskRecord]:
        tasks = [
            asyncio.create_task(self.dispatch(agent, instruction))
            for agent, instruction in assignments
        ]
        return list(await asyncio.gather(*tasks))

    def snapshot(self) -> dict[str, Any]:
        active = [
            r.to_dict()
            for c in self.connectors.values()
            for r in c.active_tasks.values()
        ]
        return {
            "agents": self.list(),
            "active_tasks": active,
            "recent": [r.to_dict() for r in self.history[-20:]],
        }


# Singleton registry pre-loaded with the offline demo agent.
def build_default_registry(event_bus: EventBus | None = None) -> AgentRegistry:
    registry = AgentRegistry(event_bus)
    from vocalis.agents.echo import EchoAgent

    registry.register(EchoAgent(event_bus))
    try:  # optional integrations
        from vocalis.agents.openai_agent import OpenAIAgent

        registry.register(OpenAIAgent(event_bus))
    except Exception:
        pass
    try:
        from vocalis.agents.claude_code import ClaudeCodeAgent

        registry.register(ClaudeCodeAgent(event_bus))
    except Exception:
        pass
    return registry


global_registry = build_default_registry()
