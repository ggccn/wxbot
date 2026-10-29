"""维护工具：把群笔记的头部（群名/成员）同步成微信里的现状，正文一个字都不动。

用法: .venv\\Scripts\\python.exe sync_group_notes.py
"""
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(BASE_DIR))
from wechatauto.db import WeChatDB
from paths import NOTES_DIR, BASE_DIR

NOTES = NOTES_DIR
HEAD_RE = re.compile(r"\A<!-- wxid:[^\n]*-->\s*\n# [^\n]*\n\n群名：[^\n]*\n成员：[^\n]*\n\n?", re.S)


def sync(gid: str, name: str, members: list[str], f: Path) -> str:
    body = f.read_text(encoding="utf-8", errors="replace")
    body = HEAD_RE.sub("", body).lstrip("\n")
    head = (
        f"<!-- wxid: {gid} | group: {name} | synced: {time.strftime('%Y-%m-%d %H:%M')} -->\n"
        f"# {name}\n\n"
        f"群名：{name}（{len(members)} 人）\n"
        f"成员：{'、'.join(members) if members else '（还没读到）'}\n\n"
    )
    f.write_text(head + body, encoding="utf-8")
    return f"{gid} -> {name}（{len(members)} 人）{f.stat().st_size}B"


def main() -> int:
    db = WeChatDB()
    n = 0
    for f in sorted(NOTES.glob("*@chatroom.md")):
        gid = f.stem
        try:
            name = db.group_id_to_name(gid) or gid
            members = [m.get("nick_name") or m.get("username") for m in (db.get_group_members(gid) or [])]
        except Exception as e:
            print("跳过", gid, repr(e))
            continue
        print(sync(gid, name, members, f))
        n += 1
    print("同步完成，共", n, "个群笔记")
    return 0


if __name__ == "__main__":
    sys.exit(main())
