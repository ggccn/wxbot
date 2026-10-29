"""好感度 / 心情值：按互动信号缓慢漂移，影响语气和要不要发表情。"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

DEFAULTS = {
    "affinity": 50.0,      # 对某个人的好感 0-100
    "mood": 60.0,          # 此刻心情 0-100（全局）
    "last_seen": 0.0,
}

# 对方这话是什么调子
_DISMISSIVE = re.compile(
    r"^(随便|随便你|无所谓|懒得|不想说|行吧|哦+|嗯+|呵+|6+|知道了|打住|别说了|"
    r"关你什么事|不关你事|算了|没必要|闭嘴)")
_WARM = re.compile(
    r"(谢谢|谢了|辛苦|你真|你太|可爱|有意思|喜欢|哈哈+|笑死|厉害|牛|早安|晚安|"
    r"想你了|在吗|你好|宝|抱抱)")
_HOSTILE = re.compile(
    r"(滚|傻[逼bB比]|智障|废物|闭嘴|去死|垃圾|有病|你算什么东西|机器人吧)")

LIMITS = {"affinity": (0.0, 100.0), "mood": (0.0, 100.0)}
DELTA = {
    "warm": {"affinity": +2.5, "mood": +2.0},
    "neutral": {"affinity": +0.4, "mood": +0.2},
    "dismissive": {"affinity": -2.0, "mood": -3.0},
    "hostile": {"affinity": -4.0, "mood": -6.0},
}
TAG = {"warm": "暖", "neutral": "平", "dismissive": "敷衍", "hostile": "冲"}


def classify(text: str) -> str:
    """给一句话定调：warm / neutral / dismissive / hostile。"""
    t = (text or "").strip()
    if not t:
        return "neutral"
    if _HOSTILE.search(t):
        return "hostile"
    if _DISMISSIVE.match(t) or (len(t) <= 4 and t.endswith(("吧", "了")) and "随便" in t):
        return "dismissive"
    if _WARM.search(t):
        return "warm"
    return "neutral"


def _clamp(key: str, v: float) -> float:
    lo, hi = LIMITS[key]
    return max(lo, min(hi, v))


def affinity_word(v: float) -> str:
    if v >= 80:
        return "很亲近，愿意多聊两句"
    if v >= 60:
        return "挺熟，放松"
    if v >= 40:
        return "一般，客客气气"
    if v >= 25:
        return "有点烦他，话会变短"
    return "很不待见，能不接就不接"


def mood_word(v: float) -> str:
    if v >= 80:
        return "心情很好，愿意撒娇"
    if v >= 60:
        return "心情不错"
    if v >= 40:
        return "一般般"
    if v >= 25:
        return "有点低落，不想卖萌"
    return "心情差，话少、别惹我"


class Mood:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.data = {"contacts": {}, "global": {"mood": DEFAULTS["mood"], "ts": 0.0}}
        self.load()

    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self.data.update(raw)
        except Exception:
            pass
        self.data.setdefault("contacts", {})
        self.data.setdefault("global", {"mood": DEFAULTS["mood"], "ts": 0.0})

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2) + "\n",
                                 encoding="utf-8")
        except Exception:
            pass

    # --- 读 ---
    def state(self, ident: str) -> dict:
        c = self.data["contacts"].setdefault(ident, dict(DEFAULTS))
        g = self.data["global"]
        return {"affinity": float(c.get("affinity", 50.0)),
                "mood": float(g.get("mood", 60.0)),
                "last_seen": float(c.get("last_seen", 0.0))}

    def allow_sticker(self, ident: str) -> bool:
        s = self.state(ident)
        return s["mood"] >= 35.0 and s["affinity"] >= 30.0

    def describe(self, ident: str) -> str:
        s = self.state(ident)
        return (f"好感 {s['affinity']:.0f}/100（{affinity_word(s['affinity'])}），"
                f"心情 {s['mood']:.0f}/100（{mood_word(s['mood'])}）")

    def prompt_line(self, ident: str) -> str:
        s = self.state(ident)
        ap = ("[你此刻的状态：" + self.describe(ident) +
              "。好感低就话短、少哄人；心情低就别撒娇、别主动发表情。这是你心里的感觉，"
              "别把它当数字念出来。]")
        return "\n" + ap

    # --- 写 ---
    def update(self, ident: str, text: str) -> dict:
        sig = classify(text)
        d = DELTA[sig]
        c = self.data["contacts"].setdefault(ident, dict(DEFAULTS))
        c["affinity"] = _clamp("affinity", float(c.get("affinity", 50.0)) + d["affinity"])
        c["last_seen"] = time.time()
        g = self.data["global"]
        g["mood"] = _clamp("mood", float(g.get("mood", 60.0)) + d["mood"])
        g["ts"] = time.time()
        self.save()
        return {"sig": sig, "sig_cn": TAG[sig], **self.state(ident)}

    def decay(self, hours: float = 8.0) -> None:
        """长时间没人说话，情绪回到中性（免得一直记仇）。"""
        g = self.data["global"]
        idle = (time.time() - float(g.get("ts", 0) or 0)) / 3600.0
        if idle >= hours:
            m = float(g.get("mood", 60.0))
            g["mood"] = m + (60.0 - m) * 0.3
            g["ts"] = time.time()
            self.save()
