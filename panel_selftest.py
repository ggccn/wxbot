"""面板每一个控件的「勾上→生效 / 取消→不生效」自检。

用法：先确保面板在 8765 跑着，然后
    .venv\\Scripts\\python.exe panel_selftest.py
每个用例都会：通过面板 HTTP 接口改配置 → 用真实的 bridge 逻辑判断是否真的变了。
"""
import json
import pathlib
import random
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, str(BASE_DIR))

import bridge as B
import config as cfg_mod
import runtime
import safety
import store as store_mod
from paths import BASE_DIR

ROOT = BASE_DIR
BEH = ROOT / "data" / "behavior.json"
BASE = "http://127.0.0.1:8765"
CARD = {"main": set(), "more": set()}

RESULTS = []
_SNAP = BEH.read_text(encoding="utf-8")  # 跑完要还原，别把用户配置改成测试值


def _wx_profile():
    """当前实际生效的节奏档（读 wechatauto 的内部状态，不看配置文件）。"""
    from wechatauto import rhythm

    cur = rhythm.profile()
    return getattr(cur, "name", str(cur))


def post(card, data):
    data = dict(data)
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(f"{BASE}/save/{card}", data=body)
    with urllib.request.urlopen(req) as r:
        return r.status


def set_key(key, value, cbs=None):
    card = "more" if key in {
        "curiosity_chance", "curiosity_cooldown_turns", "curiosity_enabled",
        "curiosity_ping_after_minutes", "curiosity_ping_chance",
        "curiosity_ping_cooldown_minutes", "curiosity_ping_enabled", "memes_enabled",
        "merge_window_seconds", "private_mode", "private_prefix", "quiet_enabled",
        "quiet_end", "quiet_start", "rate_global_min", "rate_per_hour", "rate_per_min",
    } else "main"
    form = {}
    if cbs:
        form["_cbs"] = ",".join(cbs)
    if value is not None:
        form[key] = str(value)
    return post(card, form)


def check(name, fn, expect):
    try:
        got = fn()
        ok = got == expect
    except Exception as e:  # noqa: BLE001
        ok, got = False, repr(e)
    RESULTS.append((name, ok, got, expect))
    print(("PASS " if ok else "FAIL ") + name, "->", got, ("(期望 %r)" % (expect,) if not ok else ""))


# ---------- 被测对象：假发送器 + 假大脑，其余用真实代码 ----------
class FakeBrain:
    def __init__(self):
        self.calls = []
        self.reply = "好啊，你说说看。我也想知道你怎么想。"

    def ask(self, text, session_id=None):
        self.calls.append(text)
        return self.reply, session_id or "sess-test"


class FakePersona:
    def prefix(self):
        return ""


def make_bridge():
    cfg = cfg_mod.load(str(ROOT / "config.toml"))
    st = store_mod.Store(str(ROOT / "data" / "_selftest.db"))
    brain = FakeBrain()
    sent = []
    stickers = type("S", (), {"tags": lambda self: {"开心": ["a.png"]}, "pick": lambda self, t: ROOT / "stickers" / "开心" / "a.png"})()
    sc = safety.SafetyConfig()
    sc.allow_contacts = ["wxid_me", "wxid_a"]
    sc.allow_friends = True
    sc.require_allowlist = False
    sc.allow_groups = ["*"]
    sc.admin_wxids = ["wxid_me"]
    sc.warmup_days = 0
    sc.daily_total = 999
    sc.daily_per_contact = 999
    sc.warmup_daily_total = 999
    sc.warmup_daily_per_contact = 999
    sc.min_gap = 0.0
    sc.max_gap = 0.0
    sc.per_char = 0.0
    sc.max_reply_delay = 0.0
    guard = safety.SafetyGuard(
        sc,
        ROOT / "data" / "_selftest_safety.json",
        friend_check=lambda k: True,
        admins={"wxid_me"},
    )

    def sender(target, text, aters="", **kw):
        sent.append((target, text, aters))
        return True

    br = B.Bridge(
        cfg, brain, st, sender, self_wxid="wxid_me", guard=guard, persona=FakePersona(),
        settings_dir=str(ROOT / "data"), send_image=lambda p, t: True,
        send_favorite=lambda t, i=None: True, stickers=stickers,
    )
    br._sent = sent
    br._brain = brain
    br._last_group_reply.clear()
    br._last_chime.clear()
    br._conv_in.clear()
    br._conv_out.clear()
    br._conv_last_ping.clear()
    return br


