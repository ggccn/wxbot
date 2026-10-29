"""给「大脑」用的小工具：让小鱼真的能动手操作微信。

用法（在 E:\\wxbot_home 里跑，路径写成绝对路径）：
    E:\\dsk\\wxbot\\wxctl.cmd accept            同意所有待处理的好友申请
    E:\\dsk\\wxbot\\wxctl.cmd accept --dry      只看有哪些申请，不动手
    E:\\dsk\\wxbot\\wxctl.cmd poke 张三          拍一拍某人
    E:\\dsk\\wxbot\\wxctl.cmd send 张三 你好      给某人/某群发一条文字
    E:\\dsk\\wxbot\\wxctl.cmd sticker 哈哈       发一个表情（标签先 list 看）
    E:\\dsk\\wxbot\\wxctl.cmd list               列出表情库标签

每次最多干一件事，输出一行 JSON，方便大脑读懂结果。
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
os.environ.setdefault("WECHATAUTO_RHYTHM", "fast")

# 桥常驻时也在解微信数据库；两个进程共用 %TEMP% 下的解密目录会撞车
# （os.replace -> PermissionError）。给 wxctl 一个自己的临时目录。
_TMP = os.path.join(ROOT, "data", "tmp_wxctl")
os.makedirs(_TMP, exist_ok=True)
os.environ["TMP"] = os.environ["TEMP"] = _TMP
import tempfile

tempfile.tempdir = _TMP


def _adapter():
    from adapters.wxa_adapter import WxaAdapter

    return WxaAdapter()


def _out(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False))


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    cmd = argv[1].lower()

    if cmd == "list":
        import stickers

        _out({"ok": True, "tags": stickers.tags()})
        return 0

    a = _adapter()

    if cmd == "accept":
        dry = "--dry" in argv
        names = [x for i, x in enumerate(argv) if i >= 2 and argv[i - 1] == "--name"]
        limit = int(os.environ.get("WXCTL_ACCEPT_LIMIT", "5"))
        res = a.accept_friends(limit=limit, dry_run=dry, names=names or None)
        _out({"ok": True, "dry": dry, "names": names or "全部", **res})
        return 0

    if cmd == "poke":
        if len(argv) < 3:
            _out({"ok": False, "err": "用法：wxctl poke 名字"})
            return 2
        ok = bool(a.poke(argv[2]))
        _out({"ok": ok, "who": argv[2]})
        return 0 if ok else 1

    if cmd == "send":
        if len(argv) < 4:
            _out({"ok": False, "err": "用法：wxctl send 名字 内容"})
            return 2
        text = " ".join(argv[3:])
        ok = bool(a.send(argv[2], text))
        _out({"ok": ok, "who": argv[2], "text": text[:60]})
        return 0 if ok else 1

    if cmd == "sticker":
        if len(argv) < 3:
            _out({"ok": False, "err": "用法：wxctl sticker 标签 [给谁]"})
            return 2
        import stickers

        path = stickers.pick(argv[2])
        if not path:
            _out({"ok": False, "err": "没有这个标签", "tags": stickers.tags()})
            return 1
        who = argv[3] if len(argv) > 3 else (os.environ.get("WXCTL_TO") or "")
        if not who:
            _out({"ok": False, "err": "要指定发给谁：wxctl sticker 标签 名字"})
            return 2
        ok = bool(a.send_image(path, who))
        _out({"ok": ok, "tag": argv[2], "who": who, "file": os.path.basename(path)})
        return 0 if ok else 1

    _out({"ok": False, "err": "不认识的命令：%s" % cmd})
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
