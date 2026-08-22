"""Agent registry: registration, discovery, and task dispatch."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from vocalis.agents.base import AgentConnector, TaskRecord
from vocalis.server.events import EventBus, EventType, bus

logger = logging.getLogger("vocalis.agents")


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
                "capabilities": list(c.capabilities),
                "status": c.status.value,
            }
            for c in self.connectors.values()
        ]

    def default_agent(self) -> str:
        if not self.connectors:
            raise RuntimeError("no agents registered - register a connector first")
        # Prefer the offline demo agent when present.
        if "echo" in self.connectors:
            return "echo"
        return next(iter(self.connectors))

    # -- dispatch ------------------------------------------------------
    async def dispatch(self, agent: str | None, instruction: str, **kw: Any) -> TaskRecord:
        agent = agent or self.default_agent()
        connector = self.get(agent)
        # Create the record up-front so queued/started/progress events all
        # carry the same id (the HUD relies on this for task timelines).
        record = TaskRecord(agent=agent, instruction=instruction)
        await self.bus.publish(EventType.TASK_QUEUED, **record.to_dict())
        record = await connector.run(instruction, record=record, **kw)
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


def build_default_registry(
    event_bus: EventBus | None = None, config: Any = None
) -> AgentRegistry:
    """Registry pre-loaded with the offline demo agent + any optional integrations."""
    registry = AgentRegistry(event_bus)
    from vocalis.agents.echo import EchoAgent

    registry.register(EchoAgent(event_bus))
    try:  # optional integrations - missing deps must not be silent
        from vocalis.agents.openai_agent import OpenAIAgent

        registry.register(OpenAIAgent(event_bus))
    except Exception:
        logger.debug("openai connector unavailable", exc_info=True)
    try:
        from vocalis.agents.claude_code import ClaudeCodeAgent

        registry.register(ClaudeCodeAgent(event_bus))
    except Exception:
        logger.debug("claude-code connector unavailable", exc_info=True)

    # User-declared CLI agents (codex / opencode / aider / ...) from config.toml.
    try:
        if config is None:
            from vocalis.config import VocalisConfig

            config = VocalisConfig.load()
        for entry in getattr(config, "cli_agents", []) or []:
            from vocalis.agents.cli_agent import GenericCLIAgent

            registry.register(
                GenericCLIAgent(
                    name=entry.name,
                    command=list(entry.command),
                    event_bus=event_bus,
                    timeout_s=entry.timeout_s,
                )
            )
            logger.info("registered CLI agent %r -> %s", entry.name, entry.command)
    except Exception:
        logger.warning("failed to load cli_agents from config", exc_info=True)
    return registry
