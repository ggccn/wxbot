"""一次性全量重建笔记（由「我」负责整理，不是她边聊边记）。

- 每人/每群读全部历史（最多 400 条），产出「## 档案」+「## 时间线（带日期）」；
- 写回 notes/<key>.md，并把水位（note_watermark.json）设到最后一条消息 id；
- 之后 bridge 的 idle_tick 只处理水位之后的新消息，按日期追加 1~3 行。
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(BASE_DIR))

import notes  # noqa: E402
from brain import Brain  # noqa: E402
from store import Store  # noqa: E402
from paths import HOME_DIR, DATA_DIR, BASE_DIR
import os

DATA = DATA_DIR
DSH = os.environ.get("DSH_CMD", "dsh")
HOME = str(HOME_DIR)
LIMIT = 400

PROMPT = """你是一个只做事实整理的助手。下面是微信聊天记录（"我"是机器人小鱼，"对方"是人）。

请把它整理成两段，**只输出这两种行**，不要解释、不要客套：

档案|一句话（长期不变的：怎么称呼、性格、在意的事、忌讳；最多 6 行；不要写具体事件）
时间线|MM-DD|一件事（按时间先后，最多 8 行；每行必须以 时间线|MM-DD| 开头）

规则：
1. 只写记录里出现过的，不许猜、不许编；
2. 档案写长期有效的结论，时间线写具体事件（谁做了什么、约定了什么、发生了什么）；
3. 日期用记录里的日期（MM-DD），没有确切日期就写最近的日期；
4. 不复述原话，每条不超过 40 字。

已有笔记（可以参考、可以纠正，别照抄）：
{old}

聊天记录：
{transcript}
"""

GPROMPT = """你是一个只做事实整理的助手。下面是微信群聊天记录（"我"是机器人小鱼）。

只输出这两种行，不要解释：

档案|一句话（群名/气氛；成员写「名字：一句话」；忌讳；最多 8 行；不要写具体事件）
时间线|MM-DD|一件事（按时间先后，最多 8 行；每行必须以 时间线|MM-DD| 开头）

规则：只写记录里出现的；成员写"名字：一句话"；不猜不编；每条不超过 40 字。

已有笔记（可参考）：
{old}

聊天记录：
{transcript}
"""


def transcript(store: Store, key: str, limit: int = LIMIT) -> tuple[str, str, int, int]:
    names = {}
    try:
        for w, a in store.db.execute("SELECT wxid, alias FROM contacts").fetchall():
            if w and (a or "").strip():
                names[w] = a.strip()
    except Exception:
        pass
    rows = store.db.execute(
        "SELECT id, direction, sender, content, ts FROM ("
        "  SELECT id, direction, sender, content, ts FROM messages "
        "  WHERE (room=? OR (sender=? AND (room IS NULL OR room=''))) ORDER BY id DESC LIMIT ?"
        ") ORDER BY id ASC",
        (key, key, limit),
    ).fetchall()
    lines = []
    for _id, direction, sender, content, ts in rows:
        who = "我" if direction == "out" else names.get(sender, "对方")
        c = (content or "").replace("\n", " ")
        if c.startswith("[") and "文件" in c:
            c = "[图/表情]"
        day = time.strftime("%m-%d", time.localtime(ts or 0))
        lines.append(f"[{day}] {who}：{c[:70]}")
    total = store.db.execute(
        "SELECT COUNT(*) FROM messages WHERE (room=? OR (sender=? AND (room IS NULL OR room='')))",
        (key, key),
    ).fetchone()[0]
    max_id = store.db.execute(
        "SELECT MAX(id) FROM messages WHERE (room=? OR (sender=? AND (room IS NULL OR room='')))",
        (key, key),
    ).fetchone()[0] or 0
    covered = time.strftime("%Y-%m-%d %H:%M") if rows[-1:] else time.strftime("%Y-%m-%d %H:%M")
    return "\n".join(lines), covered, int(total), int(max_id)


def main() -> int:
    store = Store(str(DATA / "wechat.db"))
    brain = Brain(DSH, HOME, profile="headless", timeout=300)

    targets: list[tuple[str, str, bool]] = []
    for wxid, alias in store.db.execute("SELECT wxid, alias FROM contacts").fetchall():
        if not wxid or wxid == "filehelper" or wxid.startswith("filehelper"):
            continue
        if (alias or "").strip() in ("", "文件传输助手"):
            continue
        targets.append((wxid, (alias or wxid).strip(), wxid.endswith("@chatroom")))

    only = sys.argv[1] if len(sys.argv) > 1 else ""
    print(f"待整理 {len(targets)} 个")
    for key, name, is_group in targets:
        if only and only not in key and only not in name:
            continue
        tr, covered, total, max_id = transcript(store, key)
        if len(tr) < 40:
            print(f"— 跳过 {name}（记录太少）")
            continue
        cur = notes.read_note(key)
        old = "\n".join(
            [f"- {a}" for a in cur["arch"]] + [f"- {t}" for t in cur["timeline"]]
        ) or "（无）"
        p = (GPROMPT if is_group else PROMPT).format(old=old, transcript=tr)
        t0 = time.time()
        try:
            out, _sid = brain.ask(p, None)
        except Exception as e:
            print(f"✗ {name} 失败：{e!r}")
            continue
        arch, tl = notes.parse_model_lines(out)
        days = sorted({m for m in re.findall(r"^\[(\d{2}-\d{2})\]", tr, re.M)})
        tl = notes.stamp_missing_dates(tl, days)
        if not arch and not tl:
            print(f"✗ {name} 输出无法解析：{(out or '')[:80]!r}")
            continue
        path = notes.write_note(key, name, arch, tl, covered, total, extra=cur["extra"])
        notes.set_watermark(DATA, key, max_id)
        print(f"✓ {name:14s} 档案 {len(arch)} 条 / 时间线 {len(tl)} 条  "
              f"{len(path.read_text(encoding='utf-8'))}B  {time.time()-t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
