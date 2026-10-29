"""消息路由核心：过滤、限流、排队、调大脑、回消息。

跟具体微信实现解耦 —— 只要能提供一个 sender(target, text, at_wxid) 就能用。
"""
from __future__ import annotations

import mood as mood_mod
import memory as memory_mod   # 聊完总结要用 memory_mod.summarize_idle（之前漏了这行，报 NameError）
import json
import logging
import random
import re
import threading
import time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from brain import Brain, BrainError
from safety import SafetyGuard
from paths import NOTES_DIR, MEMES_FILE, GAMES_FILE

log = logging.getLogger("bridge")

def _com_init() -> None:
    """线程池里每个工作线程先初始化 COM，否则 uiautomation 报 WinError -2147221008。"""
    try:
        import pythoncom

        pythoncom.CoInitialize()
    except Exception:
        pass

_WEEKDAYS = "一二三四五六日"

# 防封闸门拦下时的中文解释（主人问起"怎么不回了"，她能说清楚）
_BLOCK_CN = {
    "daily-contact": "今天跟这个人说得够多了",
    "daily-total": "今天的自动回复额度用完了",
    "cooling-down": "刚触发了保护性的冷却",
    "not-in-allowlist": "这个人不在白名单里",
    "screen-locked": "电脑锁屏了",
}

_HELP_TEXT = (
    "主人命令：\n"
    "/人设 <文本>  改人设（下一句就生效）；/人设 查看；/人设 清空；/人设 追加 <文本>\n"
    "/节奏 natural|calm|fast  改回复快慢\n"
    "/主动 <目标> <内容>  让它主动发一条（昵称/备注/群名/文件传输助手）；不带内容 = 让她自己编一句\n"
    "/同意  通过待处理的好友申请\n"
    "/表情  看表情库；/表情 找 <标签> [关键词] 去网上抓；/表情 发 <标签> [目标]；/表情 加 <标签> <路径或链接>\n"
    "/戳 <目标>  戳一戳对方\n"
    "/上限  看今日上限；/上限 日 300；/上限 人 80；/上限 关（不限量）；/上限 开\n"
    "/停  全部静音只回你；/继续  解除静音；/状态  看静音/排队/额度\n"
    "/stats 统计  /safety 防封状态  /reset 清记忆"
)


def _save_settings(settings: dict) -> None:
    """把运行时设置（目前是节奏档）写回 data/settings.json。"""
    try:
        import json

        path = Path(__file__).resolve().parent / "data" / "settings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:  # 写不了不影响运行
        log.debug("设置写盘失败：%s", e)


def _notes_path(ident: str, alias: str = "") -> Path:
    """笔记按 **wxid** 存，改备注不会丢记忆。

    旧版本按显示名存，所以这里留了兼容：wxid 文件不存在但有同名的旧文件时，
    读的时候照旧能读到（第一次写会自动用 wxid 文件名，等于自然迁移）。
    """
    safe = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", (ident or "").strip())[:40] or "unknown"
    return NOTES_DIR / f"{safe}.md"


def _legacy_path(alias: str) -> Path:
    safe = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", (alias or "").strip())[:40] or "unknown"
    return NOTES_DIR / f"{safe}.md"


def trim_note(path: Path, max_chars: int = 2400, keep_blocks: int = 8) -> None:
    r"""笔记别无限长：头部（称呼/印象）保留，只留最近 keep_blocks 段记忆，超长归档。

    归档文件：<notes>/_archive/<同名>.md（只追加，不参与提示词）
    """
    try:
        if not path.exists():
            return
        text = path.read_text(encoding="utf-8")
        if len(text) <= max_chars and text.count("\n## ") <= keep_blocks:
            return
        parts = re.split(r"(?m)^(?=## )", text)
        head = parts[0]
        if len(head) > 900:  # 群笔记的成员名单也会无限长，头部同样要收
            head = head[:900].rstrip() + "\n…（名单太长，中略）"
        blocks = parts[1:]
        keep = blocks[-keep_blocks:]
        drop = blocks[:-keep_blocks]
        if drop:
            arch = path.parent / "_archive" / path.name
            arch.parent.mkdir(parents=True, exist_ok=True)
            with open(arch, "a", encoding="utf-8") as f:
                f.write("".join(drop))
        _notes_path(path.stem)  # 触发目录创建
        path.write_text(head.rstrip() + "\n" + "".join(keep).strip() + "\n", encoding="utf-8")
    except Exception:
        pass



def _read_note(ident: str, alias: str = "", limit: int = 900) -> str:
    """读这个人的印象笔记（她自己维护的），用来决定怎么称呼、怎么接话。"""
    # 只认 wxid 文件。以前会退回「按名字存」的旧笔记，而那些文件是早期不分人
    # 写出来的（同一个人的笔记里混着别人的事），一律不读了。
    try:
        p = _notes_path(ident)
        if p.exists():
            text = p.read_text(encoding="utf-8").strip()
            if len(text) <= limit:
                return text
            # 头（称呼/印象）+ 尾（最新几次记忆）都要保住，中间截断
            head = text[: int(limit * 0.55)]
            tail = text[-int(limit * 0.40):]
            return head.rstrip() + "\n…（中略）…\n" + tail.lstrip()
    except OSError:
        pass
    return ""


def _memes_path() -> Path:
    return MEMES_FILE


def _read_memes(limit: int = 900) -> str:
    """梗库：她认得多少黑话，全看这里。太长就只塞前 900 字，正文在文件里。"""
    p = _memes_path()
    try:
        if not p.exists():
            return ""
        text = p.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    if len(text) > limit:
        text = text[:limit] + "\n…（还有更多，要查就用 read_file 看这个文件）"
    return text


def _games_path() -> Path:
    return GAMES_FILE


# 聊到"玩点什么"才把游戏规则塞进提示词，平时一个字都不占
_GAME_HINT = re.compile(r"游戏|玩点|玩个|猜|炸|接龙|二选一|无聊|没意思|打发|赌|输了|赢了|谁先")


def _read_games(limit: int = 700) -> str:
    """游戏库：她会玩的那几个小游戏的规则。"""
    try:
        p = _games_path()
        if not p.exists():
            return ""
        text = p.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    text = re.sub(r"(?m)^#.*$\n?", "", text).strip()
    if len(text) > limit:
        text = text[:limit]
    return text


def _with_clock(text: str) -> str:
    """给正文加一行「现在几点」，模型据此回答时间类问题。

    2026-10-22 实测：headless 会话里的 shell 工具在 `E:\\wxbot_home` 上会报
    `SetNamedSecurityInfoW failed (Win32 5): grantWrite(...)`，拿不到系统时间；
    把时间直接写进提示词最稳，也不依赖任何工具权限。
    """
    now = time.localtime()
    stamp = time.strftime("%Y-%m-%d %H:%M", now)
    week = _WEEKDAYS[now.tm_wday]
    return f"[现在是 {stamp} 星期{week}]\n{text}"


def _clip(text: str, cap: int = 40) -> str:
    """按标点截断，尽量不把她的话切在半句上（"最近 50 条"每行都占字数预算）。"""
    text = (text or "").strip()
    if len(text) <= cap:
        return text
    head = text[:cap + 1]
    cut = max(head.rfind(p) for p in "。！？…～；;，、, ")
    if cut >= int(cap * 0.6):
        return head[:cut + 1].rstrip(" ，,、")
    return text[:cap] + "…"


def _norm_in(text: str) -> str:
    """把一条来消息搓成"指纹"，用来判断对方是不是在复读同一句话。"""
    t = re.sub(r"\s+", "", str(text or ""))
    t = re.sub("[，。！？、,.!?…~～：:；;（）()\\[\\]\"'“”‘’]", "", t)
    return t[:80].lower()


@dataclass
class Msg:
    msgid: str
    sender: str              # 发送者 wxid
    room: str = ""           # 群 id，私聊为空
    text: str = ""
    at_me: bool = False
    is_group: bool = False
    alias: str = ""
    from_self: bool = False
    kind: str = "text"       # text / image / sticker / voice / system
    media: str = ""          # 本地文件路径（图片/表情下载下来的）
    ts: float = 0.0          # 这条消息在微信里的时间（补收时才知道）
    stale: bool = False      # True = 电脑没在的时候收到的，属于"迟到消息"


_STICKER_RE = re.compile(r"\[\[\s*表情\s*[:：]\s*([^\[\]]{1,20}?)\s*\]\]")
_ACTION_RE = re.compile(
    r"\[\[\s*动作\s*[:：]\s*([^|\[\]]{1,10})\s*\|\s*([^|\[\]]{0,40})\s*"
    r"(?:\|\s*([^\[\]]{0,400}))?\s*\]\]")
_MEDIA_RE = re.compile(r"\[[^\]]*文件：([^\]]+)\]")

# 「一次多回几条」：真人聊天常见连发两三句短话，而不是一条长段落。
# 面板（panel.py）改 data\behavior.json 就能调，桥按 mtime 热加载。
BEHAVIOR_DEFAULTS = {
    "bubbles_enabled": True,
    "bubble_chance": 0.45,      # 没有换行时，按句子拆成多条的概率
    "bubble_max": 2,            # 一条回复最多拆成几条（碎成 5 条就把对话切乱了）
    "bubble_gap_min": 0.05,      # 两条之间的停顿（秒）
    "bubble_gap_max": 0.15,
    "auto_sticker_chance": 0.15,            # 她主动补一张表情的概率
    "auto_sticker_tags": ["可爱", "开心", "害羞", "收到"],
    # 群聊（面板里可调，改完一分钟内生效）
    "group_mode": "smart",                  # smart / at / keyword / off
    "group_keywords": ["小鱼", "鲸鱼", "小助手"],
    "group_at_sender": False,               # 群里回复时要不要 @ 人
    "group_chime_chance": 0.05,             # 没人叫她时插一句的概率
    "group_chime_cooldown_minutes": 12,     # 同一个群两次插话的最小间隔
    "group_thread_minutes": 3,
    "private_mode": "", "private_prefix": "小鱼",
    "quiet_enabled": False, "quiet_start": "23:30", "quiet_end": "08:00",
    "rate_per_min": 20, "rate_per_hour": 240, "rate_global_min": 90,
    "merge_window_seconds": 1.2, "memes_enabled": False,
    "backfill_reply_minutes": 240,             # 0 = 关闭"接着说"判定
    # 好奇心：她本来就该对人有点兴趣，不然话题全靠对方提供
    "curiosity_enabled": True,
    "curiosity_chance": 0.3,               # 轮到可以问的时候，真问的概率
    "curiosity_cooldown_turns": 4,         # 同一个人至少隔几轮才问一次
    # 换话题的闸：默认只在当前话题里往下追问，别硬拐到清单上的另一个方向
    "curiosity_follow_topic": True,        # True=顺着他这句话追问细节；False=老行为（从话题清单里挑一个）
    "curiosity_lull_only": True,           # True=只在冷场/他这话很短的时候才注入
    "curiosity_lull_seconds": 90,          # 距他上一条超过这么多秒算冷场
    "curiosity_short_chars": 6,            # 他这条不超过这么多字算"很短"
    "curiosity_no_switch": True,           # 禁止「对了/话说/说起来/说真的」这类硬转场
    "curiosity_ping_enabled": True,   # 私聊对话收尾时按概率追问一句
    "curiosity_ping_chance": 0.3,     # 每次对话 30%
    "curiosity_ping_after_minutes": 1,
    "curiosity_ping_cooldown_minutes": 60,  # 追问后 1 小时内不再追问  # 对方 3 分钟没吭声算对话结束
}

# 好奇心：她该主动想了解对方，但问得像人——一次一个、具体、别查户口。
_CURIOUS_TOPICS = [
    ("作息", ("几点睡", "几点起", "熬夜", "作息", "夜班", "睡不睡")),
    ("吃饭口味", ("辣", "甜", "口味", "爱吃", "不吃", "外卖", "奶茶", "火锅", "食堂")),
    ("在忙什么", ("上班", "上课", "打工", "上班族", "工作", "实习", "作业", "考试", "忙")),
    ("游戏和玩的", ("打瓦", "游戏", "上分", "steam", "手游", "玩什么", "rank")),
    ("住哪一带", ("城市", "哪的", "住", "北方", "南方", "本地", "外地")),
    ("身边的小动物", ("猫", "狗", "养", "宠物")),
    ("最近在追什么", ("番", "剧", "看什么", "追番", "电影", "听什么")),
    ("脾气和底线", ("别惹", "介意", "底线", "讨厌", "不喜欢")),
    ("上课和作业", ("上课", "作业", "课表", "点名", "考试")),
    ("家里人和老家", ("家里", "老家", "爸爸", "妈妈", "兄弟", "姐妹")),
    ("朋友和局", ("朋友", "宿舍", "开黑", "局", "聚会")),
    ("音乐口味", ("歌", "单曲", "循环", "乐队", "rap", "说唱")),
    ("电影和番", ("番", "动画", "电影", "剧", "二刷")),
    ("手机和电脑", ("手机", "电脑", "显卡", "配置", "型号")),
    ("钱和花销", ("生活费", "花销", "买", "贵", "省钱", "工资")),
    ("运动", ("运动", "跑步", "健身", "球", "骑车")),
    ("梦想和以后", ("以后", "梦想", "想干", "大学", "考研", "城市")),
    ("口头禅", ("口头禅", "爱说", "常挂嘴边")),
    ("小时候", ("小时候", "小时候", "童年", "以前")),
    ("最近开心的事", ("开心", "爽", "高兴", "好事")),
    ("讨厌的东西", ("烦", "讨厌", "受不了", "雷")),
]

