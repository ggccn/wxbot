"""人设（persona）：可被主人在微信里用命令改写，立即生效。

人设文本存在 `<WXBOT_HOME>/PERSONA.md`，每次回复前读一次并注入提示词，
所以改完下一句就生效，不用重启。
"""
from __future__ import annotations

import logging
from pathlib import Path
from paths import PERSONA_FILE

log = logging.getLogger("persona")

DEFAULT_PATH = PERSONA_FILE

_BLANK = "（没有设定人设，按 AGENTS.md 的默认风格说话）"


class Persona:
    def __init__(self, path: str | Path = DEFAULT_PATH, max_chars: int = 3400):
        self.path = Path(path)
        self.max_chars = max_chars
        self._cache: str | None = None
        self._cache_mtime: float = 0.0

    # ---------------- 读 ----------------
    def text(self) -> str:
        """当前人设。文件没变就用缓存，避免每条消息都读盘。"""
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            return ""
        if self._cache is not None and mtime == self._cache_mtime:
            return self._cache
        try:
            raw = self.path.read_text(encoding="utf-8").strip()
        except OSError as e:
            log.warning("读人设失败：%s", e)
            return ""
        self._cache, self._cache_mtime = raw, mtime
        return raw

    def prefix(self) -> str:
        """注入提示词的前缀；没人设时返回空串。"""
        t = self.text()
        if not t:
            return ""
        return f"[人设，按这个说话]\n{t[: self.max_chars]}\n\n"

    # ---------------- 写 ----------------
    def set_text(self, text: str) -> str:
        text = text.strip()
        if not text:
            return self.clear()
        if len(text) > self.max_chars * 4:
            return f"太长了（上限 {self.max_chars * 4} 字），写短点。"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(text + "\n", encoding="utf-8")
        self._cache, self._cache_mtime = text, self.path.stat().st_mtime
        log.info("人设已更新（%d 字）", len(text))
        return f"好，人设换了，下一条回复就按这个来：\n{text[:120]}"

    def append(self, text: str) -> str:
        text = text.strip()
        if not text:
            return "要追加什么？"
        old = self.text()
        return self.set_text((old + "\n" + text) if old else text)

    def clear(self) -> str:
        try:
            self.path.unlink()
        except OSError:
            pass
        self._cache, self._cache_mtime = "", 0.0
        log.info("人设已清空")
        return "人设清掉了，回到默认风格。"

    def describe(self) -> str:
        t = self.text()
        return t if t else _BLANK
