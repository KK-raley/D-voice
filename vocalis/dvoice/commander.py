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
from vocalis.server.confirmations import is_risky
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
# Kept for reference: strict start-of-sentence question hints. Superseded
# in plan() by "non-imperative -> brain" routing, because ASR output has no
# punctuation and question words usually appear mid-sentence in Chinese.
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
        confirmations: Any | None = None,
    ) -> None:
        self.registry = registry
        self.brain = brain or DVoiceBrain(registry=registry)
        self.bus = event_bus or bus
        # Optional ConfirmationService (P0-2): risky dispatches are suspended
        # into a HUD confirmation card instead of executing immediately.
        self.confirmations = confirmations

    # ------------------------------------------------------------------
    # Planning
    # ------------------------------------------------------------------
    def plan(self, utterance: str) -> CommandPlan:
        text = utterance.strip()
        if not text:
            return CommandPlan(raw=text)

        # 祈使句优先于状态查询：任务指令里常含"进度/状态"字样
        # （"让 echo 汇报进度"），不能被 STATUS_PATTERNS 截胡。
        imperative = text.lower().startswith(("run ", "execute ", "帮我", "让", "@"))
        if imperative:
            # Direct fan-out syntax: "@agent1 do X; @agent2 do Y" or
            # "让agent1做X，让agent2做Y"
            assignments = self._parse_fanout(text)
            if assignments:
                return CommandPlan(raw=text, assignments=assignments)
            return CommandPlan(
                raw=text, assignments=[(self.registry.default_agent(), text)]
            )

        if STATUS_PATTERNS.search(text) and len(text) < 60:
            return CommandPlan(raw=text, is_status_query=True)

        # 其余（疑问句、聊天、中性语句）一律交给大脑对话：
        # 语音转写没有标点且问话词常在句中（"你的名字叫什么…"），按疑问词
        # 猜意图会漏判，曾把用户的提问当成任务派给演示 agent，导致播报里
        # 全是问题原文。对语音管家而言，默认"回应"比默认"派发"更符合直觉
        # ——需要派发时用 "@echo ..." / "让 echo ..." / "帮我 ..." 表达。
        return CommandPlan(raw=text, question=text)

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
    async def execute(
        self,
        utterance: str,
        user: str | None = None,
        voiceprint: str | None = None,
    ) -> dict[str, Any]:
        """Plan + run one utterance; ``user``/``voiceprint`` feed the receipt.

        Risky assignments (P0-2) are suspended: when a ConfirmationService is
        wired and any assignment matches :data:`RISK_PATTERN`, the plan is
        parked and a ``confirm.requested`` event tells the HUD to show a card.
        Only ``execute_plan`` after an explicit approval performs the dispatch.
        """
        plan = self.plan(utterance)
        await self.bus.publish(EventType.DVOICE_COMMAND, **plan.to_dict())

        if plan.is_status_query:
            report = await self.brain.status_report()
            return await self._receipt(
                plan, "status", reply=report, user=user, voiceprint=voiceprint
            )

        if plan.question:
            reply = await self.brain.chat(plan.question, user=user)
            return await self._receipt(
                plan, "answer", reply=reply, user=user, voiceprint=voiceprint
            )

        if not plan.assignments:
            reply = await self.brain.chat(utterance, user=user)
            return await self._receipt(
                plan, "answer", reply=reply, user=user, voiceprint=voiceprint
            )

        if self.confirmations is not None and any(
            is_risky(instruction) for _, instruction in plan.assignments
        ):
            # Identity rides along in the parked plan so the post-approval
            # execution emits a receipt with the verified user (audit chain).
            confirmation = await self.confirmations.create(
                {**plan.to_dict(), "user": user, "voiceprint": voiceprint}
            )
            return await self._receipt(
                plan,
                "confirmation",
                user=user,
                voiceprint=voiceprint,
                confirmation=confirmation,
            )

        return await self.execute_plan(plan, user=user, voiceprint=voiceprint)

    async def execute_plan(
        self,
        plan: CommandPlan | dict[str, Any],
        user: str | None = None,
        voiceprint: str | None = None,
    ) -> dict[str, Any]:
        """Dispatch a (possibly approval-released) plan and emit its receipt."""
        if isinstance(plan, dict):
            plan = CommandPlan(
                assignments=[
                    (a["agent"], a["instruction"])
                    for a in plan.get("assignments", [])
                ],
                question=plan.get("question"),
                raw=plan.get("raw", ""),
                is_status_query=bool(plan.get("statusQuery")),
            )

        if plan.is_status_query:
            report = await self.brain.status_report()
            return await self._receipt(
                plan, "status", reply=report, user=user, voiceprint=voiceprint
            )

        if len(plan.assignments) == 1:
            agent, instruction = plan.assignments[0]
            record = await self.registry.dispatch(agent, instruction)
            return await self._receipt(
                plan,
                "task",
                task=record.to_dict(),
                user=user,
                voiceprint=voiceprint,
            )

        records = await self.registry.dispatch_many(plan.assignments)
        return await self._receipt(
            plan,
            "tasks",
            tasks=[r.to_dict() for r in records],
            user=user,
            voiceprint=voiceprint,
        )

    async def _receipt(
        self,
        plan: CommandPlan,
        kind: str,
        *,
        user: str | None = None,
        voiceprint: str | None = None,
        reply: str = "",
        confirmation: dict[str, Any] | None = None,
        task: dict[str, Any] | None = None,
        tasks: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Build the result dict and publish the P0-6 command.receipt event."""
        local_llm = bool(
            getattr(self.brain, "brain_cfg", None) is not None
            and self.brain.brain_cfg.enabled
            and self.brain.brain_cfg.local_only
        )
        task_ids = [t["id"] for t in (tasks or [])]
        if task is not None:
            task_ids = [task["id"]]
        receipt = {
            "transcript": plan.raw,
            "kind": kind,
            "user": user,
            "voiceprint": voiceprint,
            "local_llm": local_llm,
            "agents": [a for a, _ in plan.assignments],
            "task_ids": task_ids,
            "reply_preview": (reply or "")[:160],
            "confirmation_id": (confirmation or {}).get("id"),
            "cancellable": bool(task_ids),
        }
        await self.bus.publish(EventType.COMMAND_RECEIPT, **receipt)
        result: dict[str, Any] = {"kind": kind, "plan": plan.to_dict()}
        if reply:
            result["reply"] = reply
        if confirmation is not None:
            result["confirmation"] = confirmation
        if task is not None:
            result["task"] = task
        if tasks is not None:
            result["tasks"] = tasks
        return result