_CURIO_ASK_HINT = {
    "作息": "几点睡啊 / 是不是又熬到后半夜了",
    "吃饭口味": "吃不吃辣 / 今天吃了啥",
    "在忙什么": "今天忙不忙啊 / 手上那事弄完了没",
    "游戏和玩的": "最近在打什么 / 上分了没",
    "住哪一带": "你那边现在冷不冷 / 早上几点天亮",
    "身边的小动物": "你那儿有猫吗",
    "最近在追什么": "最近在追什么 / 有什么好看的",
    "上课和作业": "今天几节课 / 作业多不多",
    "家里人和老家": "你家里几口人 / 老家哪儿的",
    "朋友和局": "平时跟谁一块儿玩",
    "音乐口味": "最近单曲循环哪首",
    "电影和番": "有什么番是你会二刷的",
    "手机和电脑": "你手机什么型号 / 电脑是自己攒的吗",
    "钱和花销": "一个月生活费够花吗 / 最近买了什么不后悔的东西",
    "运动": "除了坐着还会动吗 / 会去跑步吗",
    "梦想和以后": "以后想干嘛 / 有没有特别想去的城市",
    "口头禅": "你平时最爱说哪个词",
    "小时候": "小时候最皮干过啥",
    "最近开心的事": "这周有什么让你爽到的事",
    "讨厌的东西": "最烦别人怎么说话",
}

_SENT_SPLIT = re.compile(r"(?<=[。！？!?…~～])")

# 对方这句"没什么可接的"：嗯/哦/好/哈哈/在吗/单个问号…… 这时候才轮得到好奇心出场
_CURIO_DEAD_RE = re.compile(
    r"^[\s。，,.!！?？~～…·、]*"
    r"(嗯+|哦+|喔+|噢+|好+|好吧|行|行吧|可以|哈+|嘿+|呵+|嘿嘿|在吗|在么|在不在|"
    r"没事|随便|都行|你呢|是吗|真的吗|草|艹|6+|"
    r"？+|\?+|\.{2,}|…+)"
    r"[\s。，,.!！?？~～…·、]*$"
)


def _action_lines(who: str, allow=None) -> str:
    """把动作注册表渲染成提示词里的可执行清单（加新动作不用改这里）。

    allow：只列这些动作名（None = 全列）。她主动找人时用得到。
    """
    try:
        import actions as actions_mod

        out = []
        for e in actions_mod.ORDER:
            if allow is not None and e.name not in allow:
                continue
            if e.ready:
                out.append(f"  {e.desc}")
            else:
                out.append(f"  {e.desc}（还没接上，别用）")
        return "\n".join(out)
    except Exception:
        return ("  发消息给某人：[[动作:发消息|某人|正文]]\n"
                "  给当前这个人发表情：[[动作:表情|" + who + "|标签]]")

# 主人说人话的"停一下"（比 /mute 好记，微信里直接说就行）

# 单字「鱼」太容易误触发：钓鱼、摸鱼、鱼香肉丝、鱼缸…… 这些当没听见。
_FISH_COMPOUND = (
    "钓鱼", "摸鱼", "鱼香", "鱼缸", "鱼肉", "鱼丸", "鱼汤", "鱼片", "鱼头", "鱼骨",
    "鱼刺", "鱼竿", "鱼饵", "鱼塘", "飞鱼", "鱼子", "鱼皮", "鱼块", "鱼市", "鱼贩",
)


def _kw_hit(text: str, kws) -> bool:
    """关键词命中判断；单字「鱼」要求不是常见词组的一部分。"""
    if not text:
        return False
    for k in kws:
        if not k:
            continue
        if k != "鱼":
            if k in text:
                return True
            continue
        probe = text
        for bad in _FISH_COMPOUND:
            probe = probe.replace(bad, "")
        if "鱼" in probe:
            return True
    return False


_STOP_RE = re.compile(
    r"^(停|停下|停一下|先停|别聊了|别聊|不要聊了|先别聊|先别回|别回了|暂停|安静|闭嘴|别说话|停手|收手)")
_RESUME_RE = re.compile(r"^(继续|继续聊|好了|可以聊了|恢复|接着聊|继续说)")


def load_behavior(path=None) -> dict:
    conf = dict(BEHAVIOR_DEFAULTS)
    if not path:
        return conf
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            conf.update({k: v for k, v in raw.items() if k in BEHAVIOR_DEFAULTS})
    except Exception:
        pass
    return conf


def split_bubbles(reply: str, conf: dict | None = None, rng=random) -> list[str]:
    """把一条回复切成「真人会连发的那几条」。

    - 她自己用换行分了段：就按段发（最多 bubble_max 段）；
    - 单段但够长、且掷骰子命中：按句末标点拆成 2~3 条；
    - 其余情况原样一条。
    """
    conf = conf or BEHAVIOR_DEFAULTS
    reply = (reply or "").strip()
    if not reply:
        return []
    if not conf.get("bubbles_enabled", True):
        return [reply]
    mx = max(1, int(conf.get("bubble_max", 3) or 3))
    lines = [x.strip() for x in reply.split("\n") if x.strip()]
    if len(lines) >= 2:
        return lines[:mx] if len(lines) <= mx else ["\n".join(lines)]
    if len(reply) < 12 or rng.random() >= float(conf.get("bubble_chance", 0.45)):
        return [reply]
    parts = [p.strip() for p in _SENT_SPLIT.split(reply) if p.strip()]
    if len(parts) < 2:
        return [reply]
    out: list[str] = []
    cur = ""
    for p in parts:
        # 攒够一句半就断一条；已经拆到上限就别再拆，剩下的并进最后一条
        if cur and (len(cur) >= 8 or len(out) + 1 >= mx):
            out.append(cur)
            cur = p
        else:
            cur += p
    if cur:
        out.append(cur)
    # 太短的碎尾巴并回去，别让她拿一条"嗯。"单独占一屏
    fixed: list[str] = []
    for b in out:
        if fixed and len(b) < 5:
            fixed[-1] = fixed[-1] + b
        else:
            fixed.append(b)
    out = fixed
    return out if len(out) >= 2 else [reply]



def pull_actions(reply: str) -> tuple[str, list[tuple[str, str, str]]]:
    """从回复里抠出 [[动作:发消息|目标|内容]] 标记，返回（去掉标记的正文, [(动作, 目标, 内容)])。

    走标记而不是让模型自己跑命令，是因为模型跑一次 shell 动辄 30~120 秒，
    经常把我的 120 秒超时撑爆，然后只能回一句"我卡住了"。标记方式是纯文本，
    秒出，执行交给桥自己（同一进程、同一把界面锁），稳得多。
    """
    acts = [(m.group(1).strip(), m.group(2).strip(), (m.group(3) or "").strip())
            for m in _ACTION_RE.finditer(reply or "")]
    clean = _ACTION_RE.sub("", reply or "").strip()
    return clean, acts


def pull_stickers(reply: str) -> tuple[str, list[str]]:
    """从回复里抠出 [[表情:标签]] 标记，返回（去掉标记的正文, 标签列表）。"""
    tags = [m.group(1).strip() for m in _STICKER_RE.finditer(reply or "")]
    clean = _STICKER_RE.sub("", reply or "").strip()
    return clean, [t for t in tags if t]


def _media_path(text: str) -> str:
    """从 `[表情文件：C:\\...]` 这种占位里抠出本地路径。"""
    m = _MEDIA_RE.search(text or "")
    return m.group(1).strip() if m else ""


def split_text(text: str, limit: int) -> list[str]:
    """按段落/句子把长回复切成多条，避免微信超长消息。"""
    text = text.strip()
    if len(text) <= limit:
        return [text] if text else []
    chunks, buf = [], ""
    for para in text.split("\n"):
        candidate = f"{buf}\n{para}" if buf else para
        if len(candidate) <= limit:
            buf = candidate
            continue
        if buf:
            chunks.append(buf)
        while len(para) > limit:  # 单段仍然超长，硬切
            chunks.append(para[:limit])
            para = para[limit:]
        buf = para
    if buf:
        chunks.append(buf)
    return chunks


class RateLimiter:
    def __init__(self, per_min: int, per_hour: int, global_per_min: int):
        self.per_min = per_min
        self.per_hour = per_hour
        self.global_per_min = global_per_min
        self._hits: dict[str, deque] = defaultdict(deque)
        self._global: deque = deque()
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.time()
        with self._lock:
            hist = self._hits[key]
            while hist and now - hist[0] > 3600:
                hist.popleft()
            in_min = sum(1 for t in hist if now - t <= 60)
            if in_min >= self.per_min or len(hist) >= self.per_hour:
                return False
            while self._global and now - self._global[0] > 60:
                self._global.popleft()
            if len(self._global) >= self.global_per_min:
                return False
            hist.append(now)
            self._global.append(now)
            return True


