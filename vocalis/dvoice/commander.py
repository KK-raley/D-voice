"""Commander: routes natural-language orders to the right agent.

Intent detection runs on the DVoiceBrain (local LLM when available,
rule-based fallback otherwise) and supports both single dispatch and
parallel fan-out ("ask X to do A, and Y to do B").
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from vocalis.agents.registry import AgentRegistry
from vocalis.dvoice.assistant import DVoiceBrain
from vocalis.server.events import EventBus, EventType, bus


@dataclass
class CommandPlan:
    """Parsed execution plan derived from a user utterance."""

    assignments: list[tuple[str, str]] = field(default_factory=list)
    question: str | None = None
    raw: str = ""
    is_status_query: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "assignments": [{"agent": a, "instruction": i} for a, i in self.assignments],
            "question": self.question,
            "statusQuery": self.is_status_query,
            "raw": self.raw,
        }


STATUS_PATTERNS = re.compile(
    r"(现在|当前|目前)?(的)?(状态|进展|进度|情况)|\bstatus\b|\bprogress\b|what.s (going on|happening)",
    re.IGNORECASE,
)
# Word-boundary anchored question hints (avoids matching "whatever"/"几百").
QUESTION_HINTS_RE = re.compile(
    r"\b(what|why|how|who|when|where|is|are|can|does)\b.*\?|"
    r"^\s*(什么|为什么|怎么|怎样|如何|谁|几|哪|是否|能不能)\b|"
    r"[?？]\s*$",
    re.IGNORECASE,
)


class Commander:
    def __init__(
        self,
        registry: AgentRegistry,
        brain: DVoiceBrain | None = None,
        event_bus: EventBus | None = None,
    ) -> None:
        self.registry = registry
        self.brain = brain or DVoiceBrain(registry=registry)
        self.bus = event_bus or bus

    # ------------------------------------------------------------------
    # Planning
    # ------------------------------------------------------------------
    def plan(self, utterance: str) -> CommandPlan:
        text = utterance.strip()
        if not text:
            return CommandPlan(raw=text)

        if STATUS_PATTERNS.search(text) and len(text) < 60:
            return CommandPlan(raw=text, is_status_query=True)

        # Direct fan-out syntax: "@agent1 do X; @agent2 do Y" or
        # "让agent1做X，让agent2做Y"
        assignments = self._parse_fanout(text)
        if assignments:
            return CommandPlan(raw=text, assignments=assignments)

        # Is it a question (-> brain) or an order (-> default agent)?
        imperative = text.lower().startswith(("run ", "execute ", "帮我", "让", "@"))
        if QUESTION_HINTS_RE.search(text) and not imperative:
            return CommandPlan(raw=text, question=text)

        return CommandPlan(raw=text, assignments=[(self.registry.default_agent(), text)])

    def _parse_fanout(self, text: str) -> list[tuple[str, str]]:
        known = {name: name for name in self.registry.connectors}
        # alias map for natural speech (codex/opencode resolve if configured
        # as cli_agents in config.toml)
        aliases = {
            "claude": "claude-code",
            "code": "claude-code",
            "gpt": "openai",
            "codex": "codex",
            "opencode": "opencode",
        }
        segments: list[str] = re.split(r"[;；]|然后|以及|并且|, and ", text)
        assignments: list[tuple[str, str]] = []
        for seg in segments:
            seg = seg.strip()
            if not seg:
                continue
            m = re.match(r"[@让用]?\s*(\w+)[，,\s]*(?:去做|执行|帮我|去)?\s*(.+)", seg)
            target = None
            instruction = seg
            if m:
                cand = m.group(1).lower()
                target = known.get(cand) or known.get(aliases.get(cand, ""))
                if target:
                    instruction = m.group(2).strip()
            if target:
                assignments.append((target, instruction))
            elif assignments:
                # continuation of previous instruction
                prev_agent, prev_instr = assignments[-1]
                assignments[-1] = (prev_agent, f"{prev_instr} {seg}")
        return assignments if assignments else []

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------
    async def execute(self, utterance: str) -> dict[str, Any]:
        plan = self.plan(utterance)
        await self.bus.publish(EventType.DVOICE_COMMAND, **plan.to_dict())

        if plan.is_status_query:
            report = await self.brain.status_report()
            return {"kind": "status", "reply": report, "plan": plan.to_dict()}

        if plan.question:
            reply = await self.brain.chat(plan.question)
            return {"kind": "answer", "reply": reply, "plan": plan.to_dict()}

        if not plan.assignments:
            reply = await self.brain.chat(utterance)
            return {"kind": "answer", "reply": reply, "plan": plan.to_dict()}

        if len(plan.assignments) == 1:
            agent, instruction = plan.assignments[0]
            record = await self.registry.dispatch(agent, instruction)
            return {"kind": "task", "task": record.to_dict(), "plan": plan.to_dict()}

        records = await self.registry.dispatch_many(plan.assignments)
        return {
            "kind": "tasks",
            "tasks": [r.to_dict() for r in records],
            "plan": plan.to_dict(),
        }
