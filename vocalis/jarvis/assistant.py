"""JarvisBrain: a local small language model (via Ollama) that acts as the
always-on operations narrator and conversational layer.

Responsibilities:
  * answer the user's questions in real time (fully local, private)
  * translate raw agent progress events into natural spoken narration
  * summarize system status on demand ("What's going on right now?")

If Ollama is unreachable the brain degrades gracefully to deterministic
rule-based templates, so the ecosystem never goes mute.
"""

from __future__ import annotations

from typing import Any

from vocalis.agents.registry import AgentRegistry
from vocalis.config import BrainConfig, VocalisConfig
from vocalis.server.events import Event, EventBus, EventType, bus

SYSTEM_PROMPT = """You are JARVIS, the user's voice operations assistant.
You run locally and narrate the status of AI agents working for the user.
Personality: concise, calm, slightly witty, like Iron Man's JARVIS.
Rules:
- Keep spoken replies under 3 sentences (they are read aloud).
- When reporting task status, mention agent name, state, and ETA if clear.
- Never invent task details not present in the context.
- Reply in the user's language (Chinese input -> Chinese reply, etc.).
"""


class JarvisBrain:
    def __init__(
        self,
        config: VocalisConfig | None = None,
        registry: AgentRegistry | None = None,
        event_bus: EventBus | None = None,
    ) -> None:
        self.config = config or VocalisConfig.load()
        self.brain_cfg: BrainConfig = self.config.brain
        self.registry = registry
        self.bus = event_bus or bus
        self.history: list[dict[str, str]] = []
        self._ollama = None
        self._checked = False

    # ------------------------------------------------------------------
    # Backend probing
    # ------------------------------------------------------------------
    def _get_ollama(self):
        if not self.brain_cfg.enabled:
            return None
        if self._ollama is not None:
            return self._ollama
        if self._checked:  # previously failed -> stay on rules unless reset
            return None
        self._checked = True
        try:
            import ollama

            client = ollama.AsyncClient(host=self.brain_cfg.host)
            self._ollama = client
            return client
        except Exception:
            return None

    async def available(self) -> bool:
        client = self._get_ollama()
        if client is None:
            return False
        try:
            await client.list()
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Dialogue
    # ------------------------------------------------------------------
    async def chat(self, user_text: str, context: dict[str, Any] | None = None) -> str:
        """Real-time conversational reply (question answering)."""
        context = context or {}
        client = self._get_ollama()
        if client is not None:
            try:
                messages = [{"role": "system", "content": SYSTEM_PROMPT}]
                if context:
                    messages.append(
                        {
                            "role": "system",
                            "content": "Live system context:\n"
                            + "\n".join(f"- {k}: {v}" for k, v in context.items()),
                        }
                    )
                messages.extend(self.history[-8:])
                messages.append({"role": "user", "content": user_text})
                resp = await client.chat(
                    model=self.brain_cfg.model,
                    messages=messages,
                    options={
                        "temperature": self.brain_cfg.temperature,
                        "num_predict": self.brain_cfg.max_tokens,
                    },
                )
                reply = resp["message"]["content"].strip()
                self._remember(user_text, reply)
                return reply
            except Exception:
                pass  # fall through to rules
        if self.brain_cfg.fallback_to_rules:
            reply = self._rule_reply(user_text, context)
            self._remember(user_text, reply)
            return reply
        return "(jarvis offline)"

    # ------------------------------------------------------------------
    # Narration of agent events
    # ------------------------------------------------------------------
    async def narrate_event(self, event: Event | None = None, **kw: Any) -> str:
        """Turn a raw bus event into a short spoken sentence."""
        if event is not None:
            etype: str = event.type.value
            data = dict(event.data)
        else:
            etype = str(kw.pop("type", ""))
            data = dict(kw)
        template = {
            EventType.TASK_STARTED: "Starting {agent}: {instruction}.",
            EventType.TASK_PROGRESS: "{agent} is at {progress_percent:.0f} percent - {current_step}.",
            EventType.TASK_COMPLETED: "Task complete. {agent} finished: {instruction}.",
            EventType.TASK_FAILED: "Heads up - {agent} failed: {error}.",
            EventType.MONITOR_ALERT: "Warning: {message}.",
        }.get(etype)
        if template is None:
            return ""
        try:
            data = dict(data)
            data.setdefault("progress_percent", float(data.get("progress", 0)) * 100)
            return template.format(**{k: v for k, v in data.items() if k in template})
        except Exception:
            return ""

    # ------------------------------------------------------------------
    # Status summary ("What's happening?")
    # ------------------------------------------------------------------
    async def status_report(self) -> str:
        snap = self.registry.snapshot() if self.registry else {"agents": [], "active_tasks": []}
        active = snap["active_tasks"]
        agents = snap["agents"]
        if not active:
            names = ", ".join(a["name"] for a in agents) or "no agents"
            return f"All systems idle. Connected agents: {names}. Awaiting your command."
        lines = [f"{len(active)} task(s) in flight."]
        for t in active:
            lines.append(
                f"{t['agent']} is {t['current_step'] or t['status']}, {t['progress']*100:.0f}% done."
            )
        return " ".join(lines)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _remember(self, user_text: str, reply: str) -> None:
        self.history.append({"role": "user", "content": user_text})
        self.history.append({"role": "assistant", "content": reply})
        if len(self.history) > 40:
            self.history = self.history[-40:]

    def _rule_reply(self, user_text: str, context: dict[str, Any]) -> str:
        """Deterministic fallback dialogue (no local model available)."""
        text = user_text.lower()
        if any(k in text for k in ("status", "状态", "进展", "怎么样", "进度")):
            import asyncio

            try:
                return asyncio.get_event_loop().run_until_complete(self.status_report())
            except Exception:
                return "All monitored tasks are proceeding."
        if any(k in text for k in ("who are you", "你是谁")):
            return "I'm JARVIS, your local operations narrator. I watch your agents and answer questions."
        if any(k in text for k in ("hello", "hi", "你好", "在吗")):
            return "At your service."
        if text.endswith("?") or text.endswith("？"):
            return (
                "My local reasoning core is offline, but I can report task status, "
                "dispatch agents, and adjust my voice. Start Ollama for full dialogue."
            )
        return "Understood. Say 'status' for a report, or give me a task to dispatch."
