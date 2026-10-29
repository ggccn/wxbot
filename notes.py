"""笔记的读写与增量合并。

笔记文件格式（固定三段，便于机器增量维护）：

    # 每个联系人/群一份，文件名用 wxid 或群 id
    <!-- 整理于 2026-10-26 00:12 ｜ 覆盖到 2026-10-25 23:50 ｜ 297 条 -->

    ## 档案
    - 称呼：你
    - 印象：话短，爱发表情，常让我猜图

    ## 时间线
    - 10-24：他妈妈加了我好友，他嫌我说话太凶
    - 10-25：约好周末去吃烤肉

「档案」放长期不变的结论（称呼/性格/忌讳/群成员），「时间线」按日期追加事件。
每次整理只处理上次水位之后的新消息，追加 1~3 行，不再整篇重写。
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Iterable, Optional
from paths import NOTES_DIR

NOTES_DIR = NOTES_DIR
ARCH = "## 档案"
TL = "## 时间线"
_META_RE = re.compile(r"<!--\s*整理于\s*([^｜|]*?)\s*[｜|]\s*覆盖到\s*([^｜|]*?)\s*[｜|]\s*(\d+)\s*条\s*-->")


def path_of(key: str) -> Path:
    return NOTES_DIR / (key if key.endswith(".md") else f"{key}.md")


def wm_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / "note_watermark.json"


def _load_wm(data_dir) -> dict:
    p = wm_path(data_dir)
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def get_watermark(data_dir, key: str) -> int:
    return int(_load_wm(data_dir).get(key, 0) or 0)


def set_watermark(data_dir, key: str, msg_id: int) -> None:
    wm = _load_wm(data_dir)
    wm[key] = int(msg_id)
    p = wm_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(wm, ensure_ascii=False, indent=0), encoding="utf-8")


def read_note(key: str) -> dict:
    p = path_of(key)
    if not p.exists():
        return {"title": "", "meta": "", "arch": [], "timeline": [], "extra": []}
    text = p.read_text(encoding="utf-8")
    title, meta = "", ""
    arch: list[str] = []
    timeline: list[str] = []
    extra: list[str] = []          # 其它段落（## 已经问过 等）原样保留，重写时不丢
    bucket = None
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("# ") and not title:
            title = s[2:].strip()
            continue
        m = _META_RE.search(s)
        if m:
            meta = s
            continue
        if s.startswith(ARCH):
            bucket = "arch"
            continue
        if s.startswith(TL):
            bucket = "tl"
            continue
        if s.startswith("## "):
            bucket = "extra"
            extra.append(s)
            continue
        if s.startswith("- ") and bucket == "arch":
            arch.append(s[2:].strip())
        elif bucket == "tl" and s.startswith("- "):
            timeline.append(s[2:].strip())
        elif bucket == "extra":
            extra.append(s)
    return {"title": title, "meta": meta, "arch": arch, "timeline": timeline, "extra": extra}


def write_note(key: str, title: str, arch: Iterable[str], timeline: Iterable[str],
               covered_to: str, count: int, extra: Iterable[str] = ()) -> Path:
    path = path_of(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    now = time.strftime("%Y-%m-%d %H:%M")
    meta = f"<!-- 整理于 {now} ｜ 覆盖到 {covered_to} ｜ {int(count)} 条 -->"
    lines = [f"# {title}", meta, "", ARCH] + [f"- {x}" for x in arch if str(x).strip()] + ["", TL] + \
            [f"- {x}" for x in timeline if str(x).strip()]
    extra_lines = [str(x).strip() for x in extra if str(x).strip()]
    if extra_lines:
        lines += [""] + extra_lines
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def append_new(key: str, new_arch: Iterable[str], new_lines: Iterable[str],
               covered_to: str, count: int) -> Path:
    """增量合并：档案去重追加、时间线追加，重写 meta；其它段落原样保留。"""
    cur = read_note(key)
    arch = list(cur["arch"])
    seen = {a.strip() for a in arch}
    for a in new_arch:
        a = str(a).strip()
        if a and a not in seen:
            arch.append(a)
            seen.add(a)
    tl = list(cur["timeline"]) + [str(x).strip() for x in new_lines if str(x).strip()]
    title = cur["title"] or key
    return write_note(key, title, arch, tl, covered_to, count, extra=cur["extra"])


def due_since(store, key: str, data_dir, limit: int = 60) -> tuple[int, int, list[str]]:
    """返回 (新消息数, 最新 id, 水位之后的消息文本行)。

    每条行首带上 [MM-DD]（由消息 ts 生成），模型只能从这几个日期里挑，
    不能靠"今天"猜——补历史时才不会全写成同一个日期。
    """
    wm = get_watermark(data_dir, key)
    rows = store.db.execute(
        "SELECT id, direction, content, ts FROM messages "
        "WHERE id > ? AND (room=? OR (sender=? AND (room IS NULL OR room=''))) ORDER BY id ASC LIMIT ?",
        (wm, key, key, limit),
    ).fetchall()
    lines = []
    for _id, direction, content, ts in rows:
        who = "我" if direction == "out" else "对方"
        c = (content or "").replace("\n", " ")
        if c.startswith("[") and "文件" in c:
            c = "[图]"
        day = time.strftime("%m-%d", time.localtime(ts or 0))
        lines.append(f"[{day}] {who}：{c[:80]}")
    max_id = max((r[0] for r in rows), default=wm)
    return len(rows), int(max_id), lines


def parse_model_lines(text: str) -> tuple[list[str], list[str]]:
    """模型输出解析：`档案|…` / `时间线|MM-DD|事件`（也容忍 `MM-DD：事件`、`- ` 开头）。"""
    arch, tl = [], []
    for raw in (text or "").splitlines():
        s = raw.strip().lstrip("-").strip()
        s = re.sub(r"^\[\d{1,2}-\d{1,2}\]\s*", "", s)     # 容忍模型把记录里的 [10-25] 抄回来
        if not s:
            continue
        if s.startswith("档案|") or s.startswith("档案："):
            v = s.split("|", 1)[-1].split("：", 1)[-1].strip()
            if v and not re.search(r"没有出现|未提及|无记录|不适用|记录未提|没发言", v):
                arch.append(v)
        elif s.startswith("时间线|") or s.startswith("时间线："):
            v = s.split("|", 1)[-1].split("：", 1)[-1].strip()
            v = re.sub(r"^(\d{1,2}-\d{1,2})[：:|\s]+", r"\1：", v)
            if v:
                tl.append(v)
        elif re.match(r"^\d{1,2}-\d{1,2}", s):
            tl.append(re.sub(r"^(\d{1,2}-\d{1,2})[：:|\s]+", r"\1：", s))
    return [a for a in arch if a], [t for t in tl if t]


def stamp_missing_dates(timeline: list[str], days: list[str]) -> list[str]:
    """给没写日期的条目按顺序补上日期（days 是记录里出现过的 MM-DD，升序）。"""
    if not days:
        return timeline
    out = []
    n = max(1, len(timeline))
    for i, line in enumerate(timeline):
        if re.match(r"^\d{1,2}-\d{1,2}", line):
            out.append(line)
            continue
        idx = min(len(days) - 1, int(i * len(days) / n))
        out.append(f"{days[idx]}：{line}")
    return out


def merge_from_model(key: str, model_text: str, covered_to: str, count: int) -> Optional[Path]:
    arch, tl = parse_model_lines(model_text)
    if not arch and not tl:
        return None
    return append_new(key, arch[:4], tl[:4], covered_to, count)
