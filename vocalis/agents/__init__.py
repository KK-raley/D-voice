"""Pluggable agent connectors.

Any coding assistant, LLM API, or automation agent can be attached to
Vocalis by implementing :class:`~vocalis.agents.base.AgentConnector`. The
connector reports granular progress events so the monitor (and D-VOICE)
can narrate what is happening in real time.
"""

from vocalis.agents.base import AgentConnector, AgentStatus, TaskRecord, TaskStatus
from vocalis.agents.registry import AgentRegistry, build_default_registry

__all__ = [
    "AgentConnector",
    "AgentStatus",
    "TaskRecord",
    "TaskStatus",
    "AgentRegistry",
    "build_default_registry",
]
