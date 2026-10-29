"""运行时设置：节奏档、以及「档位 → 拟人延迟参数」的唯一一张表。

`/节奏` 命令和启动流程都走这里，避免两处各写一份表、越改越不一致。

踩过的坑：库（wechatauto.rhythm）的环境变量覆盖只在 `_from_env()` 里生效，
而 `set_profile()` 只用已经记下的 `_env_over`。启动顺序是「先设环境变量、再调
set_profile」，于是 WECHATAUTO_WRITE_GAP/BURST 其实从没生效过——实测 120 秒内
写满 6 次就冷却 66 秒，回复被拖到 70 多秒，表情包"卡在输入框里"就是这个冷却。
所以这里在切档之后，显式用 configure() 把间隔和突发上限再压一遍。
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

log = logging.getLogger("runtime")

# name -> (min_gap, max_gap, max_reply_delay) 秒
RHYTHM_TABLE = {
    "natural": (0.05, 0.15, 0.4),
    "calm": (4.0, 8.0, 15.0),
    "fast": (0.3, 0.8, 1.5),
}

DEFAULT_PROFILE = "natural"

# 库那层的写动作节流（默认值偏向"安全但很慢"，我们要的是"像人但别卡"）
WRITE_GAP_DEFAULT = 0.08
WRITE_BURST_DEFAULT = 40


def settings_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / "settings.json"


def load_settings(data_dir: str | Path) -> dict:
    try:
        return json.loads(settings_path(data_dir).read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_settings(data_dir: str | Path, settings: dict) -> None:
    try:
        p = settings_path(data_dir)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        log.debug("设置写盘失败：%s", e)


def _num(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def apply_profile(name: str, guard=None) -> str:
    """切换节奏档：库那层（发送间隔/突发上限）+ 我们这层的拟人延迟一起改。"""
    name = (name or DEFAULT_PROFILE).strip().lower()
    if name not in RHYTHM_TABLE:
        return f"未知档位 {name!r}"

    gap = _num("WECHATAUTO_WRITE_GAP", WRITE_GAP_DEFAULT)
    burst = int(_num("WECHATAUTO_WRITE_BURST", WRITE_BURST_DEFAULT))
    try:
        import wechatauto.rhythm as rhythm

        rhythm.set_profile(name)
        # 切档后显式覆盖，别指望环境变量（见模块 docstring）
        rhythm.configure(gap=(gap, gap * 2.0), burst=burst, cooloff=(0.2, 0.6),
                         type_gap=(0.01, 0.03))
    except Exception as e:
        log.warning("切换节奏档失败（库那层）：%s", e)

    lo, hi, cap = RHYTHM_TABLE[name]
    if guard is not None:
        guard.conf.min_gap_seconds = lo
        guard.conf.max_gap_seconds = hi
        guard.conf.max_reply_delay = cap
    return (f"{name}（回复前等 {lo:g}–{hi:g} 秒，封顶 {cap:g} 秒；"
            f"写动作间隔 {gap:g} 秒、120 秒内上限 {burst} 次）")
