"""动作注册表：以后加新能力，只写一个函数 + @action 就行，不用改 bridge。

模型在回复里写 `[[动作:名字|对象|内容]]`，bridge 会查这张表并执行。
- name/aliases：模型可以说中文或英文
- desc：会被拼进系统提示，告诉模型"你还能干这些"
- ready=False：登记了但还没接上（回复里会如实说"这个我还不会"）
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Optional

log = logging.getLogger("actions")


@dataclass
class Entry:
    name: str
    fn: Callable[..., tuple[bool, str]]
    desc: str
    ready: bool = True
    aliases: tuple[str, ...] = ()


REGISTRY: dict[str, Entry] = {}
ORDER: list[Entry] = []


def action(name: str, aliases: tuple[str, ...] = (), desc: str = "",
           ready: bool = True):
    def deco(fn):
        entry = Entry(name=name, fn=fn, desc=desc, ready=ready, aliases=aliases)
        ORDER.append(entry)
        for n in (name, *aliases):
            REGISTRY[n.lower()] = entry
        return fn
    return deco


def resolve(name: str) -> Optional[Entry]:
    return REGISTRY.get((name or "").strip().lower())


def run(name: str, hint: str, payload: str, ctx: dict) -> tuple[bool, str]:
    entry = resolve(name)
    if not entry:
        return False, f"我不认识「{name}」这个动作。"
    if not entry.ready:
        return False, "这个我还没接上，先记在本子上了。"
    try:
        return entry.fn(hint=hint, payload=payload, ctx=ctx)
    except Exception as e:
        log.exception("动作 %s 执行失败", entry.name)
        return False, f"动手的时候出错了：{e}"


def describe(ready_only: bool = False) -> str:
    lines = []
    for e in ORDER:
        if ready_only and not e.ready:
            continue
        flag = "" if e.ready else "（未启用）"
        lines.append(f"- {e.name}{flag}：{e.desc}")
    return "\n".join(lines)


# ---------------------------------------------------------------- 已接上的动作

@action("发消息", aliases=("send", "私聊", "告诉"), desc="给某个人/群发一条文字：[[动作:发消息|名字|正文]]")
def _send(hint: str, payload: str, ctx: dict) -> tuple[bool, str]:
    send = ctx["bridge"]._safe_send
    who = (hint or "").strip()
    body = payload or ""
    if not body.strip():
        return False, "要发的内容是空的。"
    if not who:
        # 以前这里会退化成"发给当前聊天窗口"，于是"回复 Z" 发到了你身上。
        # 宁可不动，也不猜收件人。
        log.warning("动作[发消息]没写收件人，拒绝执行（避免发错人）")
        return False, "没说要发给谁，我不乱发。"
    target = ctx["resolve"](who) or who
    ok = send(target, body)
    return bool(ok), ("发了" if ok else "没发出去")


@action("表情", aliases=("sticker", "发表情"), desc="按情绪发表情：[[动作:表情|当前人|标签]]，标签见表情库")
def _sticker(hint: str, payload: str, ctx: dict) -> tuple[bool, str]:
    tag = (payload or hint or "").strip() or "可爱"
    ok = ctx["bridge"]._safe_send_sticker(ctx["target"], tag)
    return bool(ok), ("表情发出去了" if ok else "表情没发出去")


@action("戳", aliases=("poke", "拍一拍", "戳一戳"), desc="戳一下某人：[[动作:戳|名字|]]")
def _poke(hint: str, payload: str, ctx: dict) -> tuple[bool, str]:
    poke = ctx["bridge"].poke
    if not poke:
        return False, "戳一戳这个我这边没接上。"
    who = ctx["resolve"](hint) or hint or ctx["name"]
    ok = poke(who)
    return bool(ok), ("戳了" if ok else "戳不动，可能窗口被挡住了")


@action("同意好友", aliases=("同意", "accept"), desc="通过好友申请：[[动作:同意好友|名字|]]")
def _accept(hint: str, payload: str, ctx: dict) -> tuple[bool, str]:
    fn = (ctx["bridge"].actions or {}).get("accept_friends")
    if not fn:
        return False, "同意好友这个我这边没接上。"
    res = fn(limit=3, names={hint} if hint else None) or {}
    if res.get("ambiguous"):
        return False, "对得上好几个名字，你说准点。"
    n = res.get("accepted", 0)
    return n > 0, (f"通过了 {n} 个" if n else "没找到对得上的申请")


@action("存表情", aliases=("收藏表情", "save_sticker"),
        desc="把对方刚发的图/表情存进表情库：[[动作:存表情|标签|]]")
def _save_sticker(hint: str, payload: str, ctx: dict) -> tuple[bool, str]:
    lib = ctx["bridge"].stickers
    if not lib:
        return False, "表情库没加载。"
    tag = (payload or hint or "收到").strip()
    src = ctx.get("media") or ""
    if not src:
        return False, "这条消息里没有能存的图。"
    p = lib.add_file(src, tag)
    return (p is not None), (f"存进「{tag}」了" if p else "没存上")


@action("记笔记", aliases=("note", "记下来"),
        desc="把这个人/这个群的事记进笔记：[[动作:记笔记|.|- 名字：印象]]（一次一行，可连写几行）")
def _note(hint: str, payload: str, ctx: dict) -> tuple[bool, str]:
    """往「当前会话」的笔记里追加内容。

    群聊时 ctx["ident"] 是群 id，私聊时是对方 wxid —— 群里认人的结果就落进
    notes/<群id>.md，不会串到某个人的笔记里。
    """
    import time as _t

    from bridge import _notes_path

    text = (payload or "").strip()
    if not text:
        return False, "没写要记什么。"
    ident = ctx.get("ident") or ""
    if not ident:
        return False, "不知道记在谁名下。"
    p = _notes_path(ident)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        old = p.read_text(encoding="utf-8") if p.exists() else ""
        stamp = _t.strftime("%Y-%m-%d %H:%M")
        add = f"\n## 记于 {stamp}\n{text}\n"
        p.write_text((old.rstrip() + add) if old else add.lstrip(), encoding="utf-8")
        return True, "记下了"
        try:
            from bridge import trim_note

            trim_note(p)
        except Exception:
            pass
    except Exception as e:
        return False, f"没记上：{e}"


# ------------------------------------------------------- 登记了、还没接上的动作

@action("加好友", aliases=("add_friend",), ready=False,
        desc="主动加某人为好友：[[动作:加好友|微信号|验证语]]")
def _add_friend(hint: str, payload: str, ctx: dict) -> tuple[bool, str]:
    return False, "加好友还没接上。"


@action("删好友", aliases=("del_friend", "删除好友"), ready=False,
        desc="删掉某个好友：[[动作:删好友|名字|确认]]")
def _del_friend(hint: str, payload: str, ctx: dict) -> tuple[bool, str]:
    return False, "删好友还没接上（这个要二次确认，我不会擅自删人）。"


@action("退群", aliases=("leave_group",), ready=False,
        desc="退出某个群：[[动作:退群|群名|确认]]")
def _leave_group(hint: str, payload: str, ctx: dict) -> tuple[bool, str]:
    return False, "退群还没接上。"


@action("进群", aliases=("join_group",), ready=False,
        desc="加入某个群：[[动作:进群|群名或邀请链接|]]")
def _join_group(hint: str, payload: str, ctx: dict) -> tuple[bool, str]:
    return False, "进群还没接上。"
