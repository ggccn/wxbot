"""防封层：白名单、每日预算、拟人延迟、内容红线、熔断。

为什么要有这一层
----------------
微信的风控看的是「行为的时间分布 + 内容结构 + 被投诉记录」，不是看你有没
有装某个库。所以降低封号概率靠的是让这个号看起来像人在用：

1. 只回白名单里的人（不碰陌生人 = 不产生骚扰投诉）；
2. 绝不主动发（不进群拉人、不群发、不加好友）；
3. 每条回复前有 2–8 秒不等的"阅读+打字"延迟，长回复更久；
4. 每人每天、全账号每天都有硬上限，白天之外不出声；
5. 内容层面禁掉链接、手机号、二维码、转账/兼职/刷单这类高危词；
6. 发送失败或微信出现异常提示时立刻熔断，停手等人来看。

已知依据：
- 腾讯云社区《微信封号机制，其实是个"行为协议"》把判定归纳为消息频率/
  重复率/内容结构、群操作频率、登录心跳、页面动作轨迹、网络指纹几类。
- wechatauto 作者在 2026-09-21 用真实账号跑 UI 自动化时触发过一次风控
  （会话中途被要求重新登录），因此库内置了 rhythm.py 拟人节奏层。
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional

log = logging.getLogger("safety")

# ---- 内容红线 -------------------------------------------------------------
URL_RE = re.compile(r"(https?://|www\.|t\.cn/|mp\.weixin\.qq\.com)", re.I)
PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")

RISK_WORDS = (
    # 金钱 / 账号安全
    "转账", "红包", "收款码", "付款码", "银行卡", "身份证", "验证码",
    "贷款", "借条", "发票", "套现", "洗钱",
    # 拉人 / 营销
    "加好友", "加微信", "群发", "扫码进群", "私聊我", "代理", "返利", "刷单",
    "兼职日结", "博彩", "赌博", "外围", "成人", "裸聊",
    # 引流
    "二维码", "扫一扫", "下载APP", "下载 APP",
)

DEFAULT_BLOCKED_REPLY = "这个话题我不太方便接，换个别的吧。"


@dataclass
class SafetyConfig:
    """防封参数。默认值已经是保守档，改大之前先想清楚。"""
    # 白名单：空列表 + require_allowlist=True 表示"谁都不回"（安全默认）
    allow_contacts: list = field(default_factory=list)
    allow_groups: list = field(default_factory=list)
    require_allowlist: bool = True
    # True：除了 allow_contacts 里的人，**本号好友**也可以聊（陌生人仍然一律不回）。
    # 想让朋友都能跟它说话就设 True；只想自己用就设 False。
    allow_friends: bool = False
    # 每日预算
    daily_per_contact: int = 80
    daily_total: int = 300
    warmup_days: int = 7          # 头几天按更严的额度走
    warmup_daily_total: int = 60
    warmup_daily_per_contact: int = 30
    # 拟人延迟
    min_gap_seconds: float = 2.0
    max_gap_seconds: float = 8.0
    per_char_seconds: float = 0.045
    max_reply_delay: float = 15.0
    # 重复内容抑制
    repeat_window_seconds: int = 1800
    max_repeat: int = 2
    # 熔断
    fail_streak_limit: int = 3
    cooldown_seconds: int = 1800
    blocked_reply: str = DEFAULT_BLOCKED_REPLY
    # 只在屏幕解锁时才发（GUI 自动化锁屏会失败）
    require_unlocked: bool = True


def _today() -> str:
    return date.today().isoformat()


def is_screen_locked() -> bool:
    """当前会话是否处于锁屏状态（GUI 自动化在锁屏时点不到窗口）。"""
    try:
        import ctypes
        user32 = ctypes.windll.user32
        hdesk = user32.OpenInputDesktop(0, False, 0x0100)  # DESKTOP_SWITCHDESKTOP
        if not hdesk:
            return True
        user32.CloseDesktop(hdesk)
        return False
    except Exception:
        return False


class SafetyGuard:
    """有状态的防封闸门。状态落盘到 data/safety.json，重启不清零。"""

    def __init__(self, conf: SafetyConfig, state_path: str | Path, friend_check=None,
                 admins=()):
        self.conf = conf
        self.path = Path(state_path)
        # 聊天框里 /上限 改的值（同一个 data 目录）
        self._limits_path = self.path.parent / "limits.json"
        self._limits_mtime = 0.0
        self._limits: dict = {}
        self._lock = threading.RLock()
        self._state = self._load()
        # friend_check(wxid) -> bool：判断对方是不是本号好友（用于 allow_friends 模式）
        self._friend_check = friend_check
        # 主人的会话不吃"单人每日条数"这一条（否则自己连着测一会儿就被自家闸门锁死，
        # 表现就是"突然不回了"）。全局每日上限、每分钟限流、内容审查仍然照常生效。
        self._admins = set(admins or ())

    # ---------------- 状态 ----------------
    def _blank(self) -> dict:
        return {"day": _today(), "total": 0, "per": {}, "first_run": time.time(),
                "fail_streak": 0, "cooldown_until": 0.0, "trip_reason": ""}

    def _load(self) -> dict:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return self._blank()
        st = self._blank()
        st.update({k: raw[k] for k in st if k in raw})
        if st["day"] != _today():          # 跨天清零
            st["day"], st["total"], st["per"] = _today(), 0, {}
        return st

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._state, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path)
        except Exception as e:
            log.debug("防封状态写盘失败：%s", e)

    def _roll_day(self) -> None:
        if self._state["day"] != _today():
            self._state.update(day=_today(), total=0, per={})

    # ---------------- 闸门 ----------------
    def trip(self, reason: str) -> None:
        with self._lock:
            self._state["trip_reason"] = reason
            self._state["cooldown_until"] = time.time() + self.conf.cooldown_seconds
            self._save()
        log.error("防封熔断：%s（冷却 %d 秒）", reason, self.conf.cooldown_seconds)

    def tripped(self) -> Optional[str]:
        with self._lock:
            until = float(self._state.get("cooldown_until") or 0)
            if until and time.time() < until:
                return self._state.get("trip_reason") or "熔断中"
            return None

    def _overrides(self) -> dict:
        """data/limits.json：聊天框里 /上限 改的值，改完立刻生效，不用重启。"""
        try:
            m = self._limits_path.stat().st_mtime
        except Exception:
            return {}
        if m != getattr(self, "_limits_mtime", 0.0):
            try:
                self._limits_mtime = m
                self._limits = json.loads(self._limits_path.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return getattr(self, "_limits", {}) or {}

    def set_limit(self, name: str, value) -> str:
        cur = dict(self._overrides())
        cur[name] = value
        self._limits_path.parent.mkdir(parents=True, exist_ok=True)
        self._limits_path.write_text(json.dumps(cur, ensure_ascii=False), encoding="utf-8")
        self._limits = cur
        try:
            self._limits_mtime = self._limits_path.stat().st_mtime
        except Exception:
            pass
        return self.describe_limits()

    def describe_limits(self) -> str:
        ov = self._overrides()
        c = self.conf
        lines = [
            f"全号每天：{ov.get('daily_total', c.daily_total)} 条（新手期 {c.warmup_daily_total}）",
            f"每人每天：{ov.get('daily_per_contact', c.daily_per_contact)} 条（新手期 {c.warmup_daily_per_contact}）",
        ]
        if ov.get("unlimited"):
            lines.insert(0, "当前：不限量（主人手动关掉的）")
        lines.append("改：/上限 日 300 ／ /上限 人 80 ／ /上限 关 ／ /上限 看")
        return "\n".join(lines)

    def allow_incoming(self, key: str, is_group: bool = False) -> tuple[bool, str]:
        """这条消息该不该回。key 是会话 id（私聊 wxid / 群 id）。"""
        if self.tripped():
            return False, "cooling-down"
        c = self.conf
        if c.require_allowlist:
            pool = c.allow_groups if is_group else c.allow_contacts
            listed = bool(pool) and (key in pool or "*" in pool)
            # allow_friends=True 时，只要对方是本号好友就放行（陌生人仍然不理）。
            # 好友才有"主动发消息并被投诉"的能力，所以这一层比 allowlist 松、
            # 但比"见谁都回"紧。
            friend_ok = False
            if not listed and c.allow_friends and not is_group and self._friend_check:
                try:
                    friend_ok = bool(self._friend_check(key))
                except Exception as e:
                    log.debug("好友判定失败 %s：%s", key, e)
                    friend_ok = False
            if not listed and not friend_ok:
                return False, "not-in-allowlist"
        with self._lock:
            self._roll_day()
            st = self._state
            age_days = (time.time() - float(st.get("first_run") or time.time())) / 86400.0
            warm = age_days < c.warmup_days
            cap_total = c.warmup_daily_total if warm else c.daily_total
            cap_one = c.warmup_daily_per_contact if warm else c.daily_per_contact
            # 聊天框里 /上限 设置的临时上限优先生效（存 data/limits.json，热加载）
            ov = self._overrides()
            cap_total = int(ov.get("daily_total", cap_total))
            cap_one = int(ov.get("daily_per_contact", cap_one))
            if ov.get("unlimited"):
                cap_total = cap_one = 10 ** 9
            if key in self._admins:
                cap_total = max(cap_total, 10 ** 9) if ov.get("admin_unlimited", True) else cap_total
            if st["total"] >= cap_total:
                return False, "daily-total"
            if key not in self._admins and st["per"].get(key, 0) >= cap_one:
                return False, "daily-contact"
        if c.require_unlocked and is_screen_locked():
            return False, "screen-locked"
        return True, "ok"

    def check_outgoing(self, text: str, key: str = "") -> tuple[bool, str]:
        """发送前的内容审查。返回 (是否放行, 原因)。"""
        if URL_RE.search(text):
            return False, "url"
        if PHONE_RE.search(text):
            return False, "phone"
        low = text.lower()
        for w in RISK_WORDS:
            if w.lower() in low:
                return False, "risk-word:" + w
        if key:
            with self._lock:
                rec = self._state.setdefault("recent", {}).setdefault(key, [])
                now = time.time()
                rec = [r for r in rec if now - r[0] <= self.conf.repeat_window_seconds]
                norm = re.sub(r"\s+", "", text)[:120]
                same = sum(1 for t, s in rec if s == norm)
                if same >= self.conf.max_repeat:
                    return False, "repeat"
        return True, "ok"

    def delay_for(self, text: str) -> float:
        """回复前的"阅读 + 打字"延迟（秒）。长回复更久，但封顶。"""
        import random
        c = self.conf
        base = random.uniform(c.min_gap_seconds, c.max_gap_seconds)
        extra = min(len(text) * c.per_char_seconds, c.max_reply_delay)
        total = min(base + extra, c.max_reply_delay + c.max_gap_seconds)
        time.sleep(total)
        return total

    def note_sent(self, key: str, text: str = "") -> None:
        with self._lock:
            self._roll_day()
            st = self._state
            st["total"] += 1
            st["per"][key] = st["per"].get(key, 0) + 1
            st["fail_streak"] = 0
            if text:
                rec = st.setdefault("recent", {}).setdefault(key, [])
                rec.append([time.time(), re.sub(r"\s+", "", text)[:120]])
                st["recent"][key] = rec[-10:]
            self._save()

    def note_failed(self, reason: str = "send-failed") -> bool:
        """发送失败。连续失败到阈值就熔断，返回是否刚熔断。"""
        with self._lock:
            st = self._state
            st["fail_streak"] = int(st.get("fail_streak") or 0) + 1
            streak = st["fail_streak"]
            self._save()
        if streak >= self.conf.fail_streak_limit:
            self.trip(f"连续 {streak} 次发送失败：{reason}")
            return True
        return False

    # ---------------- 展示 ----------------
    def describe(self) -> str:
        with self._lock:
            self._roll_day()
            st = self._state
            age = (time.time() - float(st.get("first_run") or time.time())) / 86400.0
            warm = age < self.conf.warmup_days
            cap_total = self.conf.warmup_daily_total if warm else self.conf.daily_total
            lines = [
                f"今日已发 {st['total']}/{cap_total} 条"
                + ("（新手保护期）" if warm else ""),
                f"白名单：{len(self.conf.allow_contacts)} 人 / {len(self.conf.allow_groups)} 群"
                + ("＋所有好友" if self.conf.allow_friends else ""),
                f"熔断：{self.tripped() or '正常'}",
            ]
            return "；".join(lines)


def load_conf(raw: dict) -> SafetyConfig:
    c = SafetyConfig()
    for k, v in (raw or {}).items():
        if hasattr(c, k):
            setattr(c, k, v)
    return c
