"""Screen vision for D-VOICE: capture + OCR + active window title.

架构（CPU-only 友好）：
* 截屏   - Pillow ImageGrab（零新依赖），超大屏降采样后输出 PNG
* 看懂   - Windows 自带 OCR（winocr，~0.4s，中文引擎随系统语言包），
           不可用或结果过少时回退 RapidOCR（~6s，离线高精度）
* 理解   - OCR 文本交给大脑（DeepSeek）推理——"看"在本地，"想"在云端

隐私边界：OCR 全部本地完成；只有用户明确提问时，屏幕文字摘要才会
发给大脑。周期监管（ScreenWatcher）默认关闭。
"""

from __future__ import annotations

import asyncio
import ctypes
import io
import re
from dataclasses import dataclass, field

logger_name = "vocalis.vision"

# CJK 字符间被 Windows OCR 插入的空格 -> 合并（保留英文单词间空格）
_CJK_SPACING = re.compile(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])")
_CJK_MIXED = re.compile(r"(?<=[\u4e00-\u9fff])\s+(?=[A-Za-z0-9])")


@dataclass
class ScreenObservation:
    """一次屏幕观测的结果。"""

    title: str = ""
    text: str = ""
    lines: list[str] = field(default_factory=list)
    engine: str = ""

    def digest(self, max_chars: int = 1600) -> str:
        """喂给大脑的紧凑摘要（标题 + 正文截断）。"""
        head = f"活动窗口: {self.title}\n" if self.title else ""
        return head + self.text[:max_chars]


def capture_screen_png(max_width: int = 1920) -> bytes:
    """截取全屏并输出 PNG（超宽屏等比降采样，控制 OCR 与传输开销）。"""
    from PIL import ImageGrab

    img = ImageGrab.grab()
    if img.width > max_width:
        ratio = max_width / img.width
        img = img.resize((max_width, int(img.height * ratio)))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def active_window_title() -> str:
    """前台窗口标题（ctypes 零依赖，仅 Windows；其他平台返回空串）。"""
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        buf = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, buf, 512)
        return buf.value.strip()
    except Exception:  # noqa: BLE001 - 非 Windows / 权限受限时静默
        return ""


def _clean_cjk_spacing(lines: list[str]) -> list[str]:
    """合并 Windows OCR 在 CJK 字符间插入的空格。"""
    return [_CJK_MIXED.sub(" ", _CJK_SPACING.sub("", line)).strip() for line in lines]


async def ocr_png(png: bytes, lang: str = "zh-CN") -> ScreenObservation:
    """对屏幕截图做 OCR。主引擎 winocr（快），异常/结果过少时回退 RapidOCR。"""
    from PIL import Image

    img = Image.open(io.BytesIO(png))
    title = active_window_title()

    try:
        import winocr

        result = await winocr.recognize_pil(img, lang=lang)
        lines = _clean_cjk_spacing([line.text for line in result.lines])
        lines = [line for line in lines if line]
        if len(lines) >= 3:
            return ScreenObservation(
                title=title, text="\n".join(lines), lines=lines, engine="win-ocr"
            )
    except Exception as e:  # noqa: BLE001 - winocr 不可用时回退
        logger = __import__("logging").getLogger(logger_name)
        logger.warning("win-ocr unavailable (%s); falling back to rapidocr", e)

    # 兜底：RapidOCR（离线高精度，慢约 6s）
    def _rapid() -> list[str]:
        from rapidocr_onnxruntime import RapidOCR

        ocr = RapidOCR()
        result, _ = ocr(img)
        return _clean_cjk_spacing([line[1] for line in (result or [])])

    lines = await asyncio.to_thread(_rapid)
    return ScreenObservation(title=title, text="\n".join(lines), lines=lines, engine="rapidocr")


async def observe_screen(max_width: int = 1920, lang: str = "zh-CN") -> ScreenObservation:
    """截屏 + OCR 一步到位。"""
    png = await asyncio.to_thread(capture_screen_png, max_width)
    return await ocr_png(png, lang=lang)
