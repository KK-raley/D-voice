"""DVoiceBrain: a local small language model that acts as the always-on
operations narrator and conversational layer.

Two backend flavors are supported:

* ``ollama`` - native Ollama API (qwen2.5, gemma3, llama3.2, ... pulled via
  ``ollama pull``; CPU-only laptops are fine with 0.5b-1.5b q4 models).
* ``openai-compatible`` - any server speaking the OpenAI chat-completions
  protocol: llama.cpp-server, LM Studio, vLLM, Ollama's /v1 endpoint, or a
  remote API. This lets D-VOICE ride any small model you can serve.

Responsibilities:
  * answer the user's questions in real time (fully local, private)
  * translate raw agent progress events into natural spoken narration
  * summarize system status on demand ("What's going on right now?")

If the model server is unreachable the brain degrades gracefully to
deterministic rule-based templates, so the ecosystem never goes mute.
Degradation is always logged and surfaced as a ``monitor.alert`` event.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from vocalis.agents.registry import AgentRegistry
from vocalis.config import BrainConfig, VocalisConfig
from vocalis.server.events import Event, EventBus, EventType, bus

logger = logging.getLogger("vocalis.dvoice")

SYSTEM_PROMPT = """You are D-VOICE, the user's voice operations assistant.
You run locally and narrate the status of AI agents working for the user.
Personality: concise, calm, slightly witty, like a trusted flight engineer.
Rules:
- Keep spoken replies under 3 sentences (they are read aloud).
- When reporting task status, mention agent name, state, and ETA if clear.
- Never invent task details not present in the context.
- Reply in the user's language (Chinese input -> Chinese reply, etc.).
"""


def _first(content: Any) -> str:
    """Extract text from either a pydantic model or a dict (ollama versions differ)."""
    if isinstance(content, dict):
        return str(content.get("message", {}).get("content", ""))
    message = getattr(content, "message", None)
    if message is not None:
        return str(getattr(message, "content", "") or "")
    return ""


class DVoiceBrain:
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
        if self.brain_cfg.backend != "ollama":
            return None
        if not self.brain_cfg.enabled:
            return None
        if self._ollama is not None:
            return self._ollama
        if self._checked:  # previously failed -> stay on rules unless reset
            return None
        self._checked = True
        try:
            import ollama

            self._ollama = ollama.AsyncClient(host=self.brain_cfg.host)
            return self._ollama
        except Exception as e:
            logger.warning("Ollama client unavailable (%s); using fallback replies", e)
            return None

    def _openai_base(self) -> str:
        base = self.brain_cfg.base_url
        if not base:
            raise ValueError(
                "brain.backend='openai-compatible' needs brain.base_url "
                "(e.g. http://localhost:8080/v1 for llama.cpp-server, "
                "http://localhost:1234/v1 for LM Studio)"
            )
        return base.rstrip("/")

    def _openai_headers(self) -> dict[str, str]:
        key = os.environ.get(self.brain_cfg.api_key_env, "not-needed")
        return {"Authorization": f"Bearer {key}"}

    async def _chat_openai(self, messages: list[dict[str, str]]) -> str:
        payload = {
            "model": self.brain_cfg.model,
            "messages": messages,
            "temperature": self.brain_cfg.temperature,
            "max_tokens": self.brain_cfg.max_tokens,
        }
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                f"{self._openai_base()}/chat/completions",
                json=payload,
                headers=self._openai_headers(),
            )
            resp.raise_for_status()
            data = resp.json()
        content = data["choices"][0]["message"]["content"]
        return str(content or "").strip()

    async def available(self) -> bool:
        if not self.brain_cfg.enabled:
            return False
        if self.brain_cfg.backend == "openai-compatible":
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    resp = await client.get(
                        f"{self._openai_base()}/models",
                        headers=self._openai_headers(),
                    )
                    return resp.status_code < 500
            except Exception:
                return False
        client = self._get_ollama()
        if client is None:
            return False
        try:
            await client.list()
            return True
        except Exception:
            return False

    async def _degrade(self, reason: str) -> None:
        """Make degradation visible instead of silently swallowing it."""
        logger.warning("D-VOICE brain degrading to rules: %s", reason)
        await self.bus.publish(
            EventType.MONITOR_ALERT, message=f"D-VOICE local model unavailable: {reason}"
        )

    # ------------------------------------------------------------------
    # Dialogue
    # ------------------------------------------------------------------
    async def chat(self, user_text: str, context: dict[str, Any] | None = None) -> str:
        """Real-time conversational reply (question answering)."""
        context = context or {}
        messages: list[dict[str, str]] = [{"role": "system", "content": SYSTEM_PROMPT}]
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

        if self.brain_cfg.backend == "openai-compatible":
            try:
                reply = await self._chat_openai(messages)
                if not reply:
                    raise ValueError("empty reply from local model")
                self._remember(user_text, reply)
                return reply
            except Exception as e:
                await self._degrade(str(e))
        else:
            client = self._get_ollama()
            if client is not None:
                try:
                    resp = await client.chat(
                        model=self.brain_cfg.model,
                        messages=messages,
                        options={
                            "temperature": self.brain_cfg.temperature,
                            "num_predict": self.brain_cfg.max_tokens,
                        },
                    )
                    reply = _first(resp).strip()
                    if not reply:
                        raise ValueError("empty reply from local model")
                    self._remember(user_text, reply)
                    return reply
                except Exception as e:
                    await self._degrade(str(e))
        if self.brain_cfg.fallback_to_rules:
            reply = await self._rule_reply(user_text, context)
            self._remember(user_text, reply)
            return reply
        return "(d-voice offline)"

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
        }.get(etype)  # type: ignore[arg-type]
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

    async def _rule_reply(self, user_text: str, context: dict[str, Any]) -> str:
        """Deterministic fallback dialogue (no local model available)."""
        text = user_text.lower()
        if any(k in text for k in ("status", "状态", "进展", "怎么样", "进度")):
            return await self.status_report()
        if any(k in text for k in ("who are you", "你是谁")):
            return "I'm D-VOICE, your local operations narrator. I watch your agents and answer questions."
        if any(k in text for k in ("hello", "hi", "你好", "在吗")):
            return "At your service."
        if text.endswith("?") or text.endswith("？"):
            return (
                "My local reasoning core is offline, but I can report task status, "
                "dispatch agents, and adjust my voice. Start Ollama for full dialogue."
            )
        return "Understood. Say 'status' for a report, or give me a task to dispatch."
