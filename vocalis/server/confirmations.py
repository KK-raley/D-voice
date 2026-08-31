"""High-risk action confirmation (P0-2).

Voice can *request* destructive actions ("删除…", "发布…", "付款…") but must
never independently approve them. A risky dispatch is suspended into a
pending confirmation: the HUD shows a card, the user approves/denies with a
click (or keyboard shortcut), and only an explicit approval executes the
plan. The agent's native approval chain stays untouched - this layer only
gates what D-VOICE itself would dispatch on the user's behalf.

Design notes:

* Detection is intentionally keyword-based (fast, deterministic, local).
  False positives only cost one extra click; false negatives are mitigated
  because the MCP server still has NO authorization tools (agents approve
  in their own UI) and CLI agents keep their own guards.
* Pending confirmations expire (default 120 s) - a stale "delete this" card
  must never execute hours later.
* Nothing here is spoken aloud by default; the card is the approval surface.
"""

from __future__ import annotations

import re
import time
import uuid
from typing import Any

from vocalis.server.events import EventBus, EventType, bus

# Actions that must never run without an explicit user confirmation.
RISK_PATTERN = re.compile(
    r"删除|删掉|清空|格式化|卸载|注销|覆盖|"
    r"付款|支付|转账|下单|购买|扣款|"
    r"发布|上线|推送|push\b|--force|reset\s+--hard|rm\s+-rf|git\s+push|"
    r"外发|发送邮件|发邮件|群发|转发给|"
    r"关闭服务|停机|shutdown|drop\s+table|deploy|"
    r"撤回|取消订单|退款",
    re.IGNORECASE,
)


def is_risky(instruction: str) -> bool:
    """True if the instruction looks like a high-risk (destructive/costly) action."""
    return bool(RISK_PATTERN.search(instruction or ""))


class ConfirmationService:
    """Pending risky-action store wired to the event bus (HUD cards)."""

    def __init__(self, event_bus: EventBus | None = None, ttl_s: float = 120.0) -> None:
        self.bus = event_bus or bus
        self.ttl_s = ttl_s
        self._pending: dict[str, dict[str, Any]] = {}

    # -- create --------------------------------------------------------
    async def create(self, plan: dict[str, Any], source: str = "command") -> dict[str, Any]:
        """Suspend a risky plan into a pending confirmation + HUD event."""
        cid = uuid.uuid4().hex[:10]
        now = time.time()
        assignments = plan.get("assignments") or []
        actions = []
        for item in assignments:
            if isinstance(item, dict):
                actions.append(
                    {"agent": item.get("agent"), "instruction": item.get("instruction")}
                )
            else:
                agent, instruction = item
                actions.append({"agent": agent, "instruction": instruction})
        record = {
            "id": cid,
            "source": source,
            "transcript": plan.get("raw", ""),
            "actions": actions,
            "created_at": now,
            "expires_at": now + self.ttl_s,
        }
        self._prune()
        self._pending[cid] = {**record, "plan": plan}
        await self.bus.publish(EventType.CONFIRM_REQUESTED, **record)
        return record

    # -- resolve -------------------------------------------------------
    async def resolve(self, cid: str, approved: bool) -> dict[str, Any] | None:
        """Resolve a pending confirmation; returns the stored plan if approved."""
        self._prune()
        entry = self._pending.pop(cid, None)
        if entry is None:
            return None
        await self.bus.publish(
            EventType.CONFIRM_RESOLVED, id=cid, approved=approved
        )
        return dict(entry["plan"]) if approved else None

    def pending(self) -> list[dict[str, Any]]:
        """JSON-friendly snapshot of unresolved (non-expired) confirmations."""
        self._prune()
        return [
            {k: v for k, v in entry.items() if k != "plan"}
            for entry in self._pending.values()
        ]

    def get(self, cid: str) -> dict[str, Any] | None:
        self._prune()
        entry = self._pending.get(cid)
        if entry is None:
            return None
        return {k: v for k, v in entry.items() if k != "plan"}

    # -- internals -----------------------------------------------------
    def _prune(self) -> None:
        now = time.time()
        for cid in [c for c, e in self._pending.items() if now >= e["expires_at"]]:
            self._pending.pop(cid, None)