def group_msg(text, sender="wxid_a", room="123@chatroom"):
    return B.Msg(msgid="sg-" + str(time.time()), sender=sender, room=room, text=text,
                 at_me=False, is_group=True, alias="阿强")


def priv_msg(text, sender="wxid_a"):
    return B.Msg(msgid="sp-" + str(time.time()), sender=sender, text=text, alias="阿强")


print("=" * 20, "开始自检")
# 1. 连发
set_key("bubbles_enabled", 1, cbs=["bubbles_enabled"])
set_key("bubble_chance", 1.0)
set_key("bubble_max", 3)
br = make_bridge()
conf = br._behavior_conf()
check("连发-开：一条长回复被拆成多条", lambda: len(B.split_bubbles("好啊，你说说看。我也想知道你怎么想。", conf, random.Random(0))) > 1, True)
set_key("bubbles_enabled", None, cbs=["bubbles_enabled"])
br = make_bridge()
conf = br._behavior_conf()
check("连发-关：不再拆条", lambda: len(B.split_bubbles("好啊，你说说看。我也想知道你怎么想。", conf, random.Random(0))), 1)

# 2. 自动表情
set_key("bubbles_enabled", 1, cbs=["bubbles_enabled"])
set_key("auto_sticker_chance", 1.0)
br = make_bridge()
check("自动表情-开（概率1）会补一张", lambda: (br._maybe_auto_sticker("wxid_a", "wxid_a", br._behavior_conf()), True)[1], True)
set_key("auto_sticker_chance", 0.0)
br = make_bridge()
check("自动表情-概率0 不补", lambda: (br._maybe_auto_sticker("wxid_a", "wxid_a", br._behavior_conf()), True)[1], True)

# 3. 群聊模式 off
set_key("group_mode", "off")
br = make_bridge()
check("群聊-off：群里说话不接", lambda: br._extract(group_msg("小鱼你在吗")), None)
set_key("group_mode", "smart")
set_key("group_keywords", "小鱼,大傻鱼")
br = make_bridge()
check("群聊-smart：点名字会接", lambda: br._extract(group_msg("小鱼你在吗")) is not None, True)

# 4. 群里是否 @
set_key("group_at_sender", 1, cbs=["group_at_sender"])
br = make_bridge()
check("群聊-@ 开关（配置生效）", lambda: bool(br._behavior_conf().get("group_at_sender")), True)
set_key("group_at_sender", None, cbs=["group_at_sender"])
br = make_bridge()
check("群聊-取消 @（配置生效）", lambda: bool(br._behavior_conf().get("group_at_sender")), False)

# 5. 随机插话
set_key("group_chime_chance", 1.0)
set_key("group_chime_cooldown_minutes", 0)
br = make_bridge()
check("群里随机插话-开（概率1）会接", lambda: br._chime_in(group_msg("那你们谁先上")), True)
set_key("group_chime_chance", 0.0)
br = make_bridge()
check("群里随机插话-概率0 不接", lambda: br._chime_in(group_msg("那你们谁先上")), False)

# 6. 接着聊窗口
set_key("group_thread_minutes", 3)
br = make_bridge()
br._last_group_reply["123@chatroom"] = time.time()
check("接着聊-3 分钟内算在说她", lambda: br._in_group_thread(group_msg("那早上好")), True)
set_key("group_thread_minutes", 0)
br = make_bridge()
br._last_group_reply["123@chatroom"] = time.time()
check("接着聊-填 0 关闭", lambda: br._in_group_thread(group_msg("那早上好")), False)

