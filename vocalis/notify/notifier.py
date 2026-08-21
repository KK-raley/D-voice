"""Task-completion notifications: voice chime + spoken summary + system toast."""

from __future__ import annotations

from typing import Any

from vocalis.server.events import EventBus, EventType, bus
from vocalis.voice.tts import TTSService

CHIME_TEXT = {
    "completed": "任务完成。Task complete.",
    "failed": "注意，任务失败了。Attention: a task has failed.",
    "stalled": "警告：任务疑似卡住。Warning: a task appears stalled.",
}


class Notifier:
    def __init__(
        self,
        tts: TTSService | None = None,
        event_bus: EventBus | None = None,
        profile: str | None = None,
        enable_toast: bool = True,
    ) -> None:
        self.tts = tts
        self.bus = event_bus or bus
        self.profile = profile
        self.enable_toast = enable_toast

    # ------------------------------------------------------------------
    async def notify_task(self, task: Any) -> None:
        """Called by the monitor when a tracked task reaches a terminal state."""
        status = getattr(task, "status", "completed")
        agent = getattr(task, "agent", "agent")
        instruction = getattr(task, "instruction", "")
        summary = {
            "completed": f"Done. {agent} finished: {instruction}.",
            "failed": f"{agent} failed: {getattr(task, 'error', 'unknown error')}",
        }.get(status, f"{agent}: {status}")

        await self.bus.publish(EventType.SYSTEM, level="notify", message=summary)
        if self.tts is not None:
            try:
                await self.tts.speak(
                    f"{CHIME_TEXT.get(status, '')} {summary}",
                    profile_name=self.profile,
                    play=True,
                )
            except Exception:
                pass
        if self.enable_toast:
            self._toast(f"Vocalis - {agent}", summary)

    # ------------------------------------------------------------------
    @staticmethod
    def _toast(title: str, message: str) -> None:
        """Best-effort desktop toast on Windows / macOS / Linux."""
        import sys

        try:  # Windows attention chime
            from winsound import MessageBeep

            MessageBeep()
        except Exception:
            pass
        try:  # macOS
            if sys.platform == "darwin":
                import subprocess

                subprocess.run(
                    ["osascript", "-e", f'display notification "{message}" with title "{title}"'],
                    check=False,
                )
                return
        except Exception:
            pass
        try:  # Linux
            import shutil
            import subprocess

            if shutil.which("notify-send"):
                subprocess.run(["notify-send", title, message], check=False)
        except Exception:
            pass
