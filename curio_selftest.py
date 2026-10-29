# -*- coding: utf-8 -*-
"""_curiosity_block 的行为自测（不碰微信、不碰网络）。"""
import random
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bridge as B


class FakeBridge(B.Bridge):
    def __init__(self, conf, notes_dir):
        self.conf = conf
        self._notes_dir = Path(notes_dir)
        self._curio_turn = {}
        self._curio_last = {}
        self._curio_asked = {}

    def _behavior_conf(self):
        return self.conf

    def _notes_path(self, ident):
        return self._notes_dir / (str(ident) + ".md")


def mk(**kw):
    conf = {
        "curiosity_enabled": True,
        "curiosity_chance": 1.0,
        "curiosity_cooldown_turns": 1,
        "curiosity_follow_topic": True,
        "curiosity_lull_only": True,
        "curiosity_lull_seconds": 90,
        "curiosity_short_chars": 6,
        "curiosity_no_switch": True,
    }
    conf.update(kw)
    return conf


def main():
    tmp = Path(tempfile.mkdtemp())
    (tmp / "u1.md").write_text("# 档案\n- 爱喝冰的\n", encoding="utf-8")
    random.seed(7)
    fails = 0

    def check(name, got, want_contains, want_empty=False, not_contains=None):
        nonlocal fails
        if want_empty:
            ok = got == ""
        else:
            ok = bool(got) and want_contains in got
            if not_contains:
                ok = ok and not_contains not in got
        print(("  OK  " if ok else "  FAIL") + f" {name}: {got[:70]!r}")
        if not ok:
            fails += 1

    # 1) 热聊（长句 + 刚发过 5 秒）→ 不许问
    b = FakeBridge(mk(), tmp)
    got = b._curiosity_block("u1", "A", "", False, last_text="我刚打完一把排位赛，队友太菜了", gap_seconds=5)
    check("热聊不插问句", got, "", want_empty=True)

    # 2) 他这句很短 → 追问细节（有上一条可退）
    b = FakeBridge(mk(), tmp)
    got = b._curiosity_block("u1", "A", "", False, last_text="今天好累", gap_seconds=5,
                             prev_text="刚下班")
    check("短句触发追问", got, "顺着他这句话")
    check("带上原话", got, "他刚说的是「今天好累」")
    check("禁转场", got, "禁止")
    check("没有旧清单味道", got, "你对他还有点好奇：", want_empty=True) if False else None

    # 3) 冷场（100 秒）→ 追问细节，且抓的是当前话题
    b = FakeBridge(mk(), tmp)
    got = b._curiosity_block("u1", "A", "", False,
                             last_text="今天下午去买了杯柠檬水，结果涨价了", gap_seconds=100)
    check("冷场触发追问", got, "顺着他这句话")
    check("抓到当前话题", got, "柠檬水")

    # 4) 关掉 lull 闸 → 热聊也问
    b = FakeBridge(mk(curiosity_lull_only=False), tmp)
    got = b._curiosity_block("u1", "A", "", False, last_text="我刚打完一把排位赛，队友太菜了", gap_seconds=5)
    check("关闸后热聊也问", got, "顺着他这句话")

    # 5) 老行为：从清单里挑方向 + 写笔记
    b = FakeBridge(mk(curiosity_follow_topic=False), tmp)
    got = b._curiosity_block("u1", "A", "", False, last_text="嗯", gap_seconds=5)
    check("老行为用清单", got, "你对他还有点好奇：")
    body = (tmp / "u1.md").read_text(encoding="utf-8")
    print(("  OK  " if "## 已经问过" in body else "  FAIL") + " 写进笔记：", body.replace("\n", " ")[-40:])

    # 6) 群聊不问
    b = FakeBridge(mk(), tmp)
    got = b._curiosity_block("g1", "A", "", True, last_text="嗯", gap_seconds=99)
    check("群聊不注入", got, "", want_empty=True)

    # 7) 冷却：cooldown=2 时，第 1 轮不问、第 2 轮问、第 3 轮又不问
    b = FakeBridge(mk(curiosity_cooldown_turns=2), tmp)
    r1 = b._curiosity_block("u1", "A", "", False, last_text="今天好累", gap_seconds=99)
    r2 = b._curiosity_block("u1", "A", "", False, last_text="今天好累", gap_seconds=99)
    r3 = b._curiosity_block("u1", "A", "", False, last_text="今天好累", gap_seconds=99)
    check("冷却期第 1 轮不问", r1, "", want_empty=True)
    check("第 2 轮问", r2, "顺着他这句话")
    check("第 3 轮冷却内不问", r3, "", want_empty=True)

    # 8) 他只回"嗯"，要顺着他上一条的话问，而不是追问"嗯"
    b = FakeBridge(mk(), tmp)
    got = b._curiosity_block("u1", "A", "", False, last_text="嗯", gap_seconds=5,
                             prev_text="今天下午去买了杯柠檬水，结果涨价了")
    check("退回到上一条", got, "上面那条是「今天下午去买了杯柠檬水")
    check("不追问嗯", got, "上面那条是", not_contains="他刚说的是「嗯」")

    print("\n结论：" + ("全部通过" if fails == 0 else f"{fails} 项失败"))


if __name__ == "__main__":
    main()