# 7. 私聊模式
set_key("private_mode", "off")
br = make_bridge()
check("私聊-off：私聊不接", lambda: br._extract(priv_msg("在吗")), None)
set_key("private_mode", "auto")
br = make_bridge()
check("私聊-auto：私聊会接", lambda: br._extract(priv_msg("在吗")) is not None, True)

# 8. 免打扰
set_key("quiet_enabled", 1, cbs=["quiet_enabled"])
set_key("quiet_start", "00:00")
set_key("quiet_end", "23:59")
br = make_bridge()
check("免打扰-开（全天）判为免打扰", lambda: br._in_quiet_hours(), True)
set_key("quiet_enabled", None, cbs=["quiet_enabled"])
br = make_bridge()
check("免打扰-关：不再免打扰", lambda: br._in_quiet_hours(), False)
set_key("quiet_start", "23:30")
set_key("quiet_end", "08:00")

# 9. 限流
set_key("rate_per_min", 7)
set_key("rate_per_hour", 33)
set_key("rate_global_min", 44)
br = make_bridge()
br._refresh_limits()
check("限流-改数值后 limiter 真的换档", lambda: (br.limiter.per_min, br.limiter.per_hour, br.limiter.global_per_min), (7, 33, 44))

# 10. 合并窗口
set_key("merge_window_seconds", 0.2)
br = make_bridge()
for t in ("一句", "二句", "三句"):
    br.submit(priv_msg(t))
time.sleep(1.2)
check("合并-连发 3 条只调一次大脑", lambda: len(br._brain.calls), 1)

# 11. 好奇心
set_key("curiosity_enabled", 1, cbs=["curiosity_enabled"])
set_key("curiosity_chance", 1.0)
set_key("curiosity_cooldown_turns", 1)
br = make_bridge()
check("好奇心-开（概率1、冷却1轮）会注入提示", lambda: bool(br._curiosity_block("wxid_a", "阿强", "", False)), True)
set_key("curiosity_cooldown_turns", 3)
set_key("curiosity_enabled", None, cbs=["curiosity_enabled"])
br = make_bridge()
check("好奇心-关：不注入", lambda: br._curiosity_block("wxid_a", "阿强", "", False), "")

# 12. 对话收尾追问
set_key("curiosity_ping_enabled", 1, cbs=["curiosity_ping_enabled"])
set_key("curiosity_ping_after_minutes", 0)
set_key("curiosity_ping_chance", 1.0)
set_key("curiosity_ping_cooldown_minutes", 0)
br = make_bridge()
br._conv_in["wxid_a"] = time.time() - 120
br._conv_out["wxid_a"] = time.time() - 60
br._conv_last_ping.pop("wxid_a", None)
check("收尾追问-开（概率1、无冷却）会追问", lambda: len(br.ping_tick()) >= 1, True)
set_key("curiosity_ping_enabled", None, cbs=["curiosity_ping_enabled"])
br = make_bridge()
br._conv_in["wxid_a"] = time.time() - 120
br._conv_out["wxid_a"] = time.time() - 60
check("收尾追问-关：不追问", lambda: br.ping_tick(), [])

# 13. 梗库
set_key("memes_enabled", 1, cbs=["memes_enabled"])
br = make_bridge()
br._run(priv_msg("kskbl"), "wxid_a", "kskbl")
check("梗库-开：提示词里带暗号", lambda: "kskbl" in br._brain.calls[-1], True)
set_key("memes_enabled", None, cbs=["memes_enabled"])
br = make_bridge()
br._run(priv_msg("kskbl"), "wxid_a", "kskbl")
check("梗库-关：提示词里没有暗号段", lambda: "只回 zdjd" in br._brain.calls[-1], False)

# 14. 说话节奏
set_key("rhythm", "calm")
br = make_bridge()
br._behavior_conf()
check("节奏-选 calm 后真换档", _wx_profile, "calm")
set_key("rhythm", "natural")
br = make_bridge()
br._behavior_conf()
check("节奏-选回 natural", _wx_profile, "natural")

