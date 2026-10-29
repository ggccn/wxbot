"""上下文增长控制：聊到一定量就把「这段聊了啥」压进笔记，再换一段干净会话。

DSH 每个联系人一个会话，会话越长越容易跑偏、也越贵。做法：
- 每聊满 `EVERY` 条（默认 60）触发一次；
- 用独立的一次性会话（不污染当前会话）把最近聊天压成几行摘要；
- 摘要追加写进 notes 目录下这个人的笔记文件，然后清掉该联系人的会话 id。
这样"记忆"留在文件里，上下文永远只有最近几十条。
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Optional

import logging

import notes as notes_mod
from paths import NOTES_DIR

log = logging.getLogger("memory")

NOTES_DIR = NOTES_DIR
EVERY = 60          # 多少条触发一次整理
KEEP_RECENT = 120   # 摘要时看最近多少条

_SUM_PROMPT = """把下面这段微信聊天压缩成一份"记忆卡"，用第三人称、不超过 12 行，只写长期有用的：

- 称呼：他/她希望被怎么叫、互相怎么称呼（没定就写"还没定"）
- 印象：性格、口味、作息、工作、在意的事、说话习惯（最多 5 条短句）
- 梗与玩笑：两人之间的黑话、外号、只有你们懂的事（最多 3 条）
- 聊到哪：上次聊到什么事、没说完的话题、答应过什么（最多 3 条）
- 忌讳：明显不想聊的话题、踩过的雷

不要复述原话，不要写日期，不要客套。直接给条目，不要解释你在做什么。

聊天记录：
{transcript}
"""

_GROUP_PROMPT = """把下面这段微信群聊天压缩成一份"群记忆卡"，用第三人称、不超过 14 行，只写长期有用的：

- 这个群：群名、大概是个什么群（朋友/游戏/工作）、气氛
- 群里的人：谁是谁，各一句话（性格、爱说什么、跟我的关系），最多 8 个人
- 梗与玩笑：群里流行的梗、外号、黑话（最多 3 条）
- 聊到哪：最近在聊什么、悬着没完的事（最多 3 条）
- 注意：谁不喜欢被怎么称呼、哪些话题别碰

不要复述原话，不要写日期。直接给条目。

