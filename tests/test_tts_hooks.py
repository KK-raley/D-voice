"""TTS pre/post 文本钩子测试（Track C4，完全离线）。

用 FakeEngine 替换 Edge-TTS（无网络、无音频硬件），VOCALIS_HOME
隔离到 tmp_path；覆盖钩子链式变换、异常隔离、成功/失败路径回调、
构造注入与运行时注册等契约。
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from vocalis.config import VocalisConfig
from vocalis.server.events import EventBus, EventType
from vocalis.voice.tts import TTSEngine, TTSService, VoiceProfile


# ---------------------------------------------------------------------
# fakes / helpers
# ---------------------------------------------------------------------
class FakeEngine(TTSEngine):
    """记录调用文本的假引擎，可选注入失败。"""

    name = "fake"

    def __init__(self, fail: bool = False) -> None:
        self.calls: list[str] = []
        self.fail = fail

    async def synthesize(self, text: str, profile: VoiceProfile) -> bytes:
        self.calls.append(text)
        if self.fail:
            raise RuntimeError("synthesis boom")
        return b"fake-audio"


def _service(
    tmp_path: Path,
    monkeypatch,
    engine: FakeEngine | None = None,
    **hook_kwargs,
) -> TTSService:
    """构造隔离的 TTSService：fake 引擎 + 独立 bus + tmp 配置目录。"""
    monkeypatch.setenv("VOCALIS_HOME", str(tmp_path))
    config = VocalisConfig()
    config.tts.engine = FakeEngine.name
    return TTSService(
        config,
        EventBus(),
        engines={FakeEngine.name: engine or FakeEngine()},
        **hook_kwargs,
    )


_DIGITS = "零一二三四五六七八九"


def _num_to_chinese(n: int) -> str:
    """0-99 的简单中文数字转换（测试辅助）。"""
    if n < 10:
        return _DIGITS[n]
    tens, ones = divmod(n, 10)
    head = "十" if tens == 1 else _DIGITS[tens] + "十"
    return head + (_DIGITS[ones] if ones else "")


def normalize_duration(text: str) -> str:
    """示例 pre 钩子：'2m41s' -> '两分四十一秒' 的文本规范化。"""

    def repl(m: re.Match[str]) -> str:
        minutes, seconds = int(m.group(1)), int(m.group(2))
        parts: list[str] = []
        if minutes:
            parts.append(("两" if minutes == 2 else _num_to_chinese(minutes)) + "分")
        if seconds or not minutes:
            parts.append(_num_to_chinese(seconds) + "秒")
        return "".join(parts)

    return re.sub(r"\b(\d+)m(\d+)s\b", repl, text)


# ---------------------------------------------------------------------
# pre_hooks：合成前文本变换
# ---------------------------------------------------------------------
def test_pre_hook_normalizes_text_before_synthesis(tmp_path, monkeypatch):
    engine = FakeEngine()
    svc = _service(tmp_path, monkeypatch, engine, pre_hooks=[normalize_duration])

    result = asyncio.run(svc.synthesize("渲染完成，用时 2m41s"))

    assert engine.calls == ["渲染完成，用时 两分四十一秒"]
    assert result.ok is True
    assert result.characters == len("渲染完成，用时 两分四十一秒")


def test_pre_hook_text_reaches_tts_speaking_event(tmp_path, monkeypatch):
    """tts.speaking 事件载荷应携带变换后的文本（HUD 展示实际口播内容）。"""
    svc = _service(tmp_path, monkeypatch, pre_hooks=[normalize_duration])

    asyncio.run(svc.synthesize("2m41s"))

    event = svc.bus.history[-1]
    assert event.type is EventType.TTS_SPEAKING
    assert event.data["text"] == "两分四十一秒"


def test_pre_hooks_chain_in_order(tmp_path, monkeypatch):
    engine = FakeEngine()
    svc = _service(
        tmp_path,
        monkeypatch,
        engine,
        pre_hooks=[lambda t: t.upper(), lambda t: t.replace(" ", "-")],
    )

    asyncio.run(svc.synthesize("hello world"))

    assert engine.calls == ["HELLO-WORLD"]


def test_pre_hook_exception_is_isolated(tmp_path, monkeypatch):
    """单个 pre 钩子抛异常：记 warning、保留当前文本，合成照常进行。"""
    engine = FakeEngine()

    def broken(text: str) -> str:
        raise ValueError("bad hook")

    svc = _service(tmp_path, monkeypatch, engine, pre_hooks=[broken, lambda t: t + "!"])

    result = asyncio.run(svc.synthesize("still works"))

    assert engine.calls == ["still works!"]  # broken 被跳过，后续钩子继续
    assert result.ok is True


def test_add_pre_hook_registers_runtime(tmp_path, monkeypatch):
    engine = FakeEngine()
    svc = _service(tmp_path, monkeypatch, engine)
    svc.add_pre_hook(lambda t: f"[note] {t}")

    asyncio.run(svc.synthesize("hi"))

    assert engine.calls == ["[note] hi"]


# ---------------------------------------------------------------------
# post_hooks：合成后回调
# ---------------------------------------------------------------------
def test_post_hook_called_on_success(tmp_path, monkeypatch):
    seen: list[tuple[str, bool]] = []
    svc = _service(
        tmp_path, monkeypatch, post_hooks=[lambda text, ok: seen.append((text, ok))]
    )

    result = asyncio.run(svc.synthesize("done"))

    assert result.ok is True
    assert seen == [("done", True)]


def test_post_hook_called_on_engine_failure(tmp_path, monkeypatch):
    seen: list[tuple[str, bool]] = []
    svc = _service(
        tmp_path,
        monkeypatch,
        FakeEngine(fail=True),
        post_hooks=[lambda text, ok: seen.append((text, ok))],
    )

    result = asyncio.run(svc.synthesize("will fail"))

    assert result.ok is False
    assert result.error == "synthesis boom"
    assert seen == [("will fail", False)]


def test_post_hook_called_on_missing_engine(tmp_path, monkeypatch):
    """配置的引擎未注册（ok=False 路径）也应触发 post 钩子。"""
    monkeypatch.setenv("VOCALIS_HOME", str(tmp_path))
    config = VocalisConfig()
    config.tts.engine = "ghost-engine"
    seen: list[tuple[str, bool]] = []
    svc = TTSService(
        config,
        EventBus(),
        engines={FakeEngine.name: FakeEngine()},
        post_hooks=[lambda text, ok: seen.append((text, ok))],
    )

    result = asyncio.run(svc.synthesize("no engine"))

    assert result.ok is False
    assert seen == [("no engine", False)]


def test_post_hook_receives_transformed_text(tmp_path, monkeypatch):
    """post 钩子收到的是变换后（实际合成）的文本。"""
    seen: list[tuple[str, bool]] = []
    svc = _service(
        tmp_path,
        monkeypatch,
        pre_hooks=[normalize_duration],
        post_hooks=[lambda text, ok: seen.append((text, ok))],
    )

    asyncio.run(svc.synthesize("took 2m41s"))

    assert seen == [("took 两分四十一秒", True)]


def test_post_hook_exception_is_isolated(tmp_path, monkeypatch):
    """post 钩子抛异常不影响合成结果返回。"""

    def broken(text: str, ok: bool) -> None:
        raise RuntimeError("stats sink down")

    engine = FakeEngine()
    svc = _service(tmp_path, monkeypatch, engine, post_hooks=[broken])

    result = asyncio.run(svc.synthesize("unaffected"))

    assert result.ok is True
    assert engine.calls == ["unaffected"]


def test_add_post_hook_registers_runtime(tmp_path, monkeypatch):
    seen: list[tuple[str, bool]] = []
    svc = _service(tmp_path, monkeypatch)
    svc.add_post_hook(lambda text, ok: seen.append((text, ok)))

    asyncio.run(svc.synthesize("runtime"))

    assert seen == [("runtime", True)]


# ---------------------------------------------------------------------
# speak() 复用钩子
# ---------------------------------------------------------------------
def test_speak_applies_hooks(tmp_path, monkeypatch):
    """speak() 内部走 synthesize()，同样应用 pre/post 钩子。"""
    engine = FakeEngine()
    seen: list[tuple[str, bool]] = []
    svc = _service(
        tmp_path,
        monkeypatch,
        engine,
        pre_hooks=[normalize_duration],
        post_hooks=[lambda text, ok: seen.append((text, ok))],
    )

    result = asyncio.run(svc.speak("耗时 1m05s", play=False))  # play=False：无音频硬件

    assert engine.calls == ["耗时 一分五秒"]
    assert result.ok is True
    assert seen == [("耗时 一分五秒", True)]
