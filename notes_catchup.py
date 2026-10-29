r"""把笔记水位之后积压的消息补进「## 时间线」（只追加，按真实日期）。

用法：
    python notes_catchup.py              # 只补有积压的会话
    python notes_catchup.py 张三          # 只补名字/备注里含这个词的
    python notes_catchup.py --all        # 所有有聊天记录的会话（含已追平的）
    python notes_catchup.py --dry        # 只报告积压，不动文件

每轮最多喂 60 条给模型，一轮一次调用；日期由消息 ts 生成（[MM-DD]），
不再靠模型猜"今天"。旧的 `## 记忆整理 时间` 段落会先被挪到
notes\_archive\<key>_记忆整理.md，避免两套格式互相覆盖。
"""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(BASE_DIR))

import memory  # noqa: E402
import notes  # noqa: E402
from brain import Brain  # noqa: E402
from store import Store  # noqa: E402
from paths import HOME_DIR, DATA_DIR, BASE_DIR, ARCHIVE_DIR
import os

DATA = DATA_DIR
DSH = os.environ.get("DSH_CMD", "dsh")
HOME = str(HOME_DIR)
ARCHIVE = ARCHIVE_DIR
STATE = DATA / "memory_state.json"
CHUNK = 60


def strip_legacy(key: str, name: str) -> int:
    """把 `## 记忆整理 …` 段落挪到归档文件，正文里删掉。返回挪走的块数。"""
    p = notes.path_of(key)
    if not p.exists():
        return 0
    text = p.read_text(encoding="utf-8")
    keep: list[str] = []
    moved: list[list[str]] = []
    cur: list[str] | None = None
    for ln in text.splitlines():
        if ln.startswith("## 记忆整理"):
            cur = [ln]
            moved.append(cur)
            continue
        if cur is not None and ln.startswith("## "):
            cur = None
        if cur is not None:
            cur.append(ln)
            continue
        keep.append(ln)
    if not moved:
        return 0
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    ap = ARCHIVE / f"{key}_记忆整理.md"
    with open(ap, "a", encoding="utf-8") as f:
        for block in moved:
            f.write(f"\n<!-- 于 {time.strftime('%Y-%m-%d %H:%M')} 从笔记迁出：{name} -->\n")
            f.write("\n".join(x for x in block if x.strip()) + "\n")
    p.write_text("\n".join(keep).rstrip() + "\n", encoding="utf-8")
    return len(moved)


def main() -> int:
    argv = [a for a in sys.argv[1:]]
    show_all = "--all" in argv
    dry = "--dry" in argv
    filt = next((a for a in argv if not a.startswith("-")), "")

    store = Store(str(DATA / "wechat.db"))
    brain = None if dry else Brain(DSH, HOME, profile="headless", timeout=300)

    alias = {}
    for wxid, a in store.db.execute("SELECT wxid, alias FROM contacts").fetchall():
        if wxid:
            alias[wxid] = (a or "").strip()

    wm = notes._load_wm(DATA)
    keys = list(wm.keys())
    # 有聊天记录但从来没记过的也算上
    for (k,) in store.db.execute("SELECT DISTINCT room FROM messages WHERE room IS NOT NULL AND room<>''"):
        if k not in keys:
            keys.append(k)

    rows = []
    for key in keys:
        name = alias.get(key) or key
        if filt and filt not in key and filt not in name:
            continue
        n_new, _max_id, _lines = notes.due_since(store, key, DATA, limit=10_000)
        rows.append((n_new, key, name))
    rows.sort(reverse=True)

    print(f"{'会话':<16}{'待补':>7}  状态")
    total_new = 0
    for n_new, key, name in rows:
        if n_new == 0 and not show_all:
            continue
        total_new += n_new
        print(f"{name:<16}{n_new:>7}  {'（dry，不动）' if dry else '准备补'}")
    if not rows or total_new == 0 and not show_all:
        print("没有积压 ✅")
        return 0
    if dry:
        print(f"合计 {total_new} 条待补（--dry，未改动）")
        return 0

    started = time.time()
    done = 0
    for n_new, key, name in rows:
        if n_new == 0 and not show_all:
            continue
        moved = strip_legacy(key, name)
        if moved:
            print(f"  迁移 {name} 的 {moved} 段旧记忆卡 → _archive")
        t0 = time.time()
        rounds = memory.catch_up(store, brain, key, name, STATE,
                                 is_group=key.endswith("@chatroom"), chunk=CHUNK)
        left, _mx, _l = notes.due_since(store, key, DATA, limit=10_000)
        done += 1
        print(f"✓ {name:<14} {n_new:>4} 条 / {rounds} 轮 / 剩 {left} 条  {time.time()-t0:.0f}s")
    print(f"完成 {done} 个会话，用时 {time.time()-started:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
