"""定时主动发消息（限制得很死那种）。

规矩：
- 默认关闭，必须在微信里用 `/定时开关 on` 打开；
- 全号每天最多 `max_per_day` 条（默认 3）；
- 两次之间至少隔 `min_interval_minutes`（默认 180 分钟）；
- 只在 `window` 时间段内、且不在免打扰时段内发；
- 锁屏不发（发不出去，还容易被判异常）；
- 每条都要过防封的内容检查（链接/手机号/高危词）；
- 目标必须是你自己用 `/定时加` 明确指定的，绝不群发。
"""
from __future__ import annotations

import json
import logging
import random
import threading
import time
from datetime import datetime
from pathlib import Path

log = logging.getLogger("proactive")

DEFAULTS = {
    "enabled": False,
    "max_per_day": 3,
    "min_interval_minutes": 180,
    "window": "09:00-22:00",
    "items": [],       # [{"who": "文件传输助手", "at": "09:30", "text": "早安"}]
    "day": "",
    "count": 0,
    "last_ts": 0.0,
    "fired": {},       # {"0": "2026-10-22"}
}


def _parse_hhmm(s: str) -> tuple[int, int]:
    h, _, m = s.partition(":")
    return int(h), int(m or 0)


class Scheduler:
    def __init__(self, path: str | Path, guard=None, sender=None, quiet: dict | None = None,
                 on_sent=None):
        self.path = Path(path)
        self.guard = guard
        self.send = sender
        self.log = logging.getLogger("proactive")
        self.quiet = quiet or {}
        # on_sent(who, text)：发出去之后回调，用来把主动消息记进消息表，
        # 这样她下一轮读上下文时能看到“自己刚主动说了什么”。
        self.on_sent = on_sent
        self.state = self._load()
        self._lock = threading.Lock()
        self._mtime = self._mtime_now()

    def _mtime_now(self) -> float:
        try:
            return self.path.stat().st_mtime
        except OSError:
            return 0.0

    def _reload_if_changed(self) -> None:
        """面板（panel.py）直接改 json；这里按 mtime 热加载，不用重启桥。

        只覆盖「配置项」，当天的 count/last_ts/fired 这些运行时状态保留，
        否则改个时间窗就会把当天额度清空。
        """
        mt = self._mtime_now()
        if mt == self._mtime or mt == 0.0:
            return
        self._mtime = mt
        fresh = self._load()
        for k in ("enabled", "max_per_day", "min_interval_minutes", "window", "items", "random"):
            if k in fresh:
                self.state[k] = fresh[k]
        log.info("定时设置已热加载：开=%s 每日≤%s 条 间隔≥%s 分钟 窗口=%s 条目=%s",
                 self.state["enabled"], self.state["max_per_day"],
                 self.state["min_interval_minutes"], self.state["window"],
                 len(self.state["items"]))


    # ---------- 状态 ----------
    def _load(self) -> dict:
        data = dict(DEFAULTS)
        try:
            if self.path.exists():
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                data.update(raw)
        except Exception as e:
            log.warning("定时状态读不出来，用默认值：%s", e)
        data["items"] = list(data.get("items") or [])
        return data

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.path)
        except Exception as e:
            log.warning("定时状态写不进去：%s", e)

    def _roll(self, today: str) -> None:
        if self.state.get("day") != today:
            self.state["day"] = today
            self.state["count"] = 0
            self.state["fired"] = {}

    # ---------- 命令用 ----------
    def describe(self) -> str:
        s = self.state
        head = "主动消息：关" if not s["enabled"] else "主动消息：开"
        lines = [f"{head}（今天 {s['count']}/{s['max_per_day']} 条，间隔≥{s['min_interval_minutes']} 分钟，{s['window']}）"]
        if not s["items"]:
            lines.append("还没有定时项。加一条：/定时加 09:30 文件传输助手 早安")
        for i, it in enumerate(s["items"]):
            lines.append(f"{i}. {it['at']} → {it['who']}：{it['text']}")
        return "\n".join(lines)

    def enable(self, on: bool) -> str:
        self.state["enabled"] = on
        self.save()
        return "定时主动已打开。" if on else "定时主动已关闭。"

    # ---------- 调度 ----------
    def _rand(self) -> dict:
        """随机段配置：默认值 + 文件里的 random 段。"""
        conf = dict(getattr(self, "RAND_DEFAULTS", {}))
        conf.update(self.state.get("random") or {})
        return conf

    def _in_window(self, now: datetime) -> bool:
        try:
            a, _, b = self.state["window"].partition("-")
            sh, sm = _parse_hhmm(a)
            eh, em = _parse_hhmm(b)
        except Exception:
            return True
        cur = now.hour * 60 + now.minute
        start, end = sh * 60 + sm, eh * 60 + em
        return start <= cur <= end if start <= end else (cur >= start or cur <= end)

    def _in_quiet(self, now: datetime) -> bool:
        q = self.quiet or {}
        if not q.get("enabled"):
            return False
        try:
            sh, sm = _parse_hhmm(q.get("start", "23:30"))
            eh, em = _parse_hhmm(q.get("end", "08:00"))
        except Exception:
            return False
        cur = now.hour * 60 + now.minute
        start, end = sh * 60 + sm, eh * 60 + em
        return start <= cur < end if start <= end else (cur >= start or cur < end)

    def random_tick(self, now=None, compose=None, target_pool=None) -> list[str]:
        """每调用一次掷一回骰子；命中才发一条。返回发出去的说明。"""
        import random as _r
        from datetime import datetime as _dt

        self._reload_if_changed()
        cfg = self._rand()
        if not cfg.get("enabled"):
            return []
        now = now or _dt.now()
        if not self._in_window(now) or self._in_quiet(now):
            return []
        st = self.state.setdefault("random_state", {"day": "", "count": 0, "last_ts": 0.0, "sent": []})
        today = now.strftime("%Y-%m-%d")
        if st.get("day") != today:
            st.update({"day": today, "count": 0, "sent": []})
        if st["count"] >= int(cfg.get("per_day") or 0):
            return []
        if (now.timestamp() - float(st.get("last_ts") or 0)) < int(cfg.get("min_interval_minutes") or 0) * 60:
            return []
        if _r.random() >= float(cfg.get("chance") or 0):
            return []
        pool = [t for t in (cfg.get("targets") or []) if t] or list(target_pool or [])
        # 每天发给不同的人：今天已经发过的从池子里剔掉
        done = set(st.get("sent") or [])
        pool = [t for t in pool if t not in done]
        if not pool:
            return []
        target = _r.choice(pool)
        text = ""
        if compose is not None:
            try:
                text = (compose(target) or "").strip()
            except Exception as e:
                self.log.warning("随机主动生成失败：%r", e)
                return []
        if not text:
            return []
        if self.guard is not None:
            ok, why = self.guard.check_outgoing(text, target)
            if not ok:
                self.log.info("随机主动被内容检查拦下（%s）", why)
                return []
            try:
                self.guard.delay_for(text)
            except Exception:
                pass
        if self.send is None:
            return []
        try:
            ok = self.send(target, text)
        except Exception as e:
            self.log.warning("随机主动发送失败：%r", e)
            ok = False
        if not ok:
            self.log.warning("随机主动：发送返回 False（%s）", target)
            return []
        st["count"] += 1
        st["last_ts"] = now.timestamp()
        st["sent"] = list(dict.fromkeys((st.get("sent") or []) + [target]))
        self.state["random_state"] = st
        self.save()
        if self.on_sent is not None:
            try:
                self.on_sent(target, text)
            except Exception as e:
                self.log.warning("主动消息记账失败：%r", e)
        self.log.info("随机主动发出：%s → %s", target, text[:30])
        return [f"{target}：{text[:40]}"]


def make_scheduler(settings_dir: str, guard=None, sender=None, quiet: dict | None = None,
                   on_sent=None) -> Scheduler:
    return Scheduler(Path(settings_dir) / "proactive.json", guard=guard, sender=sender,
                     quiet=quiet, on_sent=on_sent)
