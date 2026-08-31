"""Local Qwen conversation and deterministic operations narration.

Qwen runs in llama.cpp on loopback; no cloud account or key is required.
Legacy local Ollama/compatible servers remain supported. Model failures are
explicitly reported and rule replies are never represented as model inference.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from vocalis.agents.registry import AgentRegistry
from vocalis.config import BrainConfig, VocalisConfig
from vocalis.dvoice.local_qwen import ensure_local_qwen, probe_local_qwen, validate_local_url
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
        # P0-7 multi-user isolation: per-speaker dialogue histories + short
        # local summaries. User A's context is never shown to user B; on
        # sleep the speaker's history is cleared and replaced by a compact
        # summary (rule-based - no LLM call) for that same user's next wake.
        self.histories: dict[str, list[dict[str, str]]] = {}
        self.summaries: dict[str, str] = {}
        self._ollama = None
        self._checked = False
        self.last_reply_source: str | None = None
        self.last_error: str | None = None

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

            host = self.brain_cfg.host
            if self.brain_cfg.local_only:
                host = validate_local_url(host)
            self._ollama = ollama.AsyncClient(
                host=host, timeout=self.brain_cfg.timeout_s, trust_env=False,
                follow_redirects=False,
            )
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
        if self.brain_cfg.local_only or self.brain_cfg.backend == "local-qwen":
            return validate_local_url(base)
        return base.rstrip("/")

    def _openai_headers(self) -> dict[str, str]:
        if self.brain_cfg.local_only or self.brain_cfg.backend == "local-qwen":
            return {}  # Never forward a saved cloud secret to the local runtime.
        key = os.environ.get(self.brain_cfg.api_key_env, "not-needed")
        return {"Authorization": f"Bearer {key}"}

    async def _chat_openai(self, messages: list[dict[str, str]]) -> str:
        payload = {
            "model": self.brain_cfg.model,
            "messages": messages,
            "temperature": self.brain_cfg.temperature,
            "max_tokens": self.brain_cfg.max_tokens,
        }
        if self.brain_cfg.backend == "local-qwen":
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        base = self._openai_base()  # validate before constructing a network client
        timeout = self.brain_cfg.timeout_s
        if not 0 < timeout <= 600:
            raise ValueError("brain.timeout_s must be between 0 and 600")
        async with httpx.AsyncClient(
            timeout=timeout, trust_env=False, follow_redirects=False
        ) as client:
            resp = await client.post(
                f"{base}/chat/completions",
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
        if self.brain_cfg.backend == "local-qwen":
            try:
                status = await probe_local_qwen(self.brain_cfg)
                return bool(status["available"])
            except Exception:
                return False
        if self.brain_cfg.backend == "openai-compatible":
            try:
                base = self._openai_base()
                async with httpx.AsyncClient(
                    timeout=5.0, trust_env=False, follow_redirects=False
                ) as client:
                    resp = await client.get(
                        f"{base}/models",
                        headers=self._openai_headers(),
                    )
                    return 200 <= resp.status_code < 300
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
        self.last_error = reason
        await self.bus.publish(
            EventType.MONITOR_ALERT, message=f"D-VOICE local model unavailable: {reason}"
        )

    # ------------------------------------------------------------------
    # Dialogue
    # ------------------------------------------------------------------
    def _history_for(self, user: str | None) -> list[dict[str, str]]:
        """Per-user history storage (P0-7); None = anonymous/global lane."""
        if user is None:
            return self.history
        return self.histories.setdefault(user, [])

    def end_session(self, user: str | None, reason: str = "sleep") -> str | None:
        """Clear one speaker's context on sleep; keep a short local summary.

        Rule-based (no LLM call, safe inside sync sleep paths). The summary
        is reintroduced only to the *same* user on their next wake, so a
        later speaker can never touch the previous speaker's context.
        """
        if user is None:
            self.history = []
            return None
        history = self.histories.get(user) or []
        turns = len(history) // 2
        last_user = next(
            (m["content"] for m in reversed(history) if m["role"] == "user"), ""
        )
        summary = (
            f"（上次会话摘要 · {turns} 轮 · 因{reason}结束）"
            f"最后的请求：{last_user[:80]}" if turns else ""
        )
        self.summaries[user] = summary
        self.histories[user] = []
        return summary or None

    async def chat(
        self, user_text: str, context: dict[str, Any] | None = None,
        user: str | None = None,
    ) -> str:
        """Real-time conversational reply (question answering).

        ``user`` pins the dialogue to one speaker's isolated history (P0-7).
        """
        context = dict(context or {})
        history = self._history_for(user)
        if user is not None and not history and self.summaries.get(user):
            # Same user returned: reintroduce only their own short summary.
            context.setdefault("previous_session", self.summaries[user])
        self.last_reply_source = None
        self.last_error = None
        messages: list[dict[str, str]] = [{"role": "system", "content": SYSTEM_PROMPT}]
        if context:
            messages.append(
                {
                    "role": "system",
                    "content": "Live system context:\n"
                    + "\n".join(f"- {k}: {v}" for k, v in context.items()),
                }
            )
        messages.extend(history[-8:])
        messages.append({"role": "user", "content": user_text})

        if not self.brain_cfg.enabled:
            self.last_error = "Local model is disabled"
        elif self.brain_cfg.backend in {"local-qwen", "openai-compatible"}:
            try:
                if self.brain_cfg.backend == "local-qwen" and self.brain_cfg.auto_start:
                    status = await ensure_local_qwen(self.brain_cfg)
                    if not status["available"]:
                        raise RuntimeError(status.get("error") or status["status"])
                reply = await self._chat_openai(messages)
                if not reply:
                    raise ValueError("empty reply from local model")
                self._remember(user_text, reply, user)
                self.last_reply_source = "local-model" if self.brain_cfg.local_only else "model"
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
                    self._remember(user_text, reply, user)
                    self.last_reply_source = "local-model" if self.brain_cfg.local_only else "model"
                    return reply
                except Exception as e:
                    await self._degrade(str(e))
            else:
                await self._degrade("Local backend is unavailable or unsupported")
        if self.brain_cfg.fallback_to_rules:
            reply = await self._rule_reply(user_text, context)
            self.last_reply_source = "rules"
            reply = "[规则回复：本地模型未连接] " + reply
            self._remember(user_text, reply, user)
            return reply
        self.last_reply_source = "offline"
        return "(d-voice offline: local model unavailable)"

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
    def _remember(self, user_text: str, reply: str, user: str | None = None) -> None:
        history = self._history_for(user)
        history.append({"role": "user", "content": user_text})
        history.append({"role": "assistant", "content": reply})
        if len(history) > 40:
            trimmed = history[-40:]
            if user is None:
                self.history = trimmed
            else:
                self.histories[user] = trimmed

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
                "dispatch agents, and adjust my voice. Start local Qwen for full dialogue."
            )
        return "Understood. Say 'status' for a report, or give me a task to dispatch."