聊天记录：
{transcript}
"""


def _transcript(db, key: str, limit: int = KEEP_RECENT) -> str:
    rows = db.execute(
        "SELECT direction, content FROM messages WHERE (room=? OR (sender=? AND (room IS NULL OR room=''))) "
        "ORDER BY id DESC LIMIT ?", (key, key, limit)).fetchall()
    out = []
    for direction, content in reversed(rows):
        who = "我" if direction == "out" else "对方"
        text = re.sub(r"\s+", " ", (content or "")).strip()
        if text:
            out.append(f"{who}：{text[:120]}")
    return "\n".join(out)


def _notes_path(name: str) -> Path:
    safe = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", name)[:40] or "unknown"
    return NOTES_DIR / f"{safe}.md"


def due(store, key: str, state_path: Path, every: int = EVERY) -> int:
    """返回距离上次整理新增的消息条数（到阈值才真整理）。"""
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        state = {}
    last = int(state.get(key, 0))
    total = store.db.execute(
        "SELECT COUNT(*) FROM messages WHERE (room=? OR (sender=? AND (room IS NULL OR room='')))", (key, key)).fetchone()[0]
    return total - last if total >= last else 0



def _last_seq(store, key: str) -> int:
    try:
        row = store.db.execute(
            "SELECT MAX(id) FROM messages WHERE (room=? OR (sender=? AND (room IS NULL OR room='')))",
            (key, key),
        ).fetchone()
        return int(row[0] or 0) if row else 0
    except Exception:
        return 0


def summarize_idle(store, brain, key: str, name: str, state_path: Path,
                   is_group: bool = False, force: bool = False,
                   min_new: int = 6, limit: int = 40) -> Optional[str]:
    """聊完再总结：把最近 limit 条压成一张短记忆卡，覆盖式写进笔记的「## 最近印象」。

    和 compress() 的区别：compress 是「够 60 条就整理 + 换会话」；这里是「这一段
    对话结束了」就顺手更新印象，不换会话、不追加一堆历史段。
    """
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        state = {}
    last = int(state.get(key, 0) or 0)
    seq = _last_seq(store, key)
    if seq <= last and not force:
        return None
    # 只数上次总结之后的新消息
    try:
        rows = store.db.execute(
            "SELECT COUNT(*) FROM messages WHERE id > ? AND (room=? OR (sender=? AND (room IS NULL OR room='')))",
            (last, key, key),
        ).fetchone()
        new_n = int(rows[0] or 0) if rows else 0
    except Exception:
        new_n = 0
    if new_n < min_new and not force:
        return None

    n_new, max_id, lines = notes_mod.due_since(store, key, state_path.parent, limit=max(limit, 40))
    if n_new == 0 and not force:
        return None
    if n_new < min_new and not force:
        return None
    if not lines:
        return None

    cur = notes_mod.read_note(key)
    old = "\n".join([f"- {a}" for a in cur["arch"]]) or "（无）"
    transcript = "\n".join(lines)
    prompt = (
        "下面是刚聊完的一小段微信对话（\"我\"是机器人小鱼），每行开头的 [MM-DD] 是这条消息的日期。"
        "请更新这个人的笔记，**只输出这两种行**，不要解释：\n"
        "档案|一句话（新发现的、长期不变的事实：称呼/性格/忌讳/在意的事；没有就不输出）\n"
        "时间线|MM-DD|一件事（这次聊到的事，最多 3 行）\n"
        "规则：日期必须用记录里真实出现过的 [MM-DD]，不许用今天；只写记录里确实出现的，不猜不编；"
        "不要复述原话；每条不超 40 字。\n\n"
        f"已有档案：\n{old}\n\n对话：\n{transcript}"
    )
    try:
        out, _sid = brain.ask(prompt)
    except Exception as e:
        log.warning("总结 %s 的印象失败：%r", name, e)
        return None

    arch, tl = notes_mod.parse_model_lines(out or "")
    if not arch and not tl:
        return None
    covered = time.strftime("%Y-%m-%d %H:%M")
    count = cur["meta"] or ""
    path = notes_mod.append_new(key, arch[:2], tl[:3], covered, n_new)
    notes_mod.set_watermark(state_path.parent, key, max_id)
    log.info("聊完总结：%s → %s（新增 %d 条，档案 +%d，时间线 +%d）", name, path.name, n_new, len(arch[:2]), len(tl[:3]))
    return out


def compress(store, brain, key: str, name: str, state_path: Path,
             every: int = EVERY, force: bool = False,
             is_group: bool = False) -> Optional[str]:
    """够了就整理一次，然后换新会话。

    笔记只保留一套格式（## 档案 / ## 时间线，按日期追加）。原来这里会往文件末尾
    追加 `## 记忆整理 时间` 卡片，但 summarize_idle 走 append_new 是整篇重写，
    两套格式会互相覆盖（旧笔记里就躺着两段重复卡片）。现在统一走增量路径。
    """
    grew = due(store, key, state_path, every)
    if not force and grew < every:
        return None

    summary = None
    try:
        summary = summarize_idle(store, brain, key, name, state_path,
                                 is_group=is_group, force=True, min_new=1,
                                 limit=KEEP_RECENT)
    except Exception as e:
        log.warning("整理 %s 的上下文失败：%r", name, e)
        return None

    total = store.db.execute(
        "SELECT COUNT(*) FROM messages WHERE (room=? OR (sender=? AND (room IS NULL OR room='')))", (key, key)).fetchone()[0]
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        state = {}
    state[key] = total
    state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

    store.reset_session(key)
    log.info("已整理 %s 的上下文（%d 条），并换了新会话", name, total)
    return summary


def catch_up(store, brain, key: str, name: str, state_path: Path,
             is_group: bool = False, chunk: int = 60,
             max_rounds: int = 12, pause: float = 0.8) -> int:
    """把水位之后积压的消息分轮补齐（每轮 chunk 条），返回实际整理的轮数。

    每轮只把水位往前推 chunk 条，模型看见的永远是"一小段"，不会一次喂几百条。
    """
    rounds = 0
    for _ in range(max_rounds):
        n_new, _max_id, _lines = notes_mod.due_since(store, key, state_path.parent, limit=chunk)
        if n_new <= 0:
            break
        out = summarize_idle(store, brain, key, name, state_path,
                             is_group=is_group, force=True, min_new=1, limit=chunk)
        if out is None:          # 模型没给可解析的东西就停，别原地打转
            log.warning("补齐 %s 中断：这一轮没整理出内容", name)
            break
        rounds += 1
        time.sleep(pause)
    return rounds
