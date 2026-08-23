"""克隆音色注册表：JSON 索引 + 参考音频落盘（重启不丢）。

合规设计（参考 IndexTTS 官方 vLLM recipe 的强制同意标识）：
每条注册记录都携带注册时的同意声明文本（``consent`` 字段）作为审计
痕迹——参考音频属于说话人生物特征，注册即提取音色特征，必须留痕。

IndexTTS 引擎自带参考音频特征缓存（同一音色重复合成不重复提取特征），
sidecar 层只负责注册表的持久化，不做特征缓存。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger("vocalis.voice.sidecar.registry")

#: 安全音色名：字母/数字/下划线/连字符，1-64 字符。
#: 音色名会拼进文件路径（refs/<name>.wav），必须杜绝路径穿越。
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")

#: 注册条目的必备字段（缺一不可——被篡改/损坏的注册表条目直接丢弃）。
_REQUIRED_ENTRY_FIELDS = ("name", "ref_file", "language", "consent", "created_at")


def validate_name(name: str) -> str:
    """校验音色名（防路径穿越/非法字符）；通过则原样返回。"""
    if not _SAFE_NAME_RE.match(name or ""):
        raise ValueError(
            f"非法音色名 {name!r}：仅允许字母/数字/下划线/连字符（1-64 字符）"
        )
    return name


class VoiceRegistry:
    """已注册克隆音色的磁盘持久化注册表（voices.json + refs/ 目录）。

    参数：
        root: 注册表根目录（``voices.json`` 与 ``refs/`` 都在其中）。
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.refs_dir = self.root / "refs"
        self.refs_dir.mkdir(parents=True, exist_ok=True)
        self.index_path = self.root / "voices.json"
        self._entries: dict[str, dict] = self._load()

    # -- 查询 -------------------------------------------------------------
    def list(self) -> list[dict]:
        """全部注册条目（按注册先后排序的浅拷贝）。"""
        return [dict(entry) for entry in self._entries.values()]

    def get(self, name: str) -> dict | None:
        """按名取条目（无则 None）；返回浅拷贝防止调用方污染内部状态。"""
        entry = self._entries.get(name)
        return dict(entry) if entry is not None else None

    def ref_path(self, name: str) -> Path:
        """name 的参考音频路径（调用方须先 :meth:`get` 确认存在）。

        双保险：``_load`` 已对条目做白名单校验，这里再 ``resolve()`` 并
        确认结果仍在 refs 目录内——即使注册表被篡改（绕过加载期校验的
        竞态、或运行期被外部改写）也不会路径穿越读到任意文件。
        """
        entry = self._entries[name]
        path = (self.refs_dir / str(entry["ref_file"])).resolve()
        refs_root = self.refs_dir.resolve()
        if not path.is_relative_to(refs_root):
            raise ValueError(
                f"音色 {name!r} 的参考音频路径 {path} 逃逸出 refs 目录，"
                "疑似注册表被篡改：条目将被忽略"
            )
        return path

    def __len__(self) -> int:
        return len(self._entries)

    # -- 写入 -------------------------------------------------------------
    def add(self, name: str, language: str, consent: str, audio: bytes) -> dict:
        """注册/覆盖一个克隆音色：参考音频落盘 + 索引原子更新。

        同名重复注册按"覆盖更新"处理（换参考音频/改语言），旧参考文件
        被新文件替换，不留孤儿文件。
        """
        validate_name(name)
        entry = {
            "name": name,
            "language": language,
            "kind": "cloned",
            "consent": consent,  # 审计留痕：注册时的同意声明原文
            "ref_file": f"{name}.wav",
            "created_at": datetime.now(UTC).isoformat(),
        }
        (self.refs_dir / entry["ref_file"]).write_bytes(audio)
        self._entries[name] = entry
        self._save()
        logger.info("克隆音色已注册: %s (language=%s)", name, language)
        return dict(entry)

    # -- 持久化 -------------------------------------------------------------
    @classmethod
    def _validate_entry(cls, key: str, raw: object) -> dict | None:
        """逐条校验磁盘上的注册表条目；不合法返回 None（调用方丢弃）。

        磁盘上的 voices.json 是可被篡改的外部输入，加载时逐条做白名单
        校验（与 :meth:`add` 写入路径的约束完全一致）：
        * ``name`` 匹配 ``_SAFE_NAME_RE``（杜绝 ``../`` 等路径穿越）；
        * ``ref_file`` 必须是 ``<name>.wav``（防止指向任意文件）；
        * 必备字段（name/ref_file/language/consent/created_at）齐全且非空。
        """
        if not isinstance(raw, dict):
            logger.warning("注册表条目 %r 非 dict，已丢弃", key)
            return None
        name = raw.get("name")
        if not isinstance(name, str) or not _SAFE_NAME_RE.match(name or ""):
            logger.warning("注册表条目 %r 音色名非法（name=%r），已丢弃", key, name)
            return None
        if raw.get("ref_file") != f"{name}.wav":
            logger.warning(
                "注册表条目 %r 的 ref_file=%r 与音色名不匹配（应为 %r），已丢弃",
                key,
                raw.get("ref_file"),
                f"{name}.wav",
            )
            return None
        for field in _REQUIRED_ENTRY_FIELDS:
            value = raw.get(field)
            if not isinstance(value, str) or not value:
                logger.warning(
                    "注册表条目 %r 缺少必备字段 %r（或为空），已丢弃", key, field
                )
                return None
        if name != key:
            logger.warning("注册表条目键名 %r 与 name=%r 不一致，以 name 为准", key, name)
        return dict(raw)

    def _load(self) -> dict[str, dict]:
        if not self.index_path.exists():
            return {}
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            logger.warning("注册表 %s 损坏，忽略并重建", self.index_path, exc_info=True)
            return {}
        if not isinstance(data, dict):
            return {}
        entries: dict[str, dict] = {}
        for key, raw in data.items():
            entry = self._validate_entry(str(key), raw)
            if entry is not None:
                entries[str(entry["name"])] = entry
        return entries

    def _save(self) -> None:
        """原子写（tmp + replace）：崩溃不会留下半截 JSON。"""
        tmp = self.index_path.with_name(self.index_path.name + ".tmp")
        tmp.write_text(
            json.dumps(self._entries, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(self.index_path)
