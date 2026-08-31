"""ScreenWatcher: 周期性屏幕观测——独立于 agent 上报的监管通道。

传统监管完全依赖 agent 通过事件总线上报进度；本监视器让 D-VOICE
自己"看"屏幕（截屏 + 本地 OCR），把观测结果发布到事件总线上供
HUD 展示与事后问答，从而形成不依赖 agent 连接的第二监管来源。

隐私：观测只在本地完成（OCR 不出网）；默认关闭，由 /api/vision/watch
或配置显式开启。观测文本只进事件总线的本地历史，不会自动上传云端。
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Any

from vocalis.config import VocalisConfig
from vocalis.server.events import Event, EventBus, EventType

logger = logging.getLogger("vocalis.vision")


class ScreenWatcher:
    """周期截屏观测循环（默认关闭；start/stop 可反复切换）。"""

    def __init__(
        self,
        config: VocalisConfig | None = None,
        event_bus: EventBus | None = None,
        interval_s: float = 30.0,
        history_size: int = 20,
    ) -> None:
        self.config = config or VocalisConfig.load()
        self.bus = event_bus
        self.interval_s = interval_s
        self.history: deque[dict[str, Any]] = deque(maxlen=history_size)
        self._task: asyncio.Task | None = None
        self._wake: asyncio.Event | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def snapshot(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "interval_s": self.interval_s,
            "observations": list(self.history),
        }

    async def start(self, interval_s: float | None = None) -> None:
        if self.running:
            return
        if interval_s:
            self.interval_s = interval_s
        self._wake = asyncio.Event()
        self._task = asyncio.create_task(self._loop(), name="screen-watcher")
        logger.info("ScreenWatcher started (interval=%.1fs)", self.interval_s)

    async def stop(self) -> None:
        if self._task is None:
            return
        self._wake.set()
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None
        self._wake = None
        logger.info("ScreenWatcher stopped")

    async def _loop(self) -> None:
        from vocalis.vision.screen import observe_screen

        assert self._wake is not None
        while True:
            try:
                obs = await observe_screen()
                record = {
                    "title": obs.title,
                    "engine": obs.engine,
                    "lines": len(obs.lines),
                    "text": obs.text[:600],
                    "ts": asyncio.get_event_loop().time(),
                }
                self.history.append(record)
                if self.bus is not None:
                    await self.bus.publish(
                        Event(
                            type=EventType.VISION_SCREEN,
                            data={"title": obs.title, "lines": len(obs.lines),
                                  "text": obs.text[:300]},
                        )
                    )
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - 观测失败不终止监管
                logger.warning("screen observation failed: %s", e)
            try:
                await asyncio.wait_for(
                    self._wake.wait(), timeout=self.interval_s
                )
                self._wake.clear()  # stop 信号
            except asyncio.TimeoutError:
                pass