# 15. 数值类：写进去的值得真的进配置
set_key("bubble_gap_min", 0.35)
set_key("bubble_gap_max", 1.75)
br = make_bridge()
check("数值-气泡间隔真的进配置", lambda: (br._behavior_conf().get("bubble_gap_min"), br._behavior_conf().get("bubble_gap_max")), (0.35, 1.75))

set_key("auto_sticker_tags", "开心,黄豆人")
br = make_bridge()
check("表情标签-只认填进去的几个", lambda: br._behavior_conf().get("auto_sticker_tags"), ["开心", "黄豆人"])

# 16. 私聊前缀
set_key("private_mode", "prefix")
set_key("private_prefix", "小鱼")
br = make_bridge()
check("私聊-prefix：带前缀才接", lambda: br._extract(priv_msg("小鱼 在吗")) is not None, True)
check("私聊-prefix：不带前缀不接", lambda: br._extract(priv_msg("在吗")), None)
set_key("private_mode", "auto")

# 17. 收尾追问的两个门槛
set_key("curiosity_ping_enabled", 1, cbs=["curiosity_ping_enabled"])
set_key("curiosity_ping_after_minutes", 10)
set_key("curiosity_ping_chance", 1.0)
set_key("curiosity_ping_cooldown_minutes", 0)
br = make_bridge()
br._conv_in["wxid_a"] = time.time() - 60
br._conv_out["wxid_a"] = time.time() - 30
check("收尾追问-没到 N 分钟不追问", lambda: br.ping_tick(), [])
set_key("curiosity_ping_after_minutes", 0)
set_key("curiosity_ping_cooldown_minutes", 30)
br = make_bridge()
br._conv_in["wxid_a"] = time.time() - 120
br._conv_out["wxid_a"] = time.time() - 60
br._conv_last_ping["wxid_a"] = time.time() - 60
check("收尾追问-CD 未到不追问", lambda: br.ping_tick(), [])

# 18. 群里随机插话的冷却
set_key("group_chime_chance", 1.0)
set_key("group_chime_cooldown_minutes", 30)
br = make_bridge()
br._last_group_reply["123@chatroom"] = time.time() - 60
check("随机插话-冷却内不插", lambda: br._chime_in(group_msg("那你们谁先上")), False)
set_key("group_chime_chance", 0.0)

# 19. 合并窗口：0 = 立刻回，1.2 = 等一会儿攒起来
set_key("merge_window_seconds", 0.0)
br = make_bridge()
_t0 = time.time()
br.submit(priv_msg("马上回"))
while not br._brain.calls and time.time() - _t0 < 1.5:
    time.sleep(0.02)
_gap = time.time() - _t0
check("合并-窗口 0：立刻处理（<0.6s）", lambda: _gap < 0.6, True)
set_key("merge_window_seconds", 1.2)

# 20. 免打扰只在时段内生效
set_key("quiet_enabled", 1, cbs=["quiet_enabled"])
set_key("quiet_start", "03:00")
set_key("quiet_end", "03:01")
br = make_bridge()
check("免打扰-不在时段内不拦", lambda: br._in_quiet_hours(), False)
set_key("quiet_start", "23:30")
set_key("quiet_end", "08:00")

print("=" * 20)
bad = [r for r in RESULTS if not r[1]]
print(f"共 {len(RESULTS)} 项，通过 {len(RESULTS) - len(bad)}，失败 {len(bad)}")
for name, ok, got, exp in bad:
    print("  失败：", name, "实际", got, "期望", exp)
BEH.write_text(_SNAP, encoding="utf-8")  # 还原
(ROOT / "data" / "panel_selftest_last.json").write_text(
    json.dumps([{"case": n, "ok": o, "got": repr(g), "expect": repr(e)} for n, o, g, e in RESULTS],
               ensure_ascii=False, indent=2), encoding="utf-8")
