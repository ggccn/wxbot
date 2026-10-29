"""不装微信也能联调：控制台里假装自己是微信好友。"""
from __future__ import annotations

import itertools
import time

from bridge import Msg


class MockAdapter:
    def __init__(self, wxid: str = "wxid_mock_owner", name: str = "你", verbose: bool = True):
        self.wxid = wxid
        self.name = name
        self.verbose = verbose
        self._ids = itertools.count(1)

    def make(self, text: str, sender: str = "", alias: str = "", room: str = "",
             at_me: bool = False) -> Msg:
        return Msg(
            msgid=f"mock-{int(time.time() * 1000)}-{next(self._ids)}",
            sender=sender or self.wxid,
            room=room,
            text=text,
            at_me=at_me,
            is_group=bool(room),
            alias=alias or self.name,
        )

    def send(self, target: str, text: str, aters: str = "") -> bool:
        if self.verbose:
            print(f"\n[小助手 → {target}]{' @' + aters if aters else ''} {text}\n")
        return True