class Bridge:
    def __init__(self, cfg, brain: Brain, store, sender: Callable[..., bool],
                 self_wxid: str = "", name_of: Optional[Callable[[Msg], str]] = None,
                 guard: Optional[SafetyGuard] = None, persona=None,
                 settings: Optional[dict] = None, settings_dir: str = "",
                 actions: Optional[dict] = None, scheduler=None,
                 send_image: Optional[Callable[..., bool]] = None,
                 send_favorite: Optional[Callable[..., bool]] = None,
                 poke: Optional[Callable[[str], bool]] = None,
                 stickers=None, group_info=None, address_book=None):
        self.cfg = cfg
        self.brain = brain
        self.store = store
        self.send = sender
        self.send_image = send_image
        self.send_favorite = send_favorite
        self.poke = poke
        self.stickers = stickers
        self.group_info = group_info
        self.address_book = address_book
        self.guard = guard
        self.persona = persona
        self.settings = settings
        self.scheduler = scheduler
        self.actions = dict(actions or {})
        self.settings_dir = settings_dir or str(Path(__file__).resolve().parent / "data")
        self.behavior_path = Path(self.settings_dir) / "behavior.json"
        self._behavior = load_behavior(self.behavior_path)
        self._behavior_mtime = self._behavior_mtime_now()
        self.self_wxid = self_wxid
        self.name_of = name_of or (lambda m: m.alias or m.sender)
        self._pool = ThreadPoolExecutor(
            max_workers=cfg.max_workers, thread_name_prefix="brain", initializer=_com_init
        )
        self._locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
        self._inbox: dict[str, list[tuple[Msg, str]]] = {}
        self._busy: set[str] = set()
        self._inbox_lock = threading.Lock()
        # 一次回复拆成几条时，这几条必须连着发完：中间被别人插进来，
        # 第二段就会隔十几秒才到，读起来就"上下不接"了。
        self._seq_lock = threading.RLock()
        # 最近发过的话：防"同一条刷两遍"
        self._recent_out: dict[tuple[str, str], float] = {}
        self._last_group_reply: dict[str, float] = {}
        self._last_group_speaker: dict[str, str] = {}
        self._last_chime: dict[str, float] = {}
        self._pending: dict[str, list[tuple[Msg, str]]] = {}
        self._pending_lock = threading.Lock()
        self.limiter = RateLimiter(cfg.per_contact_per_minute, cfg.per_contact_per_hour,
                                   cfg.global_per_minute)
        self._limiter_sig = (cfg.per_contact_per_minute, cfg.per_contact_per_hour,
                             cfg.global_per_minute)
        # 静音状态要落盘：桥崩了/重启之后不能自己又开始跟人聊。
        self._mute_path = Path(self.settings_dir) / "mute.json"
        self.mood = mood_mod.Mood(Path(self.settings_dir) / "mood.json")
        self._muted = self._load_mute()
        if self._muted:
            log.warning("启动时处于静音（只有主人能说话），解除请发“继续聊”")
        # 自动回复熔断：对方像机器人一样刷同一条消息时，单独静音这个会话一阵
        self._mutechats_path = Path(self.settings_dir) / "mute_chats.json"
        self._mutechats_mtime = 0.0
        self._mutechats = self._load_mute_chats()
        self._recent_in: dict[str, list[tuple[str, float]]] = {}
        self._stop = threading.Event()
        self._last_notice = 0.0
        self.last_msg_ts = 0.0
        # 好奇心的三个小账本：聊到第几轮、上次问到第几轮、最近问过哪些方向
        self._curio_turn: dict[str, int] = {}
        self._curio_last: dict[str, int] = {}
        self._curio_asked: dict[str, list[str]] = {}
        # 对话收尾追问：对方最后一次说话、她最后一次说话、这轮是否已经追问过
        self._conv_in: dict[str, float] = {}
        self._conv_out: dict[str, float] = {}
        self._conv_pinged: dict[str, bool] = {}
        self._conv_last_ping: dict[str, float] = {}
        self._conv_last_activity: dict[str, float] = {}  # 谁最后说话的时间，用来判断「聊完了」

    # ---------------- 对外入口 ----------------
    def submit(self, msg: Msg, force: bool = False) -> None:
        self.last_msg_ts = time.time()
        if self._stop.is_set():
            return
        if msg.from_self:
            # 自己发的消息也会被补收捞回来：记上 seen，否则每分钟重放一遍，
            # 每放一遍还会顺手发个表情（就是"一直抢鼠标去搜 YY"的元凶）。
            if msg.msgid:
                self.store.mark_seen(msg.msgid)
            return
        if not msg.text.strip() and msg.kind == "text":
            if msg.msgid:
                self.store.mark_seen(msg.msgid)
            return
        # force=True 用于「补答」：这条消息早就记过账了，只是当时没回出去
        if not force and self.store.is_duplicate(msg.msgid):
            return
        if not force and self.store.is_seen(msg.msgid):
            return

        key = msg.room if msg.is_group else msg.sender
        if not msg.from_self and not msg.is_group:
            # 对方重新开口 = 新一轮对话，追问额度重置
            self._conv_in[key] = time.time()
            self._conv_last_activity[key] = time.time()
            self._conv_pinged[key] = False

        # 电脑关机/桥没跑时收到的消息也会补收回来：太旧的只进数据库当上下文，
        # 不回——否则一开机就对着十几个小时前的话挨个回，像发疯。
        if msg.stale and msg.ts:
            age_min = (time.time() - msg.ts) / 60.0
            limit_min = float(self._behavior_conf().get("backfill_reply_minutes", 240))
            if age_min > limit_min:
                self.store.log(msg.msgid, msg.sender, msg.room, "in", msg.text, 0.0)
                if msg.msgid:
                    self.store.mark_seen(msg.msgid)
                log.info("迟到消息只入上下文不回（%.0f 分钟前）：%s / %s",
                         age_min, msg.alias or key, (msg.text or "")[:24])
                return

        touch_alias = msg.alias
        if msg.is_group and self.group_info is not None:
            try:
                touch_alias = (self.group_info(msg.room) or {}).get("name") or msg.alias
            except Exception:
                touch_alias = msg.alias
        self.store.touch(key, touch_alias, msg.is_group)
        # 新来的人先给他建一份笔记：以后称呼、印象都往这里长，不用等她记得建。
        try:
            if not msg.is_group and not self._is_admin(msg.sender):
                np_ = _notes_path(msg.sender)
                if not np_.exists():
                    who0 = msg.alias or msg.sender
                    np_.parent.mkdir(parents=True, exist_ok=True)
                    np_.write_text(
                        f"# {who0}\n\n称呼：你（刚认识，还没叫他名字）\n"
                        f"印象：（还没聊开，先记着）\n\n## 待了解\n作息、口味、在忙什么\n",
                        encoding="utf-8")
                    log.info("给新联系人建了笔记：%s", np_.name)
        except Exception as e:
            log.debug("自动建笔记失败：%r", e)

        text = self._extract(msg)
        if text is None:
            # 记住"这条我看过了但不该回"，补收就不会每 60 秒再把它拖出来重下一次图
            if msg.msgid:
                self.store.mark_seen(msg.msgid)
            # 不接也要落库：群上下文是从 messages 表读的，不记就等于她根本没看见
            # 这句话（实测 11:52 朋友那句"你也来一起"因此丢失）。
            try:
                self.store.log(msg.msgid, msg.sender, msg.room, "in",
                               msg.text or f"[{msg.kind}]", 0.0)
            except Exception:
                pass
            log.debug("忽略（不入回复队列，但记入上下文）：%s / %s", msg.sender,
                      (msg.text or msg.kind)[:30])
            return

        self._refresh_mute()

        if text.startswith("/"):
            if self._is_admin(msg.sender):
                self._command(text, key, msg)
            else:
                log.debug("非主人发的命令，忽略：%s", text[:20])
            return

        # 主人用大白话说"停下/别聊了"也算指令：别自作主张继续跟别人聊。
        if self._is_admin(msg.sender):
            low = text.strip().rstrip("。！!~～ ")
            if _STOP_RE.match(low):
                self._set_muted(True)
                log.warning("主人让停：进入完全静默（连回执都不发）")
                return
            if _RESUME_RE.match(low):
                self._set_muted(False)
                log.info("主人让继续：解除静默（静默执行，不回执）")
                return

        if self._muted and not self._is_admin(msg.sender):
            # 静音只闭嘴、不闭眼：照常记账（写进 messages 当上下文），只是不回、
            # 也不做任何抢前台的活。这样解除静音后她还记得这段时间对方说过什么，
            # 而不是像以前那样整段对话凭空消失。
            try:
                if msg.msgid:
                    self.store.mark_seen(msg.msgid)
                self.store.log(msg.msgid, msg.sender, msg.room, "in", text)
            except Exception:
                pass
            log.debug("静音中只记账不回：%s / %s", key, (text or "")[:30])
            return
        if self._in_quiet_hours() and not self._is_admin(msg.sender):
            log.info("免打扰时段，不回复 %s", key)
            return
        if self.guard:
            ok, why = self.guard.allow_incoming(key, msg.is_group)
            if not ok and why == "cooling-down":
                # 冷却的起因本来就是"发送失败连着好几次"，这时候再把消息丢掉，
                # 用户看到的就是彻底失联。所以冷却只记日志，不拦消息，照常回。
                log.warning("冷却期（%s）但仍照常处理：%s", why, key)
                ok = True
            if not ok:
                log.info("防封闸门拦住 %s：%s", key, why)
                # 被自家闸门拦住时"完全不说话"最气人：主人问一句就告诉她为什么、多久。
                if self._is_admin(msg.sender) and time.time() - self._last_notice > 1800:
                    self._last_notice = time.time()
                    to = msg.room if msg.is_group else msg.sender
                    self._safe_send(to, f"我到上限了，先安静一会儿（{_BLOCK_CN.get(why, why)}），"
                                        f"过一会儿再聊。")
                # 关键：拦住就是拦住，不能再往下走去回复（以前这里漏了 return，
                # 于是"说完到上限了"紧接着又发了一条，看着像左脑驳右脑）。
                # 以前这里会把她"全天静音"，等于悄悄停止服务、而且只能找主人手动 /继续。
                # 现在改成：只按防封层的冷却时间暂停回复（guard 自己会拦），不再全局静音，
                # 冷却到点自动恢复；同时通知主人一次，让他知道发生了什么。
                log.warning("触发 %s：只暂停冷却，不全局静音", why)
                return
        # 主人自己不会被"每人每分钟 N 条"卡住（他聊得快是常态）；只保留一个很高的兜底，
        # 防止程序出错时真的把微信刷爆。
        self._refresh_limits()
        if not self._is_admin(msg.sender) and not self.limiter.allow(key):
            log.warning("限流：%s", key)
            return

        self.store.log(msg.msgid, msg.sender, msg.room, "in", text)
        log.info("收到 %s 的 %s：%s", key, msg.kind, (text or "")[:40])
        # 对方连发好几条时，别一条一条各回一遍：先攒 2 秒，合成一次再回。
        # 不合并的话，她答的是上一条、屏幕上已经冒出三条新的，看着就是"语无伦次"。
        window = float(self._behavior_conf().get("merge_window_seconds", 2.0) or 0)
        with self._pending_lock:
            buf = self._pending.setdefault(key, [])
            buf.append((msg, text))
            first = len(buf) == 1
        if window <= 0:
            with self._pending_lock:
                items = self._pending.pop(key, [])
            self._dispatch(key, items)
            return
        if first:
            t = threading.Timer(window, self._flush_pending, args=(key,))
            t.daemon = True
            t.start()

    def _flush_pending(self, key: str) -> None:
        with self._pending_lock:
            items = self._pending.pop(key, [])
        if items:
            self._dispatch(key, items)

    def _dispatch(self, key: str, items: list[tuple[Msg, str]]) -> None:
        if not items:
            return
        last = items[-1][0]
        # 主人刚叫停：攒着的这批直接丢掉，别在静音之后还冒出来
        if self._muted:
            log.info("静音中，丢掉 %s 的 %d 条待回消息", key, len(items))
            return
        # 对方像自动回复一样复读同一句话：静音这个会话，别陪着对下去
        if self.chat_muted(key):
            log.info("熔断静音中，这条不回：%s", key)
            return
        if self._repeat_guard(key, "\n".join(t for _, t in items if t)):
            log.warning("熔断：对方在复读同一句话，静音 1 小时 → %s", key)
            return
        text = "\n".join(t for _, t in items if t).strip()
        if len(items) > 1:
            log.info("合并了 %s 的 %d 条连发消息，一起回", key, len(items))
            text = text + f"\n（对方刚才连着发了 {len(items)} 条，合成一段看，一次回完就行。）"
        self._merge_count = len(items)
        pos = self._pool.submit(self._run, last, key, text)
        pos.add_done_callback(lambda f: f.exception() and log.error("处理异常: %r", f.exception()))

    def shutdown(self) -> None:
        self._stop.set()
        self._pool.shutdown(wait=False)

    # ---------------- 内部 ----------------
    def _extract(self, msg: Msg) -> Optional[str]:
        """判断要不要回，返回净化后的文本。"""
        r = self.cfg
        if msg.is_group:
            # 被 @ 或者叫了名字 = 明确在跟她说话：立刻重置"接着聊"窗口，
            # 之后 3 分钟内群里谁说话她都当是在跟她说。
            _kws = list(self._behavior_conf().get("group_keywords") or [])
            _hint = (msg.text or "")[:60]
            _named = msg.at_me or _kw_hit(_hint, _kws) or any(
                k and k in (msg.text or "") for k in [getattr(self.cfg, "bot_name", "")])
            if _named:
                self._last_group_reply[msg.room or ""] = time.time()

            if msg.room in self.cfg.deny_groups:
                return None
            if self.cfg.allow_groups != ["*"] and msg.room not in self.cfg.allow_groups:
                return None
            if r.group == "off":
                return None
            text = msg.text
            hit_at = msg.at_me
            bconf = self._behavior_conf()
            gmode = bconf.get("group_mode") or r.group
            kws = list(bconf.get("group_keywords") or [])
            for _n in [getattr(self.cfg, "bot_name", "")] + ['小鲸鱼', '大肥鱼', '大傻鱼', '肥鱼', '傻鱼', '鱼鱼']:
                if _n and _n not in kws:
                    kws.append(_n)
            hit_kw = _kw_hit(text, kws)
            if gmode == "off":
                return None
            if gmode == "at" and not hit_at:
                return None
            if gmode == "keyword" and not hit_kw:
                return None
            if gmode == "smart":
                # 群里发的表情包/图片本身没有文字：只看它是不是"接着她在说话"。
                # 她刚在这个群说过话（thread 窗口内），对方甩个表情就是冲她来的，得接。
                if msg.kind in ("sticker", "image"):
                    room = msg.room or ""
                    last = self._last_group_reply.get(room, 0)
                    thread_min = int(bconf.get("group_thread_minutes", 3) or 0)
                    if thread_min > 0 and time.time() - last <= thread_min * 60:
                        return text.strip() or "[表情]"
                # 四种情况开口：被 @ / 明显在跟她说话 / 正在跟她接着聊 / 小概率插一句
                if not hit_at and not self._addressed_to_me(text, msg, kws):
                    if not self._in_group_thread(msg) and not self._chime_in(msg):
                        return None
            # 去掉开头的 "@名字"
            for marker in ("@",):
                if text.startswith(marker):
                    parts = text.split("\u2005", 1)  # 微信 @ 后面的特殊空格
                    if len(parts) == 2:
                        text = parts[1]
                    else:
                        text = text.split(" ", 1)[1] if " " in text else text
            return text.strip() or None

        if msg.sender in self.cfg.deny_contacts:
            return None
        mode = (self._behavior_conf().get("private_mode") or "").strip() or r.private
        if mode == "off":
            return None
        if mode == "prefix" and msg.kind == "text":
            p = (self._behavior_conf().get("private_prefix") or r.private_prefix or "").strip()
            if not p or not msg.text.startswith(p):
                return None
            return msg.text[len(p):].strip() or None
        return msg.text.strip()

    def _addressed_to_me(self, text: str, msg: Msg, kws=None) -> bool:
        """群里这句话是不是在对她说（没 @ 也能看出来）。"""
        if not text:
            return False
        t = text.strip()
        names = [self.cfg.bot_name] + list(kws if kws is not None else (self.cfg.group_keywords or []))
        if _kw_hit(t, names):
            return True
        # 「喂/哎/那个谁」开头的招呼，或者直接问到她头上
        for lead in ("喂", "哎", "那个谁", "小鱼", "助手", "机器人"):
            if t.startswith(lead):
                return True
        for q in ("你在吗", "你在干啥", "你怎么看", "你觉得呢", "你说呢", "你俩", "你来", "你会"):
            if q in t:
                return True
        return False

    def _in_group_thread(self, msg: Msg) -> bool:
        """刚在群里跟她聊过，对方这句话就是接着跟她说的（不用再 @）。0 = 关闭。"""
        conf = self._behavior_conf()
        window = float(conf.get("group_thread_minutes", 0) or 0)
        if window <= 0:
            return False
        room = msg.room or msg.sender
        last = self._last_group_reply.get(room, 0.0)
        if not last or time.time() - last > window * 60:
            return False
        t = (msg.text or "").strip()
        if not t:
            return False
        speaker = self._last_group_speaker.get(room)
        if speaker and msg.sender == speaker:
            log.info("群里接着聊：%s 又说了一句 → %s", speaker, t[:20])
            return True
        # 不是同一个人，但很短又是在回应（"那早上好"这种）
        if len(t) <= 12 and not t.startswith("@"):
            log.info("群里短跟进，当成在跟她说话：%s", t[:20])
            return True
        return False

    def _chime_in(self, msg: Msg) -> bool:
        """没人叫她时，小概率插一句（同群有冷却，免得像刷屏）。"""
        conf = self._behavior_conf()
        chance = float(conf.get("group_chime_chance", 0.05) or 0)
        if chance <= 0 or msg.kind != "text" or len(msg.text.strip()) < 4:
            return False
        cooldown = float(conf.get("group_chime_cooldown_minutes", 12) or 12) * 60
        room = msg.room or msg.sender
        now = time.time()
        # 上一次"任何回复"和"上一次主动插话"都要满足冷却，
        # 否则连发一串"1"会被她一条条接住，看起来像刷屏。
        last = max(self._last_group_reply.get(room, 0.0), self._last_chime.get(room, 0.0))
        if now - last < cooldown:
            return False
        if random.random() >= chance:
            return False
        self._last_chime[room] = now
        log.info("群里没人叫她，随机接一句（%s）", room)
        return True

    def muted_now(self) -> bool:
        """静音中？给适配器用：静音时除主人外，连消息都不读。"""
        try:
            self._refresh_mute()
        except Exception:
            pass
        return bool(self._muted)

    def _is_admin(self, wxid: str) -> bool:
        return bool(self.cfg.admin_wxids) and wxid in self.cfg.admin_wxids

    def _refresh_limits(self) -> None:
        """面板里改了限流，下一次判断就生效（不重启）。"""
        if self.limiter is None:
            return
        c = self._behavior_conf()
        try:
            sig = (int(c.get("rate_per_min", 20)), int(c.get("rate_per_hour", 240)),
                   int(c.get("rate_global_min", 90)))
        except Exception:
            return
        if sig != getattr(self, "_limiter_sig", None):
            self.limiter = RateLimiter(*sig)
            self._limiter_sig = sig
            log.info("限流已更新：每人 %s/分、%s/时；全号 %s/分", *sig)

    def _in_quiet_hours(self) -> bool:
        # 免打扰时段也放进 behavior.json，面板改完下一次判断就生效
        c = self._behavior_conf()
        enabled = c.get("quiet_enabled")
        if enabled is None:
            q = self.cfg.quiet_hours
            if not q.get("enabled"):
                return False
            s, e = q.get("start", "00:00"), q.get("end", "00:00")
        else:
            if not enabled:
                return False
            s, e = c.get("quiet_start", "23:30"), c.get("quiet_end", "08:00")
        now = datetime.now().strftime("%H:%M")
        return (s <= now or now < e) if s > e else (s <= now < e)

    def _maybe_auto_sticker(self, target: str, key: str, conf: dict) -> None:
        """偶尔自己补一张可爱的表情，像真人聊天顺手甩个图。"""
        try:
            chance = float(conf.get("auto_sticker_chance", 0.0) or 0.0)
            if chance <= 0 or random.random() >= chance:
                return
            tags_cfg = conf.get("auto_sticker_tags") or ["可爱", "开心", "害羞", "收到"]
            have = self.stickers.tags()
            usable = [t for t in tags_cfg if have.get(t)]
            if not usable:
                return
            tag = random.choice(usable)
            time.sleep(random.uniform(0.1, 0.3))
            self._safe_send_sticker(target, tag)
        except Exception as e:
            log.debug("自动表情跳过：%s", e)

    def _behavior_mtime_now(self) -> float:
        try:
            return self.behavior_path.stat().st_mtime
        except OSError:
            return 0.0

    def _scrub_note(self, text: str, who: str) -> str:
        """把笔记里提到别人名字的行删掉。

        这类行只可能来自两种情况：早期按名字存笔记时混进来的，或者她自己
        写笔记时手滑。留着会让 A 的印象出现在 B 的对话里。
        """
        if not text:
            return text
        try:
            others = {a for a in (self._alias_names() or set()) if a and a != who}
        except Exception:
            return text
        if not others:
            return text
        keep = []
        for line in text.splitlines():
            if any(o in line for o in others):
                continue
            keep.append(line)
        return "\n".join(keep).strip()

    def _alias_names(self) -> set:
        """所有联系人的备注名（含大号名），用来识别"这行在说别人"。"""
        now = time.time()
        if getattr(self, "_alias_cache", None) and now - getattr(self, "_alias_ts", 0) < 600:
            return self._alias_cache
        names = set()
        try:
            rows = self.store.db.execute(
                "select alias from contacts where alias is not null and alias != ''")
            names = {r[0] for r in rows}
        except Exception:
            names = set()
        self._alias_cache = names
        self._alias_ts = now
        return names

    def _behavior_conf(self) -> dict:
        """面板改了 behavior.json / settings.json 就热加载，不用重启桥。"""
        self._refresh_rhythm()
        mt = self._behavior_mtime_now()
        if mt != self._behavior_mtime:
            self._behavior_mtime = mt
            self._behavior = load_behavior(self.behavior_path)
            log.info("连发设置已热加载：%s", self._behavior)
        return self._behavior

    def _refresh_rhythm(self) -> None:
        """面板里改了「说话节奏」，下一次判断就生效（不用重启）。"""
        try:
            import runtime

            path = Path(self.settings_dir) / "settings.json"
            mt = path.stat().st_mtime if path.exists() else 0.0
            if mt == getattr(self, "_settings_mtime", None):
                return
            self._settings_mtime = mt
            st = runtime.load_settings(Path(self.settings_dir)) if self.settings_dir else {}
            name = (st or {}).get("rhythm_profile") or getattr(self.cfg, "rhythm_profile", "natural")
            if name == getattr(self, "_applied_rhythm", None):
                return
            note = runtime.apply_profile(name, self.guard)
            self._applied_rhythm = name
            self.cfg.rhythm_profile = name
            if self.settings is not None:
                self.settings["rhythm_profile"] = name
            log.info("节奏已热加载：%s", note)
        except Exception as e:  # 面板改坏也不该让消息处理挂掉
            log.warning("节奏热加载失败：%r", e)


    def _mood_on(self) -> bool:
        """好感/心情是可选的玩法，默认关闭（关掉时提示词、表情闸门、/状态 全部不参与）。"""
        try:
            return bool(self._behavior_conf().get("mood_enabled", False))
        except Exception:
            return False

    def _curiosity_block(self, ident: str, who: str, note: str, is_group: bool,
                         last_text: str = "", gap_seconds: float | None = None,
                         prev_text: str = "") -> str:
        """偶尔给她一个"想了解对方"的推动：一次只问一个具体的小问题。

        默认（curiosity_follow_topic=True）**只在当前话题里往下追问细节**，
        不再从话题清单里挑一个新方向硬插进来——以前就是这样，明明在聊冰水，
        下一句突然跳到"有什么番你会二刷"。
        """
        conf = self._behavior_conf()
        if not conf.get("curiosity_enabled", True) or is_group:
            return ""
        turn = self._curio_turn.get(ident, 0) + 1
        self._curio_turn[ident] = turn
        cd = int(conf.get("curiosity_cooldown_turns", 4) or 4)
        if turn - self._curio_last.get(ident, 0) < max(1, cd):
            return ""
        chance = float(conf.get("curiosity_chance", 0.3) or 0.0)
        if random.random() > chance:
            return ""

        # 冷场闸：热聊的时候不插问句，只有他这话很短、或者已经冷下来了才问。
        text = (last_text or "").strip()
        short_chars = int(conf.get("curiosity_short_chars", 6) or 6)
        lull_seconds = float(conf.get("curiosity_lull_seconds", 90) or 90)
        dead = bool(text) and bool(_CURIO_DEAD_RE.match(text))
        short = bool(text) and len(text) <= short_chars
        if conf.get("curiosity_lull_only", True):
            quiet = (gap_seconds is not None and gap_seconds >= lull_seconds)
            if not (short or dead or quiet):
                log.debug("好奇心：这轮跳过（热聊中，他这话 %d 字、间隔 %s 秒）",
                          len(text), "?" if gap_seconds is None else int(gap_seconds))
                return ""

        follow = bool(conf.get("curiosity_follow_topic", True))
        no_switch = bool(conf.get("curiosity_no_switch", True))
        switch_ban = ("\n**禁止**用「对了」「话说」「说起来」「说真的」「顺便问一下」这类词把话题拐走——"
                      "问的必须是刚才正在聊的那件事。" if no_switch else "")

        def _finish(payload: str) -> str:
            self._curio_last[ident] = turn
            self._curio_hits = getattr(self, "_curio_hits", 0) + 1
            return payload

        if follow:
            # 他这条只有"嗯"这种，追问「嗯」毫无意义：要退回去顺着上一条的内容问。
            base = text if (len(text) >= 4 and not dead) else (prev_text or "").strip()
            if not base:
                return ""
            probe = re.sub(r"\s+", " ", base)[:24]
            lead = ("他刚说的是「" + probe + "」" if base == text
                    else "他这句只回了「" + (text or "") + "」，但上面那条是「" + probe + "」")
            log.info("好奇心：这轮顺着他这句话追问（对象 %s，第 %d 次）",
                     who or ident, getattr(self, "_curio_hits", 0) + 1)
            return _finish(
                "\n[顺着他这句话再往下问一层：" + lead + "，"
                "从这句话里挑一个还没交代清楚的细节追问一句"
                "（谁的、哪个、什么时候、为什么、后来呢、真的假的）。"
                "问的必须是这句话里已经出现过的东西，**不要新开一个方向**；"
                "想不出跟这句有关的问法，这轮就别问，只接他的话。"
                + switch_ban +
                "\n一次只问一个，别连着问、别问成查户口；他含糊过去或者不想说就别追；"
                "他答了就把答案记进他的笔记。**这条回复里要真的问出来**，别只在心里记。]"
            )

        known = note or ""
        asked = self._curio_asked.setdefault(ident, [])
        cands = [name for name, kws in _CURIOUS_TOPICS
                 if not any(k.lower() in known.lower() for k in kws)
                 and name not in asked[-3:]
                 and ("问过：" + name) not in known]
        if not cands:
            return ""
        topic = random.choice(cands)
        asked.append(topic)
        # 记进笔记：以后（包括重启后）都不再重复问同一个方向
        try:
            import notes as notes_mod
            np = self._notes_path(ident)
            if np.exists():
                body = np.read_text(encoding="utf-8")
                line = "- 问过：" + topic + "（" + time.strftime("%m-%d") + "）\n"
                if ("问过：" + topic) not in body:
                    if "## 已经问过" not in body:
                        body = body.rstrip() + "\n\n## 已经问过\n"
                    body = body.rstrip() + "\n" + line
                    np.write_text(body, encoding="utf-8")
        except Exception as e:
            log.debug("记录已问话题失败：%r", e)
        self._curio_hits = getattr(self, "_curio_hits", 0) + 1
        log.info("好奇心：这轮注入「%s」（对象 %s，第 %d 次）", topic, who or ident, self._curio_hits)
        hint = _CURIO_ASK_HINT.get(topic, "")
        return _finish(
            "\n[你对他还有点好奇：" + topic + "。如果他这话说得短、或者正好聊到相关的茬，"
            "就顺口问一句具体的" + ("（比如「" + hint + "」）" if hint else "") +
            "。一次只问一个，别连着问、别问成查户口；他含糊过去或者不想说就别追；"
            "他答了就把答案记进他的笔记。" + switch_ban +
            "**这条回复里要真的问出来**，别只在心里记。]"
        )


    @staticmethod
    def _fmt_when(ts: float) -> str:
        """把时间戳变成"几点/昨天几点/几月几号"这种大致时间，人一眼能懂。"""
        if not ts:
            return ""
        try:
            t = time.localtime(float(ts))
        except Exception:
            return ""
        now = time.localtime()
        days = (time.mktime(time.strptime(time.strftime("%Y-%m-%d", now), "%Y-%m-%d"))
                - time.mktime(time.strptime(time.strftime("%Y-%m-%d", t), "%Y-%m-%d"))) / 86400
        hm = time.strftime("%H:%M", t)
        if days <= 0:
            return hm
        if days == 1:
            return "昨天 " + hm
        if days < 7:
            return f"{int(days)}天前 {hm}"
        return time.strftime("%m-%d %H:%M", t)

    def _group_tail(self, room: str, limit: int = 40) -> list[str]:
        """群里最近几十条（带大致时间 + 说话人），她回之前得先看到这些。"""
        out: list[str] = []
        try:
            rows = self.store.db.execute(
                "SELECT sender, direction, content, ts FROM messages WHERE room=? "
                "ORDER BY id DESC LIMIT ?", (room, limit)).fetchall()
        except Exception:
            return out
        for sender, direction, content, ts in reversed(rows):
            c = re.sub(r"^wxid_[A-Za-z0-9_-]+[:：]\s*", "", (content or "").strip())
            c = c.replace("\n", " ").strip()
            # 图片/表情只留个标记，别让她对着长路径发呆
            if "[表情" in c:
                c = "[表情]"
            elif "[图片" in c or "[图" in c:
                c = "[图]"
            elif "[语音" in c:
                c = "[语音]"
            if not c:
                continue
            if direction == "out":
                who = "我"
            else:
                who = sender or "某人"
                try:
                    row = self.store.db.execute(
                        "SELECT alias FROM contacts WHERE wxid=?", (sender,)).fetchone()
                    if row and row[0]:
                        who = row[0]
                except Exception:
                    pass
                if (not who or who.startswith("wxid_")) and self.group_info is not None:
                    # 联系人表里没有这个人（刚进群/没加好友），用群成员昵称
                    try:
                        for mem in (self.group_info(room) or {}).get("members", []):
                            if mem.get("wxid") == sender and mem.get("name"):
                                who = mem["name"]
                                break
                    except Exception:
                        pass
            when = self._fmt_when(ts)
            out.append(f"[{when}] {who}：{c[:70]}" if when else f"{who}：{c[:70]}")
        return out

    def _run(self, msg: Msg, key: str, text: str) -> None:
        """进来先排队：同一个会话在回的时候，后面来的都攒进 inbox，
        等这一轮说完，把攒下的所有话合成一条一起回（一条也不丢）。"""
        with self._inbox_lock:
            self._inbox.setdefault(key, []).append((msg, text))
            if key in self._busy:
                return
            self._busy.add(key)
        while True:
            with self._inbox_lock:
                items = self._inbox.pop(key, [])
                if not items:
                    self._busy.discard(key)
                    if not self._inbox.get(key):
                        return
                    self._busy.add(key)
                    continue
            try:
                self._answer(key, items)
            except Exception:
                log.exception("处理 %s 的消息出错", key)
                return

    def _tail_blocks(self, key: str, ident: str, who: str, role: str, note: str,
                     allow_acts=None, hint: str = "") -> str:
        """回复和主动开场共用的后半段：对象/称呼/动作/连发/表情库/梗库/最近 50 条。

        这样她主动找人时看到的世界跟回话时是同一个，不会退化成模板。
        """
        out = (
            f"\n[当前聊天对象：{who}（{role}）。"
            "别把「主人」这个词用在他身上；对他用「你」。"
            "\n称呼跟着你对这个人的印象走：刚认识只叫「你」；聊熟了叫名字；"
            "要是他本人说过「叫我X」、或者你俩斗嘴斗出来的外号，才用那个。"
            "对方表现出不喜欢，立刻退回「你」，别硬叫。改完称呼顺手更新他的笔记。"
            f"\n这个人的笔记文件：{_notes_path(ident)}（没有就新建，别删别人的；改备注不影响这个文件）"
            + (f"\n你之前给他记的印象（按这个称呼和相处方式，不要念出来）：\n{note}" if note
               else "\n你还没给他记过笔记，先只叫「你」。")
            + ("\n要动手（按下面这几条）时，**不要自己跑命令**，"
               "在回复最后另起一行写标记，桥会立刻替你执行：" if allow_acts is not None
               else "\n要动手（替主人给别人发消息 / 发表情 / 戳一戳 / 同意好友申请）时，"
                    "**不要自己跑命令**，在回复最后另起一行写标记，桥会立刻替你执行：")
            + f"\n{_action_lines(who, allow_acts)}"
            "\n标记那一行不会发给对方；你想对对方说的话照常写在标记外面。]"
            "\n[连发：想一口气说两三句就**换行分条**写，每行一句短话（10 字上下），最多 3 行，"
            "桥会隔一两秒依次发出去，像真人打字。大部分时候一句就够，别每次都拆。]"
        )
        if self.stickers is not None:
            # 表情库全是 DeepSeek娘（蓝发大肥鱼），跟头像同一个角色。
            out += ("\n[表情库：每个标签是一组同情绪的表情，选跟此刻气氛最贴的那一档，"
                    "单独写一行 [[表情:标签]]。\n"
                    "  · 开心/被逗到/想笑 → [[表情:开心]]\n"
                    "  · 生气/被气到/想凶一下 → [[表情:生气]]\n"
                    "  · 无语/看不下去/离谱 → [[表情:无语]]\n"
                    "  · 疑惑/不懂/你逗我 → [[表情:疑惑]]\n"
                    "  · 害羞/被夸到/软一下 → [[表情:害羞]]\n"
                    "  · 委屈/被欺负/想撒娇 → [[表情:委屈]]\n"
                    "  · 摸鱼/困了/不想干活/躺平 → [[表情:摸鱼]]\n"
                    "  · 得意/被夸到心里/小嚣张 → [[表情:得意]]\n"
                    "  当时能用的标签：" + "、".join(sorted(self.stickers.tags().keys())[:16]) +
                    "。表情是调味，十条里最多两三条带，别每条都配。]")
        # 梗库整体关闭；只保留一条对暗号（用户指定）
        if self._behavior_conf().get("memes_enabled", False):
            memes = _read_memes()
            if memes:
                out += ("\n[梗库：\n" + memes + "]")
        else:
            out += ("\n[暗号：对方发 kskbl 或 kskbk（康神开播了，意思是\"好戏开场了\"）就回 zdjd（真的假的）；"
                    "对方没发缩写就正常说话，别主动甩字母，也别解释这个梗。]")
        # 最近 50 条对话（双方都算）：每行标清「几点 谁：说了什么」，
        # 她得知道哪句是自己说的、哪句是对方说的、隔了多久。
        # 图/表情/语音只留短标记；同一个人的连续表情合并成「[表情]×N」，别把篇幅吃光。
        recent: list[str] = []
        try:
            rows = self.store.db.execute(
                "SELECT direction, content, ts FROM messages WHERE (room=? OR (sender=? AND (room IS NULL OR room=''))) "
                "ORDER BY id DESC LIMIT 50", (key, key)).fetchall()
            lines = []
            day = ""
            for d, c0, ts in reversed(rows):
                if not c0:
                    continue
                who2 = "我" if d == "out" else "对方"
                raw = (c0 or "").replace("\n", " ")
                # 群消息落库时正文带「wxid_xxx:」前缀：私聊上下文里直接跳过，
                # 否则群里的话会冒充成"对方跟你说的"。
                if not key.endswith("@chatroom") and re.match(r"^wxid_[A-Za-z0-9_-]+[:：]", raw):
                    continue
                c1 = re.sub(r"^wxid_[A-Za-z0-9_-]+[:：]\s*", "", raw).strip()
                if not c1:
                    continue
                # 媒体类：压成两三个字的标记（她的规矩：表情别占篇幅）
                media = ""
                if "文件：" in c1 or c1.startswith("[对方发来"):
                    if "表情" in c1:
                        media = "表情"
                    elif "语音" in c1:
                        media = "语音"
                    else:
                        media = "图"
                elif c1.startswith("[") and len(c1) > 40:
                    # 其它系统占位（拍一拍等）只留开头
                    c1 = c1[:6] + "]"
                stamp = time.strftime("%H:%M", time.localtime(ts or 0))
                d0 = time.strftime("%m-%d", time.localtime(ts or 0))
                if d0 != day:
                    day = d0
                    lines.append(f"—— {d0} ——")
                if media:
                    m = (re.match(r"^(\d\d:\d\d) (我|对方)：\[(表情|图|语音)\](?:×(\d+))?$", lines[-1])
                         if lines else None)
                    if m and m.group(2) == who2:
                        lines[-1] = f"{m.group(1)} {who2}：[{m.group(3)}]×{int(m.group(4) or 1) + 1}"
                    else:
                        lines.append(f"{stamp} {who2}：[{media}]")
                    continue
                lines.append(f"{stamp} {who2}：{_clip(c1, 40)}")
                recent.append(c1)
            # 总字数上限 1300（加上时间戳/合并后，正好塞下最近这几十条），超了从最旧的丢
            while sum(len(x) for x in lines) > 1300 and len(lines) > 10:
                lines.pop(0)
            if lines:
                out += ("\n[最近 50 条对话（从旧到新，每行是「时间 谁：内容」；"
                        "**别重复你说过的话，也别把旧的问答/旧梗捡起来当新话题**）：\n"
                        + "\n".join(lines) + "]")
        except Exception as e:
            log.debug("读最近对话失败：%r", e)
        # 聊到"玩点什么"才给规则，平时不占提示词字数
        try:
            if _GAME_HINT.search(" ".join(recent[-8:]) + " " + (hint or "")):
                games = _read_games()
                if games:
                    out += ("\n[你会玩的小游戏（一次只玩一个；规则你心里有数就行，别把规则念一遍，"
                            "用一句话开个头就开玩；他要是不知道，你用一句话说清）：\n"
                            + games + "]")
        except Exception as e:
            log.debug("读游戏库失败：%r", e)
        return out

    def compose_proactive(self, key: str, who: str, hint: str = "",
                          allow_acts=None) -> str:
        """她自己主动找人说话时的完整提示词（跟回话同级，只是没有"对方说了什么"）。

        key 是 wxid/群 id，who 是通讯录里的名字。
        """
        ident = key
        role = "是主人本人" if self._is_admin(ident) else "是主人的朋友，不是主人"
        note = "" if self._is_admin(ident) else self._scrub_note(
            _read_note(ident, who), who)
        prompt = _with_clock(hint)
        # 一句话都没跟他说过：别装熟。0 历史的人只会被主动找一次、不追发。
        try:
            n_msg = self.store.db.execute(
                "SELECT count(*) FROM messages WHERE (room=? OR (sender=? AND (room IS NULL OR room='')))",
                (key, key)).fetchone()[0]
        except Exception:
            n_msg = 0
        if int(n_msg or 0) == 0:
            prompt += ("\n[你跟这个人**一句话都没说过**：别装熟、别说「突然想起你」「想你了」这种、"
                       "别问私事；就找个具体的由头起一句——问一句、认个脸、或者提一件你觉得有意思的小事；"
                       "别描述你屋里、你机器什么样；语气客气点，一两句就够，说完不用非等他回。]")
        # 主动开场的老毛病：翻旧账、复读自己说过的话（"那句「大了」我记着呢"就是这么来的）。
        prompt += ("\n[这是你主动找他，不是回他的话：只接最近几轮真正聊到的事，"
                   "**别翻旧账**——「记不记得」「第一句话」「上次你说」这类一律别提；"
                   "别复述你自己说过的话，也别把旧对话里的梗重新捡起来当新话题；"
                   "也别拿键盘、机箱、风扇、嗡嗡声、屏幕这些环境音凑开场，一句都不要。"
                   "最近几轮要是只剩「哦」「表情」这种，就别硬接了：喊他一声、问个小问题、或者拉他玩点小的。]")
        if self.address_book is not None:
            try:
                ab = self.address_book() or {}
                if ab.get("friends") or ab.get("groups"):
                    prompt += (f"\n[你的微信：好友 {ab.get('friends', 0)} 人、群 {ab.get('groups', 0)} 个。"
                               "这是他问起来时才用的信息，别主动报数字。]")
            except Exception:
                pass
        prompt += self._tail_blocks(key, ident, who, role, note, allow_acts, hint=hint)
        if self.persona is not None:
            prompt = self.persona.prefix() + prompt
        return prompt

    def _answer(self, key: str, items: list[tuple[Msg, str]]) -> None:
        msg = items[-1][0]
        text = "\n".join(t for _, t in items if t).strip()
        if len(items) > 1:
            log.info("把 %s 的 %d 条合成一条回", key, len(items))
            text = text + f"\n（对方刚才连着发了 {len(items)} 条，合成一段看，一次回完就行。）"
        # 收款助手：对方在「要给我吗」之后点头，就这时去收
        if False:  # 自动收款已移除（收款/拆红包不再做）
            pass

        if self._muted:
            # 主人发了「/停」= 她想安静（多半在打游戏）。这时连主人也不回，
            # 更不去抢前台；命令（/继续 /状态）在 _command 里已经优先处理了。
            log.info("静音中（连主人也不回），跳过 %s", key)
            return
        target = msg.room if msg.is_group else msg.sender
        if msg.is_group:
            self._last_group_reply[msg.room or key] = time.time()
            self._last_group_speaker[msg.room or key] = msg.sender
        want_at = self.cfg.group_at_sender
        if msg.is_group:
            conf = self._behavior_conf()
            if "group_at_sender" in conf:
                want_at = bool(conf.get("group_at_sender"))
        aters = msg.sender if (msg.is_group and want_at) else ""
        started = time.time()
        with self._locks[key]:
            try:
                sid = self.store.get_session(key)
                # 模型本身没有"现在几点"的感知，统一在正文前面带上本地时间，
                # 这样它回答"几点了""今天周几"不用去猜、也不依赖 shell 权限。
                # 人设放在最前面，主人改完下一句就生效。
                body = text
                self._current_media = _media_path(text) if msg.kind in ("image", "sticker") else ""
                # 当前会话标识（群：room；私聊：对方 wxid）——「记笔记」等动作按它落盘
                self._current_ident = (msg.room or msg.sender or key) if msg.is_group else (msg.sender or key)
                if msg.kind == "sticker":
                    # 表情多半只是情绪：不用描述画面，按情绪接一句就好
                    body = (f"{text}\n（对方发来一个表情包。表情通常只是情绪，不用描述画面，"
                            f"也别复述图上写了什么；就按这个情绪自然接一句。"
                            f"只有他明确问“这表情啥意思/哪里找的”时，才调用 "
                            f"read_image(file_path=\"{_media_path(text)}\") 看一眼再答。）"
                            if _media_path(text) else
                            f"{text}\n（对方发来一个表情包，你不用看清内容，按情绪自然接一句就好。）")
                elif msg.kind == "image":
                    path = _media_path(text)
                    if path:
                        body = (f"{text}\n（对方发来一张图，文件就在这台机器本地。"
                                f"先调用 read_image(file_path=\"{path}\") 看一眼（失败就再试一次这个绝对路径，"
                                f"不要直接说“看不到”）；看完**一句话**回应，别把画面从头描述一遍，"
                                f"也别编图里没有的细节。他要是问细节，再展开。）")
                    else:
                        body = (f"{text}\n（对方发来一张图，你没拿到文件；就按聊天气氛自然接一句，"
                                f"别问他要文件，也别猜内容。）")
                elif msg.kind == "system":
                    body = f"{text}\n（这是系统提示：对方拍了拍你。用一句俏皮自然的话回应，别复述这句提示。）"
                elif msg.kind == "voice":
                    body = f"{text}\n（对方发的是语音，你听不到内容；可以让他打字，但别生硬。）"
                prompt = _with_clock(body)
                plain = (text or "").strip()
                if msg.kind == "text" and len(plain) <= 8:
                    # 对方一句话就没了（"嗯""在吗""无聊"）。以前这里逼她"起个话题"，
                    # 结果就是生硬找话（"你们那边下雨吗"那种）。改成：能接就接一句短的，
                    # 没得接就顺着他的话轻轻搭一句，别硬抛新话题。
                    prompt += ("\n（对方这句很短。别复读、别追问“在吗/干嘛呢/你呢”。"
                               "有具体可接的就接一句短的；没有就顺着他的语气搭一句，"
                               "绝对不要为了编而编一个话题——硬找话题比冷场更像机器人。"
                               "如果确实要开新话题，只能从下面四类里挑，并且必须具体到细节："
                               "1) 翻他的笔记，接他之前提过的事（工作、吃的、作息、在意的人或物）；"
                               "2) 你此刻正在做的小事，说具体画面（不要“我在玩”，要“刚被队友卖了一波”）；"
                               "3) 一个带自己立场的小观点（不要“你觉得呢”这种空问）；"
                               "4) 一个具体的小请求（“帮我挑一个”）。"
                               "四类轮着用，同一类别连着用两次。）")
                elif msg.kind == "text" and len(plain) >= 40:
                    prompt += ("\n（对方说了挺长一段：先接住他话里最关键的那一点，别装没看见；"
                               "再接一句你自己的看法，别只是总结他。也别顺势又开第三个话题。）")
                who = msg.alias or msg.sender
                self._who = who  # 给动作执行时兜底目标用
                # 她说过的原话要回灌：不然会对着同一个人反复推同一个话题。
                try:
                    rows = self.store.db.execute(
                        "SELECT content FROM messages WHERE (room=? OR (sender=? AND (room IS NULL OR room=''))) "
                        "AND direction='out' ORDER BY id DESC LIMIT 6", (key, key)).fetchall()
                    said = [r[0] for r in rows if r[0]]
                    if said:
                        prompt += ("\n[你最近跟这个人说过的原话（**别重复同样的主题、别用同样的开场**）："
                                   + " / ".join(s.replace("\n", " ")[:40] for s in said) + "]")
                except Exception as e:
                    log.debug("读最近说过的话失败：%r", e)
                # 桥知道对方是不是主人，模型并不知道；不告诉它，它就会管你朋友也叫"主人"。
                role = "是主人本人" if self._is_admin(msg.sender) else "是主人的朋友，不是主人"
                note = ("" if self._is_admin(msg.sender)
                        else self._scrub_note(_read_note(msg.sender, who), who))

                # 群聊：告诉它这是哪个群、群里都有谁、说话的是谁。
                # 不写这段，她就会把群里的人当成"正在私聊的那个人"，语气全错。
                if msg.is_group:
                    gi = {}
                    if self.group_info is not None:
                        try:
                            gi = self.group_info(msg.room) or {}
                        except Exception as e:
                            log.debug("取群信息失败：%r", e)
                    gname = gi.get("name") or msg.room
                    mem = [m for m in (gi.get("members") or []) if m.get("name")]
                    members = [m["name"] for m in mem]
                    others = [n for n in members if n != who]
                    roster = "、".join(others[:15]) + ("…" if len(others) > 15 else "")
                    # 每个人单独的印象：只取有笔记的，每条压到 90 字，免得把 prompt 撑爆
                    per = []
                    for m in mem[:8]:
                        nm = m.get("name") or ""
                        wid = m.get("wxid") or ""
                        if not nm or nm == who or not wid:
                            continue
                        try:
                            n2 = self._scrub_note(_read_note(wid, nm), nm)
                        except Exception:
                            n2 = ""
                        if n2:
                            flat = " ".join(n2.split())
                            per.append(f"- {nm}：{flat[:90]}")
                    # 这个群自己的笔记（群史 + 谁是谁）
                    gnote = ""
                    try:
                        gnote = self._scrub_note(_read_note(msg.room or key, gname), gname)
                    except Exception:
                        pass
                    prompt += (
                        f"\n[这是群聊「{gname}」，不是一个跟 {who} 的私聊。现在说话的是「{who}」。"
                        + (f"\n群里还有：{roster}（共 {len(members)} 人）。" if members else "")
                        + ("\n你对这些人的印象（按人来区别对待，别念出来）：\n" + "\n".join(per) if per else "")
                        + (f"\n你对这个群的记忆：\n{gnote}" if gnote else "")
                        + ("\n（这个群你还没记过：先把人认全——现在是新群，**先自然地问一句**"
                           "「群里都有谁啊，怎么称呼」，别一上来就长篇大论。"
                           "问清楚或自己看明白之后，把人和群的事记下来："
                           "用一行 [[动作:记笔记|.|- 名字：印象，一句话]]，可以连续写几行。\n"
                           f"群的笔记文件：{_notes_path(msg.room or key)}）" if not gnote else "")
                        + "\n群里的规矩：句子短一点、别黏人、别连发超过两条；不用「主人」称呼任何人，"
                        "对谁都用「你」或者名字；跟熟的可以贫两句，跟不熟的就客气点，但都别冷场；"
                        "不要把群里某个人的私聊内容说给其他人听；别人聊的话题插不上就少说，被 @ 了必须接。]"
                    )

                # 群聊上下文：不管有没有群资料，都把最近几句抓出来——
                # 对方只 @ 一下、或者一句话里既 @ 我又 @ 别人时，没有上下文她根本不知道在说谁。
                if msg.is_group:
                    tail = self._group_tail(msg.room or key)
                    if tail:
                        log.info("群上下文：%d 条（%s）", len(tail), msg.room or key)
                        prompt += ("\n[群里刚刚发生的事（按时间先后，[时间] 说话人：内容）：\n"
                                   + "\n".join(tail)
                                   + "\n—— 回之前先把这些看一遍，特别是最后几条在聊什么、谁跟谁在说话；"
                                   "已经说过的信息别再问一遍，别答非所问。]")
                    bare = re.sub(r"^wxid_[A-Za-z0-9_-]+[:：]\s*", "", plain or "")
                    bare = re.sub(r"@\S+", "", bare).strip()
                    if len(bare) <= 2:
                        prompt += ("\n（对方只是 @ 了你、没写正文，基本是接着上面某一句在跟你说话。"
                                   "先看懂上下文，直接接那句；**不要问“叫我干嘛/怎么了”**。）")
                # 好奇心：要看他上一条是什么时候发的、说了什么——热聊中不插问句，
                # 他只回一个"嗯"的时候得顺着上一条的话题往下问，而不是追问"嗯"。
                _gap = None
                _prev = ""
                try:
                    _row = self.store.db.execute(
                        "SELECT ts, content FROM messages WHERE "
                        "(room=? OR (sender=? AND (room IS NULL OR room=''))) "
                        "AND direction='in' AND msgid<>? ORDER BY ts DESC LIMIT 1",
                        (key, key, msg.msgid)).fetchone()
                    if _row:
                        _prev = _row[1] or ""
                        if _row[0]:
                            _gap = max(0.0, time.time() - float(_row[0]))
                except Exception as _e:
                    log.debug("算上一条间隔失败：%r", _e)
                prompt += self._curiosity_block(msg.sender or key, who, note, msg.is_group,
                                                last_text=plain, gap_seconds=_gap,
                                                prev_text=_prev)
                self._mood_blocked = False
                if self._mood_on() and not msg.is_group:
                    _ident = msg.sender or key
                    _mst = self.mood.update(_ident, plain or "")
                    prompt += self.mood.prompt_line(_ident)
                    # 这一句本来就是敷衍/带刺，或者心情好感已经很低 → 这条不许发表情
                    self._mood_blocked = (_mst["sig"] in ("dismissive", "hostile")
                                          or not self.mood.allow_sticker(_ident))
                    if self._mood_blocked:
                        prompt += ("\n（他这话听着就是在敷衍你，或者你心情不好："
                                   "这条**不要**发任何表情包，句子短一点、别撒娇、别讨好。）")
                prompt += self._tail_blocks(key, msg.sender or key, who, role, note)
                if self.persona is not None:
                    prompt = self.persona.prefix() + prompt
                reply, new_sid = self.brain.ask(prompt, sid)
                if new_sid and new_sid != sid:
                    self.store.save_session(key, new_sid)
                elapsed = time.time() - started
                log.info("回复 %s（%.1fs，%d 字）", key, elapsed, len(reply))
                # 聊得够多了就顺手整理一次，把记忆写进笔记、换新会话（后台做，不挡回复）
                try:
                    import memory as memory_mod

                    if memory_mod.due(self.store, key, self._memory_state_path()) >= memory_mod.EVERY:
                        threading.Thread(
                            target=memory_mod.compress,
                            args=(self.store, self.brain, key,
                                  self.name_of(msg) if msg else key,
                                  self._memory_state_path(), memory_mod.EVERY, False,
                                  bool(msg and msg.is_group)),
                            daemon=True).start()
                except Exception as e:
                    log.debug("上下文整理没触发：%r", e)
                reply, tags = pull_stickers(reply)
                if (self._mood_on() and not msg.is_group
                        and (getattr(self, "_mood_blocked", False)
                             or not self.mood.allow_sticker(msg.sender or key))):
                    if tags:
                        log.info("心情/好感不够，这次不发表情（本来要发：%s）", "、".join(tags))
                    tags = []
                reply, acts = pull_actions(reply)
                # 真人不会每次都甩一整段：偶尔拆成两三条短句连着发。
                bconf = self._behavior_conf()
                # 对方一口气发了好几条：合成一条整的回（拆成两段反而更乱）。
                if len(items) >= 3:
                    bconf = dict(bconf)
                    bconf["bubble_max"] = 1
                    bconf["bubble_chance"] = 0.0
                    log.info("对方连发了 %d 条，这次合成一条回", len(items))
                chunks: list[str] = []
                for bub in split_bubbles(reply, bconf):
                    chunks.extend(split_text(bub, self.cfg.max_chars))
                gap_lo = float(bconf.get("bubble_gap_min", 0.05) or 0.05)
                gap_hi = max(gap_lo, float(bconf.get("bubble_gap_max", 0.15) or 0.15))
                if len(chunks) > 1:
                    log.info("连发 %d 条", len(chunks))

                # 这几条连着发完再放手，别让别人的消息插进中间
                sent_ok = False
                with self._seq_lock:
                    for i, chunk in enumerate(chunks):
                        if self.guard:
                            ok, why = self.guard.check_outgoing(chunk, key)
                            if not ok:
                                log.warning("内容被防封层拦下（%s），改为婉拒", why)
                                chunk = self.guard.conf.blocked_reply
                            if i == 0:
                                self.guard.delay_for(chunk)
                        if self._safe_send(target, chunk, aters):
                            sent_ok = True
                        elif i + 1 < len(chunks):
                            # 这条没发出去就别接着发了，免得只收到半截话
                            break
                        if self.guard:
                            self.guard.note_sent(key, chunk)
                        if i + 1 < len(chunks):
                            time.sleep(random.uniform(gap_lo, gap_hi))
                    # 一条都没发出去：记成 seen，免得补收每分钟把它拖回来重发一遍
                    # （会表现为"一直抢鼠标去搜某个人"）。同时别配表情了。
                    if not sent_ok:
                        log.warning("这条没能发出去，25 秒后补发一次：%s", (msg.text or "")[:30])
                        self._schedule_resend(target, chunks[0] if chunks else reply, aters)
                        if msg.msgid:
                            self.store.mark_seen(msg.msgid)
                        return
                    # room 记成"会话标识"（私聊就是对方 wxid），这样以后查
                    # "谁的问题还没回"才对得上；群聊本来就是群 id。
                    self.store.log(msg.msgid, self.self_wxid, msg.room or key, "out", reply, elapsed)
                    # 对方连发多条时，表情会插在对话中间干扰阅读 → 只在最后补一张
                    defer = getattr(self, "_merge_count", 1) > 1
                    for tag in tags[:1 if defer else 2]:
                        self._safe_send_sticker(target, tag)
                # 她主动配一张：不是每条都配，按面板里的概率掷骰子，
                # 标签只从「可爱类」里挑，别在正经话题上甩表情。
                if not tags and self.stickers is not None and getattr(self, "_merge_count", 1) <= 1:
                    self._maybe_auto_sticker(target, key, bconf)
                for kind, hint, payload in acts[:3]:
                    self._do_action(kind, hint, payload, target)
            except BrainError as e:
                log.error("大脑出错：%s", e)
                # DSH 一个会话只允许一个写句柄；上次超时如果没死干净，这个会话就"锁死"了，
                # 之后每次调用都秒失败。这种时候别一直卡着：换一个新会话接着聊。
                if "already owned" in str(e):
                    self.store.reset_session(key)
                    self._safe_send(target, "我这边记性卡住了，我重开一下接着聊。", aters)
                else:
                    self._safe_send(target, "我这边卡住了，等我修一下再回你。", aters)
            except Exception:
                # 任何意外都不能让这条消息"石沉大海"：记下来，并且说一句人话
                log.exception("处理消息出错（%s）", key)
                self._safe_send(target, "我这边手滑了一下，你再说一遍？", aters)

    def _do_action(self, kind: str, hint: str, payload: str, default_target: str) -> None:
        """执行模型用 [[动作:...]] 点名的界面操作。同一进程、同一把界面锁，比模型自己跑命令快得多。"""
        k = (kind or "").strip()
        who = (hint or "").strip()
        dst = who or default_target
        try:
            import actions as actions_mod

            if actions_mod.resolve(k):
                ctx = {
                    "bridge": self,
                    "target": default_target,
                    "name": getattr(self, "_current_name", "") or default_target,
                    "media": getattr(self, "_current_media", ""),
                    "ident": getattr(self, "_current_ident", "") or default_target,
                    "resolve": lambda n: n,
                }
                ok, msg = actions_mod.run(k, who, payload, ctx)
                log.info("动作 %s（%s）→ %s：%s", k, dst, "成" if ok else "没成", msg)
                return
            if k in ("发消息", "发", "send", "私聊"):
                if not payload:
                    log.warning("动作[发消息]没有内容，跳过")
                    return
                ok = self._safe_send(dst, payload)
                log.info("替主人发消息 → %s：%s（%s）", dst, payload[:30], ok)
                if ok and self.guard:
                    self.guard.note_sent(dst, payload)
            elif k in ("表情", "sticker", "发表情"):
                tag = payload or "开心"
                ok = self._safe_send_sticker(who or default_target, tag)
                log.info("替主人发表情 → %s：[%s]（%s）", dst, tag, ok)
            elif k in ("戳", "poke", "戳一戳", "拍一拍"):
                if self.poke is None:
                    log.warning("没有接到戳一戳能力")
                    return
                log.info("替主人戳一下 %s：%s", dst, self.poke(dst))
            elif k in ("同意", "accept", "通过", "加好友"):
                fn = (self.actions or {}).get("accept_friends")
                if fn is None:
                    log.warning("没有接到好友申请能力")
                    return
                res = fn(limit=3, names={who} if who else None)
                log.info("替主人同意 %s：%s", who or "全部", res)
            else:
                log.warning("不认识的动作：%s", kind)
        except Exception as e:
            log.error("动作执行失败（%s / %s）：%r", kind, hint, e)

    # ---------------- 对话收尾的追问 ----------------
    def _curiosity_question(self, key: str) -> str:
        """对话停了一会儿之后，生成一句「想更了解他」的问题（只一句）。"""
        try:
            who = self.store.alias_of(key) or key
        except Exception:
            who = key
        note = ""
        try:
            note = self._read_note(key, who, 600)
        except Exception:
            pass
        tail = ""
        try:
            rows = self.store.db.execute(
                "select direction, content from messages where (room=? or sender=?) "
                "order by id desc limit 8",
                (key, key),
            ).fetchall()
            lines = []
            for d, c in reversed(rows):
                c = (c or "").replace("\n", " ")[:40]
                if not c:
                    continue
                lines.append(("我" if d == "out" else "他") + "：" + c)
            tail = "\n".join(lines)
        except Exception:
            pass
        prompt = (
            (self.persona.prefix() if self.persona else "")
            + _with_clock("")
            + f"[当前聊天对象：{who}]\n"
            + (f"[你记得关于他的事]\n{note[:600]}\n" if note else "")
            + (f"[刚才的对话]\n{tail}\n" if tail else "")
            + "对方已经三分钟没吭声了，这轮对话基本结束。你想更了解他一点，"
            "现在问一个**具体的小问题**（作息、口味、在忙什么、玩什么、身边的小动物之类，"
            "要接着刚才的话头或者他的生活，别问「在吗」「还有事吗」这种废话）。\n"
            "**只回这一句问题本身**，一到十四字，不要解释你为什么问。"
        )
        try:
            reply, _sid = self.brain.ask(prompt, None)
        except Exception as e:
            log.warning("生成追问失败：%r", e)
            return ""
        reply = (reply or "").strip().splitlines()[0].strip() if reply else ""
        return reply[:40]

    def idle_tick(self, now: float | None = None) -> int:
        """聊完了就总结一次笔记（不再边聊边记）。

        判定：这个会话最后一条消息已过去 note_summary_after_minutes 分钟，且自上次
        总结后至少攒了 note_summary_min_new 条新消息。总结在后台线程跑，不挡回消息。
        """
        bconf = self._behavior_conf()
        if not bconf.get("note_summary_enabled", True):
            return 0
        after = float(bconf.get("note_summary_after_minutes", 10) or 0) * 60.0
        min_new = int(bconf.get("note_summary_min_new", 6) or 0)
        now = time.time() if now is None else now
        state_path = Path(self.settings_dir) / "note_summary.json"
        started = 0
        for key, ts in list(self._conv_last_activity.items()):
            if now - ts < after:
                continue
            is_group = key.endswith("@chatroom")
            name = key
            try:
                if is_group and self.group_info is not None:
                    name = (self.group_info(key) or {}).get("name") or key
                else:
                    row = self.store.db.execute(
                        "SELECT alias FROM contacts WHERE wxid=?", (key,)
                    ).fetchone()
                    if row and row[0]:
                        name = row[0]
            except Exception:
                pass

            def _job(k=key, n=name, g=is_group):
                try:
                    memory_mod.summarize_idle(
                        self.store, self.brain, k, n, state_path,
                        is_group=g, min_new=min_new,
                    )
                except Exception as e:
                    log.warning("聊完总结失败（%s）：%r", n, e)

            threading.Thread(target=_job, name="note-summary", daemon=True).start()
            self._conv_last_activity[key] = now  # 先记时间，避免 20 秒后重复排队
            started += 1
        if started:
            log.info("聊完总结：本轮排了 %d 个会话", started)
        return started

    def ping_tick(self, now: float | None = None) -> list[tuple[str, str]]:
        """每 20 秒调用一次：谁的对话刚好停了 3 分钟，就按概率追问一句。

        规则（用户定的）：
        - 只在私聊；最后一句必须是「她说的」（对方没接话才算对话结束）
        - 停下满 curiosity_ping_after_minutes 分钟才判定
        - 每次对话**只掷一次骰子**，掷完就标记，这个话题之后不再追问
        - 静音中不追问
        """
        bconf = self._behavior_conf()
        if not bconf.get("curiosity_ping_enabled", True):
            return []
        if self._muted:
            return []
        now = now or time.time()
        after = float(bconf.get("curiosity_ping_after_minutes", 3) or 3) * 60.0
        chance = float(bconf.get("curiosity_ping_chance", 0.3) or 0.3)
        out: list[tuple[str, str]] = []
        cooldown = float(bconf.get("curiosity_ping_cooldown_minutes", 60) or 60) * 60.0
        for key, ts_in in list(self._conv_in.items()):
            ts_out = self._conv_out.get(key, 0.0)
            if ts_out <= ts_in:
                continue
            if now - max(ts_in, ts_out) < after:
                continue
            if now - self._conv_last_ping.get(key, 0.0) < cooldown:
                continue
            # 掷一次骰子就记一次，冷却期（默认 1 小时）内不再追问
            self._conv_last_ping[key] = now
            if random.random() >= chance:
                log.info("对话收尾：%s 这次不追问（掷骰没过）", key)
                continue
            if self.guard:
                ok, why = self.guard.allow_incoming(key, False)
                if not ok:
                    log.info("对话收尾：%s 不追问（%s）", key, why)
                    continue
            q = self._curiosity_question(key)
            if not q:
                continue
            if self.guard:
                ok, why = self.guard.check_outgoing(q, key)
                if not ok:
                    log.info("追问内容被拦（%s），跳过", why)
                    continue
            if self._safe_send(key, q):
                if self.guard:
                    self.guard.note_sent(key, q)
                log.info("对话收尾追问 → %s：%s", key, q)
                out.append((key, q))
        return out

    def _safe_send_sticker(self, target: str, tag: str) -> bool:
        """优先用微信「收藏」里的表情；没有收藏通道时才退回表情库图片。"""
        if self.send_favorite is not None:
            idx = None
            conf = self._behavior_conf()
            mapping = conf.get("favorite_map") or {}
            picks = mapping.get(tag)
            if picks:
                try:
                    idx = random.choice([int(x) for x in picks])
                except Exception:
                    idx = None
            try:
                ok = bool(self.send_favorite(target, idx))
            except Exception as e:
                log.error("收藏表情发送失败 %s: %r", target, e)
                ok = False
            if ok:
                log.info("发了个收藏表情：%s → %s", tag, target)
                if self.guard:
                    self.guard.note_sent(target, f"[表情:{tag}]")
                time.sleep(0.1)
                return True
            log.info("收藏表情没发成，退回表情库图片（%s）", tag)

        if self.stickers is None or self.send_image is None:
            return False
        path = self.stickers.pick(tag)
        if path is None:
            log.info("表情库里没有「%s」这一档，跳过", tag)
            return False
        try:
            ok = bool(self.send_image(str(path), target))
        except Exception as e:
            log.error("表情发送失败 %s: %r", target, e)
            ok = False
        if ok:
            log.info("发了个表情：%s → %s", tag, target)
            if self.guard:
                self.guard.note_sent(target, f"[表情:{tag}]")
            time.sleep(0.6)
        return ok

    def _safe_send(self, target: str, text: str, aters: str = "") -> bool:
        # 同一句话在 90 秒内不重复发给同一个人：连发/重试/两路逻辑撞车时，
        # 用户看到的就是"同一条消息刷了两遍"，这既难堪也是风控特征。
        now = time.time()
        sig = (target, text)
        prev = self._recent_out.get(sig)
        if prev and now - prev < 90:
            log.info("这句话 %ds 前刚发过，跳过重复 → %s：%s",
                     int(now - prev), target, text[:24])
            self._conv_out[target] = now
            self._conv_last_activity[target] = now
            return True
        try:
            ok = bool(self.send(target, text, aters))
        except Exception as e:  # 发送失败不能把工作线程带崩
            log.error("发送失败 %s: %r", target, e)
            ok = False
        if ok:
            self._recent_out[sig] = now
            if len(self._recent_out) > 200:
                cutoff = now - 300
                self._recent_out = {k: v for k, v in self._recent_out.items() if v > cutoff}
        if not ok and self.guard:
            if self.guard.note_failed(f"send to {target}"):
                log.error("防封熔断已触发，停止自动回复")
                self._stop.set()
        return ok

    def _schedule_resend(self, target: str, text: str, aters: str = "",
                         delay: float = 25.0) -> None:
        """一条都没发出去时，delay 秒后补发一次（静音/已停/过不了防封就不补）。"""
        text = (text or "").strip()
        if not text:
            return

        def fire() -> None:
            try:
                if self._stop.is_set() or self.muted_now():
                    log.info("补发跳过（桥已停或静音中）：%s", text[:24])
                    return
                if self.guard:
                    ok, why = self.guard.check_outgoing(text, target)
                    if not ok:
                        log.warning("补发被防封层拦下（%s），放弃：%s", why, text[:24])
                        return
                with self._seq_lock:
                    if self._safe_send(target, text, aters):
                        log.info("补发成功 → %s：%s", target, text[:24])
                    else:
                        log.warning("补发仍然失败 → %s：%s", target, text[:24])
            except Exception as e:
                log.warning("补发出错：%r", e)

        t = threading.Timer(max(1.0, float(delay)), fire)
        t.daemon = True
        t.start()
        log.info("已安排 %.0f 秒后补发一次 → %s", delay, target)

    def _load_mute(self) -> bool:
        try:
            self._mute_mtime = self._mute_path.stat().st_mtime
        except Exception:
            self._mute_mtime = 0.0
        try:
            return bool(json.loads(self._mute_path.read_text(encoding="utf-8")).get("muted"))
        except Exception:
            return False

    def _memory_state_path(self):
        return Path(self.settings_dir) / "memory_state.json"

    def _refresh_mute(self) -> None:
        """mute.json 被外部改过（面板/脚本/我手动）就跟着变，不用重启。"""
        try:
            m = self._mute_path.stat().st_mtime
        except Exception:
            m = 0.0
        if m and m != getattr(self, "_mute_mtime", 0.0):
            self._mute_mtime = m
            self._muted = self._load_mute()
            log.warning("静音状态被外部改成：%s", "开" if self._muted else "关")

    def _set_muted(self, flag: bool) -> None:
        self._muted = flag
        try:
            self._mute_path.parent.mkdir(parents=True, exist_ok=True)
            self._mute_path.write_text(
                json.dumps({"muted": flag, "since": time.time()}, ensure_ascii=False),
                encoding="utf-8")
        except Exception as e:
            log.warning("静音状态没存上：%r", e)
        if flag:
            with self._pending_lock:
                dropped = sum(len(v) for v in self._pending.values())
                self._pending.clear()
            if dropped:
                log.info("静音：丢掉 %d 条还没回的连发消息", dropped)

    # ---------------- 自动回复熔断（只静音出问题的那个会话） ----------------
    _REPEAT_WINDOW = 600      # 10 分钟内
    _REPEAT_HITS = 3          # 同一条出现 3 次就认定是自动回复
    _MUTE_SECONDS = 3600      # 静音 1 小时

    def _load_mute_chats(self) -> dict:
        try:
            d = json.loads(self._mutechats_path.read_text(encoding="utf-8")) or {}
            return {str(k): float(v) for k, v in d.items() if v}
        except Exception:
            return {}

    def _save_mute_chats(self) -> None:
        try:
            now = time.time()
            self._mutechats = {k: v for k, v in self._mutechats.items() if float(v) > now}
            self._mutechats_path.parent.mkdir(parents=True, exist_ok=True)
            self._mutechats_path.write_text(
                json.dumps(self._mutechats, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception as e:
            log.warning("熔断静音表写不进去：%r", e)

    def chat_muted(self, key: str) -> bool:
        """这个会话是不是因为在刷同一条消息而被静音了（主动消息也要绕开）。"""
        if not key:
            return False
        try:
            m = self._mutechats_path.stat().st_mtime
            if m != self._mutechats_mtime:
                self._mutechats_mtime = m
                self._mutechats = self._load_mute_chats()
        except OSError:
            pass
        return float(self._mutechats.get(key) or 0) > time.time()

    def mute_chat(self, key: str, seconds: int = None, why: str = "") -> None:
        secs = int(seconds or self._MUTE_SECONDS)
        self._mutechats[key] = time.time() + secs
        self._save_mute_chats()
        log.warning("熔断：静音 %s 分钟 → %s（%s）", secs // 60, key, why)

    def _repeat_guard(self, key: str, text: str) -> bool:
        """同一会话 10 分钟内把同一句话发来 3 次 = 对面是自动回复/机器人。

        微信团队、公众号这类号会对着她的话回同一段话，不掐就会一直对下去。
        命中就静音这个会话 1 小时（返回 True 表示这条别回）。
        """
        t = _norm_in(text)
        if not t:
            return False
        now = time.time()
        hist = [x for x in (self._recent_in.get(key) or [])
                if now - x[1] <= self._REPEAT_WINDOW]
        hist.append((t, now))
        self._recent_in[key] = hist[-20:]
        n = sum(1 for s, _ in hist if s == t)
        if n >= self._REPEAT_HITS:
            self._recent_in[key] = []
            self.mute_chat(key, self._MUTE_SECONDS, f"同一条消息 {n} 次")
            return True
        return False

    def _cmd_limits(self, arg: str, target: str) -> None:
        if not self.guard:
            self._safe_send(target, "没装防封层，没有上限可改。")
            return
        parts = (arg or "").split(maxsplit=1)
        if not parts or not parts[0]:
            self._safe_send(target, self.guard.describe_limits())
            return
        what = parts[0]
        val = parts[1].strip() if len(parts) > 1 else ""
        if what in ("看", "show"):
            self._safe_send(target, self.guard.describe_limits())
            return
        if what in ("关", "不限", "off", "unlimited"):
            self._safe_send(target, "好了，今天不限量。\n" + self.guard.set_limit("unlimited", True))
            return
        if what in ("开", "恢复", "on"):
            self.guard.set_limit("unlimited", False)
            self._safe_send(target, "恢复按上限走。\n" + self.guard.describe_limits())
            return
        key = {"日": "daily_total", "总": "daily_total",
               "人": "daily_per_contact", "个人": "daily_per_contact"}.get(what)
        if not key:
            self._safe_send(target, "用法：/上限 看 ／ /上限 日 300 ／ /上限 人 80 ／ /上限 关")
            return
        try:
            n = int(val)
        except ValueError:
            self._safe_send(target, f"「{val}」不是数字。比如 /上限 日 300")
            return
        if n < 0:
            self._safe_send(target, "上限不能是负数。")
            return
        self._safe_send(target, "改好了。\n" + self.guard.set_limit(key, n))

    def _cmd_status(self, target: str) -> None:
        q = getattr(self._pool, "_work_queue").qsize()
        try:
            s = self.guard.describe() if self.guard else "没装防封层"
        except Exception:
            s = "防封层状态读不到"
        # 随机主动：让人一眼看到"开没开、今天发了几条、下一次什么时候"
        try:
            sc = getattr(self, "scheduler", None)
            if sc is None:
                r = "随机主动：没接上"
            else:
                rnd = (sc.state.get("random") or {})
                rstate = (sc.state.get("random_state") or {})
                today = datetime.now().strftime("%Y-%m-%d")
                done = int(rstate.get("count", 0)) if rstate.get("day") == today else 0
                per_day = int(rnd.get("per_day", 2))
                gap = int(rnd.get("min_interval_minutes", 240))
                if not rnd.get("enabled"):
                    r = "随机主动：关"
                else:
                    last = float(rstate.get("last_ts", 0) or 0)
                    nxt = ""
                    if last and done < per_day:
                        left = gap * 60 - (time.time() - last)
                        nxt = f"，{max(0, int(left // 60))} 分钟后可再发" if left > 0 else "，随时可发"
                    r = (f"随机主动：开（今天 {done}/{per_day} 条，"
                         f"间隔 {gap} 分钟，命中率 {rnd.get('chance', 0)}）{nxt}")
        except Exception as e:
            r = f"随机主动：状态读不到（{e!r}）"
        try:
            if getattr(self, "mood", None) is not None and self._mood_on():
                cur_m = "好感/心情：" + self.mood.describe(target)
            else:
                cur_m = ""
        except Exception:
            cur_m = ""
        try:
            _bc = self._behavior_conf()
            cur = ("好奇心：%s（概率 %.2f，冷却 %d 轮，本次运行命中 %d 次；%s，%s）" % (
                "开" if _bc.get("curiosity_enabled", True) else "关",
                float(_bc.get("curiosity_chance", 0.3) or 0),
                int(_bc.get("curiosity_cooldown_turns", 4) or 4),
                getattr(self, "_curio_hits", 0),
                "只顺当前话题" if _bc.get("curiosity_follow_topic", True) else "从话题清单挑",
                ("只在冷场问（%d 秒 / %d 字）" % (int(float(_bc.get("curiosity_lull_seconds", 90) or 90)),
                                              int(_bc.get("curiosity_short_chars", 6) or 6)))
                if _bc.get("curiosity_lull_only", True) else "随时可能问"))
        except Exception:
            cur = "好奇心：状态读不到"
        self._safe_send(target, (
            f"静音：{'开（连你也不回）' if self._muted else '关'}\n"
            f"排队回复：{q} 条\n"
            f"{r}\n"
            f"{cur}\n"
            f"{cur_m + chr(10) if cur_m else ''}"
            f"{s}"))

    def _command(self, raw: str, key: str, msg: Msg) -> None:
        parts = raw.strip().split(maxsplit=2)
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""
        rest = parts[2] if len(parts) > 2 else ""
        target = msg.room if msg.is_group else msg.sender

        if cmd == "/stats":
            s = self.store.stats()
            self._safe_send(target, f"消息 {s['messages']} 条，联系过 {s['contacts']} 人，24 小时 {s['last_24h']} 条。")
        elif cmd == "/reset":
            self.store.reset_session(key)
            self._safe_send(target, "这个对话的记忆清掉了，重新开始。")
        elif cmd == "/mute":
            self._set_muted(True)
            log.info("收到 /mute：完全静默")
        elif cmd == "/unmute":
            self._set_muted(False)
            log.info("收到 /unmute：解除静默（不回执）")
        elif cmd == "/safety":
            if self.guard:
                self._safe_send(target, self.guard.describe())
            else:
                self._safe_send(target, "没装防封层。")
        elif cmd in ("/人设", "/persona"):
            self._cmd_persona(arg, rest, target)
        elif cmd in ("/节奏", "/rhythm"):
            self._cmd_rhythm(arg, target)
        elif cmd in ("/主动", "/say"):
            self._cmd_say(arg, rest, target)
        elif cmd in ("/同意", "/accept"):
            self._cmd_accept(target)
        elif cmd in ("/表情", "/sticker") or cmd.startswith("/表情"):
            self._cmd_sticker(arg, rest, target)
        elif cmd in ("/戳", "/poke") or cmd.startswith("/戳"):
            self._cmd_poke(arg, rest, target)
        elif cmd in ("/help", "/帮助", "/h", "/命令"):
            self._safe_send(target, _HELP_TEXT)
        elif cmd in ("/停", "/暂停", "/stop", "/mute2"):
            self._set_muted(True)
            log.info("收到 /停：完全静默（不回执）")
        elif cmd in ("/继续", "/开始", "/resume", "/unmute2"):
            self._set_muted(False)
            log.info("收到 /继续：解除静默（静默执行，不回执）")
        elif cmd in ("/状态", "/status", "/who"):
            self._cmd_status(target)
        elif cmd in ("/上限", "/limit", "/limits"):
            self._cmd_limits(arg, target)
        else:
            import difflib

            known = ["/help", "/status", "/stats", "/safety", "/同意", "/表情", "/戳",
                     "/人设", "/节奏", "/主动", "/reset", "/停", "/继续",
                     "/上限", "/mute"]
            near = difflib.get_close_matches(cmd, known, n=2, cutoff=0.5)
            if near:
                self._safe_send(target, f"没有 {cmd}。你是不是想用 {' 或 '.join(near)}？\n发 /help 看全部。")
            else:
                self._safe_send(target, f"没有 {cmd} 这个命令。\n" + _HELP_TEXT)

    # ---------------- 子命令 ----------------
    def _cmd_persona(self, arg: str, rest: str, target: str) -> None:
        if self.persona is None:
            self._safe_send(target, "没装人设模块。")
            return
        body = " ".join(x for x in (arg, rest) if x).strip()
        if not body or body in ("查看", "show"):
            self._safe_send(target, "现在的人设：\n" + self.persona.describe())
        elif body in ("清空", "reset", "清除"):
            self._safe_send(target, self.persona.clear())
        elif body.startswith("追加 "):
            self._safe_send(target, self.persona.append(body[3:]))
        else:
            self._safe_send(target, self.persona.set_text(body))

    def _cmd_rhythm(self, arg: str, target: str) -> None:
        name = arg.strip().lower()
        if not name:
            self._safe_send(target, f"当前节奏：{self.cfg.rhythm_profile}；可选 natural / calm / fast")
            return
        if name not in ("natural", "calm", "fast"):
            self._safe_send(target, "只有 natural（正常）/ calm（更慢更稳）/ fast（快）三档。")
            return
        import runtime

        msg = runtime.apply_profile(name, self.guard)
        self.cfg.rhythm_profile = name
        if self.settings is not None:
            self.settings["rhythm_profile"] = name
            runtime.save_settings(self.settings_dir, self.settings)
        self._safe_send(target, "节奏切到 " + msg)

    def _cmd_say(self, arg: str, rest: str, target: str) -> None:
        who = arg.strip()
        text = rest.strip()
        if not who:
            self._safe_send(target, "用法：/主动 目标 内容\n例：/主动 张三 在吗\n"
                                     "只想让她自己编一句：/主动 张三（不带内容）\n"
                                     "目标可以是昵称、备注、群名或「文件传输助手」。")
            return
        if not text:
            # 不带内容 = 让她"凭记忆"自己编一句，交给主循环生成发送（要走模型，得几秒）
            try:
                import json as _json
                req = Path(self.settings_dir) / "proactive_now.json"
                req.write_text(_json.dumps({"who": who, "at": time.time()},
                                           ensure_ascii=False), encoding="utf-8")
                self._safe_send(target, f"好，让她自己编一句给「{who}」，几秒后发出去。")
            except Exception as e:
                self._safe_send(target, f"没触发成功：{e}")
            return
        if self.guard is not None:
            ok, why = self.guard.check_outgoing(text, "")
            if not ok:
                self._safe_send(target, f"这条我没敢发（{why}）。")
                return
        if self.guard is not None:
            self.guard.delay_for(text)
        ok = self._safe_send(who, text)
        if self.guard is not None and ok:
            self.guard.note_sent(who, text)
        self._safe_send(target, f"发给「{who}」了。" if ok else "没发出去，看一下微信窗口是不是被挡住了。")

    def _cmd_accept(self, target: str) -> None:
        fn = self.actions.get("accept_friends")
        if fn is None:
            self._safe_send(target, "这个版本没接界面操作，用不了 /同意。")
            return
        self._safe_send(target, "去看一下有没有新的好友申请……")
        try:
            res = fn(limit=5)
        except Exception as e:
            log.warning("通过好友申请失败：%r", e)
            self._safe_send(target, f"没弄成：{e}")
            return
        seen = res.get("seen") or []
        n = int(res.get("accepted") or 0)
        if res.get("ambiguous"):
            self._safe_send(target, "有条申请对得上好几个名字：" + "、".join(seen[:5])
                            + "。你说准哪个我再动手。")
            return
        if not seen:
            self._safe_send(target, "通讯录里没有待通过的申请。")
        elif n == 0:
            self._safe_send(target, "有申请（" + "、".join(seen[:5]) + "），但这次一条都没通过，可能弹窗没跟上。")
        else:
            names = "、".join(seen[:n])
            self._safe_send(target, f"通过了 {n} 条：{names}" + ("（还有没处理的）" if len(seen) > n else ""))

    def _cmd_sticker(self, arg: str, rest: str, target: str) -> None:
        if self.stickers is None:
            self._safe_send(target, "这个版本没装表情库。")
            return
        sub = (arg or "").strip()
        body = (rest or "").strip()

        if not sub:
            idx = self.stickers.tags()
            if not idx:
                self._safe_send(target, "表情库还是空的。\n先攒点：/表情 找 开心\n或者 /表情 找 无语 有什么好玩的？")
                return
            lines = [f"{k}（{len(v)} 张）" for k, v in sorted(idx.items())]
            self._safe_send(target, "表情库：" + "、".join(lines) + "\n用法：/表情 找 <标签> [关键词] · /表情 发 <标签> [目标] · /表情 加 <标签> <图片路径或链接>")
            return

        if sub in ("找", "搜", "抓"):
            bits = body.split(maxsplit=1)
            tag = bits[0] if bits else ""
            query = bits[1] if len(bits) > 1 else ""
            if not tag:
                self._safe_send(target, "用法：/表情 找 开心\n/表情 找 无语 无语 表情包（第二个是搜索词）")
                return
            self._safe_send(target, f"去网上找「{tag}」……")
            try:
                got = self.stickers.fetch(tag, query, n=3)
            except Exception as e:
                self._safe_send(target, f"网上找失败了：{e}")
                return
            if not got:
                self._safe_send(target, "没找到能下的图，换个词试试。")
            else:
                self._safe_send(target, f"收进库里 {len(got)} 张「{tag}」的。要试发一张吗：/表情 发 {tag}")
            return

        if sub in ("发", "发一张", "send"):
            bits = body.split(maxsplit=1)
            tag = bits[0] if bits else ""
            who = bits[1] if len(bits) > 1 else target
            if not tag:
                self._safe_send(target, "用法：/表情 发 开心  或  /表情 发 开心 张三")
                return
            ok = self._safe_send_sticker(who, tag)
            if who != target or not ok:
                self._safe_send(target, f"给「{who}」发了一个「{tag}」的表情。" if ok else f"库里没有「{tag}」，先 /表情 找 {tag}")
            return

        if sub in ("加", "加一个", "add"):
            bits = body.split(maxsplit=1)
            tag = bits[0] if bits else ""
            src = bits[1].strip() if len(bits) > 1 else ""
            if not tag or not src:
                self._safe_send(target, "用法：/表情 加 开心 C:\\路径\\图.png\n或者：/表情 加 开心 https://.../x.jpg")
                return
            try:
                if src.lower().startswith(("http://", "https://")):
                    import tempfile
                    import urllib.request
                    from pathlib import Path as _P

                    tmp = _P(tempfile.gettempdir()) / "sticker_from_url"
                    req = urllib.request.Request(src, headers={"User-Agent": "Mozilla/5.0"})
                    tmp.write_bytes(urllib.request.urlopen(req, timeout=20).read())
                    dst = self.stickers.add_file(tmp, tag)
                else:
                    dst = self.stickers.add_file(src, tag)
            except Exception as e:
                self._safe_send(target, f"没加进去：{e}")
                return
            self._safe_send(target, f"收好了：{dst.name}（标签「{tag}」）")
            return

        # 其它情况当成标签名，直接发
        ok = self._safe_send_sticker(target, sub)
        if not ok:
            self._safe_send(target, f"库里没有「{sub}」。先 /表情 找 {sub} 抓几张。")

    def _cmd_poke(self, arg: str, rest: str, target: str) -> None:
        if self.poke is None:
            self._safe_send(target, "这个版本没接戳一戳。")
            return
        who = (arg or "").strip() or (rest or "").strip()
        if not who:
            self._safe_send(target, "用法：/戳 张三 （昵称/备注/群名）")
            return
        try:
            ok = bool(self.poke(who))
        except Exception as e:
            log.warning("戳一戳失败：%r", e)
            self._safe_send(target, f"没戳成：{e}")
            return
        self._safe_send(target, f"戳了「{who}」一下。" if ok else f"没戳到「{who}」，是不是名字不对或者窗口被挡住了。")
