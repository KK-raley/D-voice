"""Long-run standby stress metrics (P0-3).

What decides the "always-on" experience is not one successful wake but
eight hours of it: memory growth, CPU drift, thread leaks, brain availability
and recovery after system sleep. :class:`StressRecorder` samples lightweight
process/system metrics on an interval and appends JSONL lines to
``VOCALIS_HOME/stress/metrics.jsonl``. No audio is ever recorded.

``psutil`` is optional; without it the recorder still tracks uptime and
thread count (stdlib) and marks process metrics as unavailable.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def stress_dir() -> Path:
    home = Path(os.environ.get("VOCALIS_HOME", Path.home() / ".vocalis"))
    return home / "stress"


def metrics_path() -> Path:
    return stress_dir() / "metrics.jsonl"


class StressRecorder:
    """Interval sampler -> JSONL; safe to run for hours in the background."""

    def __init__(
        self,
        interval_s: float = 60.0,
        brain_ok: Callable[[], Any] | None = None,
        audio_queue_depth: Callable[[], int] | None = None,
    ) -> None:
        self.interval_s = interval_s
        self._brain_ok = brain_ok  # may be async; probed with a timeout
        self._audio_queue_depth = audio_queue_depth
        self._task: asyncio.Task | None = None
        self._started_at: float | None = None
        self.last: dict[str, Any] | None = None
        self._stop = asyncio.Event()

    # -- lifecycle -----------------------------------------------------
    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._started_at = time.time()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def uptime_s(self) -> float:
        return 0.0 if self._started_at is None else time.time() - self._started_at

    # -- sampling ------------------------------------------------------
    async def sample(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "ts": time.time(),
            "uptime_s": round(self.uptime_s, 1),
            "threads": threading.active_count(),
        }
        try:
            import psutil

            proc = psutil.Process()
            with proc.oneshot():
                data["rss_mb"] = round(proc.memory_info().rss / (1024 * 1024), 1)
                data["cpu_percent"] = proc.cpu_percent(interval=None)
            data["sys_mem_percent"] = psutil.virtual_memory().percent
        except ImportError:
            data["psutil"] = False
        except Exception as e:  # never kill the loop over a metric
            data["metric_error"] = str(e)
        if self._audio_queue_depth is not None:
            try:
                data["audio_queue"] = int(self._audio_queue_depth())
            except Exception:
                pass
        if self._brain_ok is not None:
            try:
                ok = self._brain_ok()
                if asyncio.iscoroutine(ok):
                    ok = await asyncio.wait_for(ok, timeout=5.0)
                data["brain_ok"] = bool(ok)
            except Exception:
                data["brain_ok"] = False
        return data

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.last = await self.sample()
                self._append(self.last)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("stress sample failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_s)
            except asyncio.TimeoutError:
                continue

    @staticmethod
    def _append(sample: dict[str, Any]) -> None:
        path = metrics_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(sample, ensure_ascii=False) + "\n")


def summarize(path: Path | None = None) -> dict[str, Any]:
    """Aggregate a metrics JSONL file: memory/CPU/brain-uptime statistics."""
    path = path or metrics_path()
    samples: list[dict[str, Any]] = []
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                samples.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    if not samples:
        return {"samples": 0, "path": str(path)}

    def stat(key: str) -> dict[str, float] | None:
        values = [s[key] for s in samples if isinstance(s.get(key), (int, float))]
        if not values:
            return None
        return {
            "min": round(min(values), 1),
            "max": round(max(values), 1),
            "avg": round(sum(values) / len(values), 1),
        }

    brain_ok = [s["brain_ok"] for s in samples if isinstance(s.get("brain_ok"), bool)]
    duration = round(samples[-1]["ts"] - samples[0]["ts"], 1) if len(samples) > 1 else 0.0
    return {
        "samples": len(samples),
        "path": str(path),
        "duration_s": duration,
        "rss_mb": stat("rss_mb"),
        "cpu_percent": stat("cpu_percent"),
        "threads_max": max((s["threads"] for s in samples
                            if isinstance(s.get("threads"), int)), default=None),
        "brain_uptime_percent": (
            round(100.0 * sum(brain_ok) / len(brain_ok), 1) if brain_ok else None
        ),
    }
