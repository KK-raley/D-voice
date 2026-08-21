"""Agent connector + registry integration tests (offline)."""

from __future__ import annotations

import asyncio

import pytest

from vocalis.agents.base import AgentStatus, TaskStatus
from vocalis.agents.echo import EchoAgent
from vocalis.agents.registry import AgentRegistry


def test_registry_lists_echo():
    registry = AgentRegistry()
    registry.register(EchoAgent())
    agents = registry.list()
    assert agents[0]["name"] == "echo"
    assert agents[0]["status"] == "idle"


def test_registry_unknown_agent_raises():
    registry = AgentRegistry()
    with pytest.raises(KeyError):
        registry.get("nope")


def test_echo_task_completes():
    registry = AgentRegistry()
    registry.register(EchoAgent())
    record = asyncio.run(registry.dispatch("echo", "demo instruction"))
    assert record.status is TaskStatus.COMPLETED
    assert record.progress == 1.0
    assert "demo instruction" in record.output
    assert registry.history


def test_agent_returns_to_idle():
    agent = EchoAgent()
    asyncio.run(agent.run("x"))
    assert agent.status is AgentStatus.IDLE
    assert not agent.active_tasks
