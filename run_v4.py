"""微信桥 v4 —— 基于 wechatauto-replica（微信 4.x UI 自动化 + 本地库读消息）。

用法：
    python run_v4.py            # 正常跑
    python run_v4.py --debug    # 多打日志

退出码：0 正常，1 配置/环境问题，2 微信掉线（交给计划任务重拉）。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import random
import signal
import sys
import threading
import time
from pathlib import Path

# 拟人节奏档：natural 的默认间隔 2.5–6 秒已经偏慢（用户 2026-10-22 反馈"太慢"），
# 这里用环境变量把「两次写动作的间隔」压到 1.2–2.4 秒、突发上限放到 12 次/2 分钟，
# 仍然保留抖动和曲线鼠标（真正的机器特征是"恒定间隔"，不是"快"）。
# 想更保守就删掉这两行或改成 calm。
os.environ.setdefault("WECHATAUTO_RHYTHM", "natural")
os.environ.setdefault("WECHATAUTO_WRITE_GAP", "0.45")
os.environ.setdefault("WECHATAUTO_WRITE_BURST", "40")

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import config as cfg_mod           # noqa: E402
import runtime
import wxrecover                     # noqa: E402
from brain import Brain            # noqa: E402
import bridge as b                 # noqa: E402
from bridge import Bridge          # noqa: E402
from safety import SafetyGuard, load_conf  # noqa: E402
from store import Store            # noqa: E402
from adapters.wxa_adapter import WxaAdapter, preflight  # noqa: E402

log = logging.getLogger("main")


def setup_logging(debug: bool) -> None:
    LOGDIR = ROOT / "logs"
    LOGDIR.mkdir(exist_ok=True)
    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    handlers = [logging.StreamHandler(sys.stdout),
                logging.FileHandler(LOGDIR / f"bridge-{time.strftime('%Y%m%d')}.log", encoding="utf-8")]
    logging.basicConfig(level=logging.DEBUG if debug else logging.INFO, format=fmt, handlers=handlers)
    logging.getLogger("wechatauto").setLevel(logging.INFO)


def self_check() -> None:
    """启动自检：解析器/关键符号缺了就别启动，免得跑起来才在回复时崩掉。

    踩过：一次编辑把 pull_stickers 删掉了，桥照常启动、消息照常收到，
    但每次回复都在 _run 里 NameError，用户看到的只是"她不回我"。
    """
    import bridge as b

    clean, tags = b.pull_stickers("好[[表情:无语]]")
    assert clean == "好" and tags == ["无语"], "pull_stickers 坏了"
    clean, acts = b.pull_actions("好[[动作:发消息|YY|在吗]]")
    assert clean == "好" and acts == [("发消息", "YY", "在吗")], "pull_actions 坏了"
    for name in ("Bridge", "Msg", "RateLimiter", "split_text", "_with_clock", "_media_path"):
        assert hasattr(b, name), f"bridge.{name} 不见了"
    # 签名也要对得上：踩过一次——run_v4 传了 address_book，Bridge.__init__ 里没这个参数，
    # 进程一启动就 TypeError 退出，用户看到的只是"又不理我了"。
    import inspect

    sig = inspect.signature(b.Bridge.__init__).parameters
    need = ("cfg", "brain", "store", "sender", "guard", "persona", "settings",
            "settings_dir", "actions", "scheduler", "send_image", "send_favorite",
            "poke", "stickers", "group_info", "address_book")
    missing = [n for n in need if n not in sig]
    assert not missing, f"Bridge 构造参数缺：{missing}"
    log.info("自检通过：解析器、关键符号与 Bridge 签名齐全")



def _note_backfill(data_dir, since: float, count: int) -> None:
    """把这次补收的结果记下来，面板上能看到。"""
    try:
        import json as _json

        (data_dir / "backfill_last.json").write_text(
            _json.dumps({"when": time.strftime("%m-%d %H:%M:%S"),
                         "since": time.strftime("%m-%d %H:%M", time.localtime(since)),
                         "count": count}, ensure_ascii=False),
            encoding="utf-8")
    except Exception:
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-c", "--config", default=str(ROOT / "config.toml"))
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()
    setup_logging(args.debug)

    cfg = cfg_mod.load(args.config)
    log.info("配置载入：%s", cfg.source)

    try:
        self_check()
    except Exception as e:
        log.error("启动自检失败，先不启动：%s", e)
        return 1

    try:
        log.info(preflight())
    except Exception as e:
        log.error("启动自检失败：%s", e)
        return 1

    data_dir = ROOT / "data"
    data_dir.mkdir(exist_ok=True)

    # 单实例：两个桥同时在跑会各发一遍，等于主动刷屏（也是风控特征）。
    lock_f = open(data_dir / "bridge.lock", "w")
    try:
        import msvcrt

        msvcrt.locking(lock_f.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        log.error("已有一个桥在运行（data\\bridge.lock 被占用），本进程退出")
        return 0

    store = Store(str(data_dir / "wechat.db"))
    brain = Brain(cfg.dsh_cmd, cfg.agent_home, profile=cfg.profile, timeout=cfg.timeout_seconds)

    try:
        adapter = WxaAdapter(
            bot_names=[cfg.bot_name] + list(cfg.group_keywords),
            interval=0.6,
            screenshot_fallback=bool(cfg.media.get("screenshot_fallback", False)),
            fetch_original=bool(cfg.media.get("fetch_original", True)),
        )
    except Exception as e:
        log.error("微信没准备好：%s", e)
        return 1

    # friend_check 让 allow_friends 模式能判定"对方是不是好友"
    guard = SafetyGuard(load_conf(cfg.safety), data_dir / "safety.json",
                        friend_check=adapter.is_contact, admins=cfg.admin_wxids)

    # 运行时设置：节奏档等，/节奏 命令会改这里
    settings = runtime.load_settings(data_dir)
    if settings.get("rhythm_profile"):
        cfg.rhythm_profile = settings["rhythm_profile"]
    os.environ["WECHATAUTO_RHYTHM"] = cfg.rhythm_profile
    runtime.apply_profile(cfg.rhythm_profile, guard)

    from persona import Persona  # 人设：主人用 /人设 改，下一句生效
    from proactive import make_scheduler

    # 定时主动发消息：默认关闭，靠 /定时开关 on 打开，条数和间隔都有硬上限
    def _log_outgoing(who: str, text: str) -> None:
        """主动发出去的消息也写进消息表（否则她读上下文时看不到自己刚主动说过什么）。"""
        try:
            row = store.db.execute(
                "select wxid from contacts where alias = ? limit 1", (who,)).fetchone()
            key = row[0] if row else who
            store.log(f"pro-{int(time.time() * 1000)}", getattr(adapter, "wxid", ""),
                      key, "out", text, 0.0)
        except Exception:
            log.warning("主动消息记账失败", exc_info=True)

    def _record_proactive(who: str, text: str) -> None:
        _log_outgoing(who, text)
        try:
            key = _wxid_of(who)
            n = store.db.execute(
                "select count(*) from messages where "
                "(room=? or (sender=? and (room is null or room='')))",
                (key, key)).fetchone()[0]
            if int(n or 0) <= 1:
                # 发之前一句话都没有：这是第一次跟他说话。
                # 只发这一条，不追发，以后也不再主动找他（用户 2026-10-27 定）。
                _once_add(key, who)
                log.info("第一次跟「%s」说话（之前没聊过）：只发这一条，不追发", who)
                return
            _wait_bump(who, text)   # 甩一次骰子：决定追几条、下一条什么时候发
        except Exception:
            log.warning("追发记账失败", exc_info=True)

    # ---- 主动找完人没理她：隔一段时间再补一句，总共最多 N 条 ---------------
    # 配置放 data\followup.json（改完当场生效，不用重启）：
    #   {"enabled": true, "max_total": 3, "count_weights": [0.45, 0.35, 0.2],
    #    "interval_min_minutes": 17, "interval_max_minutes": 73}
    # count_weights[i] = 追 i 条的权重（0/1/2 条），所以"追几条"是掷骰子定的。
    wait_path = data_dir / "proactive_wait.json"
    followup_path = data_dir / "followup.json"
    # 只主动找过一次、至今没聊过的人：记在这里，以后不再随机找他
    once_path = data_dir / "proactive_once.json"

    FOLLOWUP_DEFAULTS = {
        "enabled": True,
        "max_total": 3,
        "count_weights": [0.45, 0.35, 0.2],
        "interval_min_minutes": 17,
        "interval_max_minutes": 73,
    }

    # 追发时的情绪底色（随机挑一个，避免每条都是同一种口气）。
    # 用户 2026-10-27 要求「更明显的语气」，所以允许直接问"怎么不回我"这类。
    FOLLOWUP_MOODS = (
        "直接问「怎么不回我」，带点不高兴",
        "炸毛：人呢？装死是吧",
        "委屈：是不是我哪句说错了",
        "嘴硬：行，不回拉倒",
        "自嘲：得，又是我自说自话",
        "假装无所谓，其实一直在等",
    )

    _wait_lock = threading.Lock()

    def _followup_conf() -> dict:
        conf = dict(FOLLOWUP_DEFAULTS)
        try:
            if followup_path.exists():
                raw = json.loads(followup_path.read_text(encoding="utf-8-sig")) or {}
                if isinstance(raw, dict):
                    conf.update(raw)
        except Exception:
            log.warning("追发配置读不出来（%s），按默认走", followup_path, exc_info=True)
        return conf

    def _followup_gap(conf: dict) -> float:
        """两条之间的间隔：17–73 分钟之间随机，别形成机器节奏。"""
        try:
            lo = max(1.0, float(conf.get("interval_min_minutes") or 17))
        except (TypeError, ValueError):
            lo = 17.0
        try:
            hi = max(lo, float(conf.get("interval_max_minutes") or 73))
        except (TypeError, ValueError):
            hi = max(lo, 73.0)
        return random.uniform(lo, hi) * 60.0

    def _followup_plan(conf: dict) -> int:
        """发完第一条主动后甩一次骰子，决定还要追几条（0 / 1 / 2…）。"""
        try:
            cap = int(conf.get("max_total") or 3)
        except (TypeError, ValueError):
            cap = 3
        max_extra = max(0, cap - 1)
        if max_extra <= 0:
            return 0
        weights = []
        raw = conf.get("count_weights") or []
        for i in range(max_extra + 1):
            try:
                w = float(raw[i])
            except (IndexError, TypeError, ValueError):
                w = 0.0
            weights.append(max(0.0, w))
        total_w = sum(weights)
        if total_w <= 0:
            return 0            # 配错了就当"不追"，绝不乱发
        pick = random.random() * total_w
        acc = 0.0
        for i, w in enumerate(weights):
            acc += w
            if pick < acc:
                return i
        return 0

    def _wait_load() -> dict:
        try:
            return json.loads(wait_path.read_text(encoding="utf-8-sig")) or {}
        except Exception:
            return {}

    def _wait_save(d: dict) -> None:
        try:
            wait_path.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception:
            log.warning("追发记录写不进去", exc_info=True)

    def _wait_mut(fn):
        """读-改-写一次（有锁）：监听线程销账和主循环追发不会互相覆盖。"""
        with _wait_lock:
            d = _wait_load()
            out = fn(d)
            _wait_save(d)
            return out

    def _wait_pop(key: str) -> None:
        _wait_mut(lambda d: d.pop(key, None))

    def _once_load() -> dict:
        try:
            d = json.loads(once_path.read_text(encoding="utf-8-sig")) or {}
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    def _once_add(key: str, who: str) -> None:
        try:
            with _wait_lock:
                d = _once_load()
                d[key] = {"who": who, "ts": time.time()}
                once_path.write_text(json.dumps(d, ensure_ascii=False, indent=1),
                                     encoding="utf-8")
        except Exception:
            log.warning("一次性主动记录写不进去", exc_info=True)

    # 这些不是真人：系统号/公众号/群，主动消息一律不发、追发也不追
    _SYS_IDS = {
        "weixin", "filehelper", "fmessage", "medianote", "floatbottle", "qqmail",
        "tmessage", "weibo", "notifymessage", "newsapp", "brandsessionholder",
        "brandservicesessionholder", "position", "announce", "weixinreminder",
        "officialaccounts", "mphelper", "exmail_tool", "qqsafe",
    }

    def _is_system(wxid: str) -> bool:
        w = str(wxid or "").strip()
        if not w:
            return True
        low = w.lower()
        return (low in _SYS_IDS or low.startswith("gh_") or low.startswith("filehelper")
                or low.endswith("@chatroom") or low.endswith("@openim"))

    def _wxid_of(who: str) -> str:
        try:
            row = store.db.execute(
                "select wxid from contacts where alias = ? limit 1", (who,)).fetchone()
            return row[0] if row and row[0] else who
        except Exception:
            return who

    def _wait_bump(who: str, text: str) -> None:
        """第一条主动刚发出去：掷骰子定追几条，并算好下一条的时间。"""
        conf = _followup_conf()
        if not conf.get("enabled", True):
            return
        _k0 = _wxid_of(who)
        if _is_system(_k0):
            log.info("主动已发（%s）：系统号，不追", who)
            return
        try:
            if bridge.chat_muted(_k0):     # bridge 在主流程后面才建好，调用时已存在
                log.info("主动已发（%s）：这个会话被熔断静音了，不追", who)
                return
        except NameError:
            pass
        plan = _followup_plan(conf)
        if plan <= 0:
            log.info("主动已发（%s）：这次不追", who)
            return
        now = time.time()
        key = _wxid_of(who)
        first = str(text or "")[:200]
        ent = {"who": who, "total": 1, "plan": plan, "last_ts": now,
               "first_text": first, "last_text": first, "texts": [first],
               "next_ts": now + _followup_gap(conf)}
        _wait_mut(lambda d: d.__setitem__(key, ent))
        log.info("主动已发（%s）：骰子说再追 %s 条，下一条约 %.0f 分钟后",
                 who, plan, (ent["next_ts"] - now) / 60.0)

    def _wait_clear(wxid: str) -> None:
        if not wxid:
            return
        hit = _wait_mut(lambda d: d.pop(wxid, None) is not None)
        if hit:
            log.info("对方回话了，追发取消（%s）", wxid)

    def _on_message(m) -> None:
        """监听回调：先销掉追发（对方回话），再交给桥。"""
        try:
            sender = getattr(m, "sender", "") or ""
            room = getattr(m, "room", "") or ""
            if sender and sender != getattr(adapter, "wxid", ""):
                _wait_clear(sender)
            elif room:
                _wait_clear(room)
        except Exception:
            pass
        bridge.submit(m)

    def _followup_tick() -> None:
        """到点的追发：对方一直没回、且不在免打扰/静音里，就补一句。

        生成和发送都在锁外做（模型要几秒，不能卡住对方回话时的销账）；
        发完再读一次表，如果这期间对方回话了（记录已销），就不再写回去。
        """
        conf = _followup_conf()
        if not conf.get("enabled", True):
            return
        try:
            if bridge.muted_now() or bridge._in_quiet_hours():
                return
        except Exception:
            pass
        for key, ent in list(_wait_load().items()):
            if not isinstance(ent, dict):
                _wait_pop(key)
                continue
            try:
                total = int(ent.get("total") or 1)
                plan = int(ent.get("plan") or 0)
                next_ts = float(ent.get("next_ts") or 0)
            except (TypeError, ValueError):
                _wait_pop(key)
                continue
            if plan <= 0 or total >= plan + 1:
                _wait_pop(key)
                continue
            if time.time() < next_ts:
                continue
            if _is_system(key):
                _wait_pop(key)
                continue
            try:
                if bridge.chat_muted(key):
                    continue
            except Exception:
                pass
            who = str(ent.get("who") or key)
            # 对方刚回话就把记录销了，这里先看一眼，别生成完又发一条
            if key not in _wait_load():
                continue
            # 把已经发过的原话给她看（知道就行，不必硬接），再带点"被晾着"的情绪
            texts = [str(t).strip() for t in (ent.get("texts") or []) if str(t).strip()]
            if not texts:
                texts = [str(ent.get("first_text") or ent.get("last_text") or "").strip()]
            texts = [t for t in texts if t]
            hist = "；".join(f"「{t}」" for t in texts)
            ago = max(1, int((time.time() - float(ent.get("last_ts") or time.time())) / 60))
            left = plan - total + 1          # 连这条一起，还能追几条
            hint = (
                "这是「追发」。"
                f"你之前已经主动发过这些话（知道就行，不必硬接）：{hist}。"
                f"对方一直没回，距你上一条已经 {ago} 分钟。"
                "现在自然再补一句：顺着说、只说此刻的情绪、或者换个话头都行，不用非接着上一条。"
                "别重复前面说过的句子，别问「在吗」。"
                f"情绪底色：{random.choice(FOLLOWUP_MOODS)}。"
                "可以直接把不高兴说出来：「怎么不回我」「人呢」「行，不回拉倒」这种都行，"
                "用你自己的脾气说，别客服腔、别解释、别找补。"
                "但别骂人、别威胁（拉黑/删好友）、别一条接一条地逼问，一轮最多一个问句。"
                + ("这是最后一次补话，说完就收。" if left <= 1
                   else "说完就停，别连着追问。")
                + "一两句，口语，只给正文。"
            )
            try:
                text = _compose(who, hint=hint)
            except Exception as e:
                log.warning("追发生成失败（%s）：%r", who, e)
                continue
            if not text:
                log.warning("追发：模型没给出内容，下个 tick 再试（%s）", who)
                continue
            if key not in _wait_load():
                log.info("对方刚回话，这条追发作废（%s）", who)
                continue
            ok, why = guard.check_outgoing(text, who)
            if not ok:
                log.info("追发被内容检查拦下（%s：%s），这条不追了", who, why)
                _wait_pop(key)
                continue
            try:
                guard.delay_for(text)
            except Exception:
                pass
            try:
                sent = _send_proactive(who, text)
            except Exception as e:
                log.warning("追发发送出错（%s）：%r", who, e)
                sent = False
            if not sent:
                log.warning("追发没发出去（%s），下个 tick 再试", who)
                continue
            _log_outgoing(who, text)
            total += 1

            def _apply(d, key=key, total=total, text=text):
                cur = d.get(key)
                if not isinstance(cur, dict):
                    return False        # 这期间对方回话了，销账已完成，别复活
                cur["total"] = total
                cur["last_ts"] = time.time()
                cur["last_text"] = text[:200]
                hist = [str(t) for t in (cur.get("texts") or []) if str(t).strip()]
                hist.append(text[:200])
                cur["texts"] = hist[-4:]
                if not cur.get("first_text"):
                    cur["first_text"] = hist[0]
                if total >= plan + 1:
                    d.pop(key, None)
                else:
                    cur["next_ts"] = time.time() + _followup_gap(conf)
                return True

            if _wait_mut(_apply):
                log.info("追发 %s/%s 已发 → %s：%s", total, plan + 1, who, text[:60])
            else:
                log.info("对方刚回话，这条追发作废（%s）", who)

    def _send_proactive(who: str, text: str) -> bool:
        """主动消息的收件人常是备注名/昵称，先换成 wxid 再发，否则会被"避免发错人"挡住。"""
        target = who
        try:
            row = store.db.execute(
                "select wxid from contacts where alias = ? and wxid not like 'filehelper%'",
                (who,),
            ).fetchone()
            if row and row[0]:
                target = row[0]
        except Exception:
            pass
        ok = adapter.send(target, text)
        if not ok:
            log.warning("随机主动没发出去（收件人 %s -> %s）", who, target)
        return bool(ok)

    scheduler = make_scheduler(str(data_dir), guard=guard,
                               sender=_send_proactive,
                               quiet=cfg.quiet_hours,
                               on_sent=_record_proactive)

    actions = {"accept_friends": adapter.accept_friends}

    # 表情库：本地 stickers\ 目录 + 需要时去网上抓（见 stickers.py）
    import stickers as sticker_lib

    bridge = Bridge(cfg, brain, store, sender=adapter.send,
                    self_wxid=adapter.wxid, guard=guard,
                    persona=Persona(), settings=settings, settings_dir=str(data_dir),
                    actions=actions, scheduler=scheduler,
                    send_image=adapter.send_image, send_favorite=adapter.send_favorite,
                    poke=adapter.poke,
                    stickers=sticker_lib, group_info=adapter.group_info,
                    address_book=adapter.address_book_summary,)
    log.info("防封状态：%s", guard.describe())
    log.info("人设：%s", "已设置" if bridge.persona.text() else "默认（AGENTS.md）")
    log.info("表情库：%s 个标签", len(sticker_lib.tags()))
    _bh = bridge._behavior_conf()
    log.info("连发：%s（拆开概率 %s，最多 %s 条）；自动表情：%s 概率 %s，标签 %s",
             "开" if _bh.get("bubbles_enabled") else "关",
             _bh.get("bubble_chance"), _bh.get("bubble_max"),
             "开" if float(_bh.get("auto_sticker_chance") or 0) > 0 else "关",
             _bh.get("auto_sticker_chance"),
             "、".join(_bh.get("auto_sticker_tags") or []))

    # 静音 = 只闭嘴、不闭眼：文字消息照读照记（当上下文），但不回复；
    # 图片/表情在静音时不点「查看原图」、不截图（那会抢前台），只留一句占位。
    def _should_skip(sender: str) -> bool:
        return False

    adapter.should_skip = _should_skip
    # 静默模式：/停 之后不抢前台、不弹窗、不动窗口（打游戏时最要紧）
    adapter.quiet = bridge.muted_now
    # 补收前先按 msgid 判重，别让已处理过的消息每分钟重下一次图/滚一次会话
    adapter.is_known = lambda mid: store.is_seen(mid) or store.is_duplicate(mid)
    bridge.scheduler = scheduler


    adapter.start(_on_message)
    log.info("桥已启动，退出请按 Ctrl+C")

    # 她主动找人时允许出现的动作标记：只留不打扰别人的这几个
    SPONT_ACTS = ("表情", "存表情", "记笔记")

    def _compose(name: str, hint: str = "") -> str:
        """让模型凭记忆给她挑个人主动说一句（随机主动/手动触发/追发共用）。

        提示词跟私聊回复用同一套上下文（人设+笔记+最近 50 条+表情库+梗库），
        这样对熟人能接上真实话题，对陌生人也不会装作认识人家很久。
        """
        ident = name
        try:
            row = store.db.execute(
                "select wxid from contacts where alias = ? limit 1", (name,)).fetchone()
            if row and row[0]:
                ident = row[0]
        except Exception:
            pass
        ctx = (f"（你主动找「{name}」说话：不是回他，是你自己闲下来、想起他，想跟他说句话。硬要求：\n"
               "1. **不许拿键盘、机箱、风扇、嗡嗡声、鼠标、屏幕这类环境音当开场**，也别写你屋里、你机器什么样——"
               "这是你最老的毛病，太像机器人了，一句都不许有；\n"
               "2. **先看下面的最近对话**：你们最近聊到的事、他没说完的话、他提过的日子（课表、加班、在打的游戏、在追的剧），"
               "拎一件出来说一句——这比什么都强；\n"
               "3. 真没什么可接，才退回这两种口气：\n"
               "   · 直接喊人：「闲着没」「一天没声了」「在干嘛呢」；\n"
               "   · 问他一个他答得上来的小问题，或者拉他玩点小的：数字炸弹（1~100，他猜、你只答「大了/小了」，"
               "猜中就炸）、词语接龙、三连二选一、五秒三个——真想玩，你**自己先一句话把规则说清**"
               "（像「1到100，我心里定个数，你来猜，我只说大了小了」），别只说「玩个游戏」然后等他定规则；"
               "别再拿 1 到 10 那种太小的糊弄人；\n"
               "4. 别每次都用同一招：别开口就问吃的，别老是二选一，别老是同一句口气；\n"
               "5. 别编外面的事（刚刷到、刚吃了什么、刚出门），你没出过门也没刷过视频；别装忙装累；"
               "别喊「主人」（除非对方确实是主人）；别问「在吗」；\n"
               "6. 说完留个能接的话头，让他好回（一句问句就够）；只给正文：不要 [[动作:…]]、[[表情:…]] 标记，"
               "不要分点，不要复述旧事。\n"
               + (f"补充情况：{hint}\n" if hint else "")
               + "）")
        try:
            prompt = bridge.compose_proactive(ident, name, hint=ctx,
                                              allow_acts=SPONT_ACTS)
        except Exception:
            log.warning("主动开场拼上下文失败，退回极简提示", exc_info=True)
            prompt = ctx
        reply, _ = brain.ask(prompt, session_id=None)
        # 主动发消息没有走 bridge 的解析链，这里必须自己洗干净，
        # 否则标记会原样发到对方手机上（2026-10-24 19:27 发给 YY 的那条就带着）。
        try:
            reply, _acts = b.pull_actions(reply)
        except Exception:
            pass
        try:
            reply, _tags = b.pull_stickers(reply)
        except Exception:
            pass
        lines = [ln.strip() for ln in reply.splitlines() if ln.strip()]
        return "\n".join(lines[:2])

    def _manual_proactive(who: str) -> None:
        """手动让她去找某个人：不看概率/额度/间隔，只看静音和免打扰。"""
        if bridge.muted_now():
            log.info("手动主动：现在是静音状态，跳过（%s）", who)
            return
        if not who:
            log.warning("手动主动：没写目标")
            return
        try:
            text = _compose(who)
        except Exception as e:
            log.warning("手动主动生成失败（%s）：%r", who, e)
            return
        if not text:
            log.warning("手动主动：模型没给出内容（%s）", who)
            return
        try:
            if not _send_proactive(who, text):
                log.warning("手动主动没发出去（%s）：%s", who, text[:40])
                return
            _record_proactive(who, text)
            log.info("手动主动：%s → %s", who, text[:60])
        except Exception as e:
            log.warning("手动主动发送出错（%s）：%r", who, e)



    # 补收：桥不在的那段时间里进来的消息，按库里最后一条的时间往前捞一次。
    # 监听的水位在"入队"时就提交了，进程半路挂掉那几条就再也不会推过来。
    try:
        # 离线补收：把电脑没开期间收到的消息也捞进上下文。
        # 太旧的只记账、不回（bridge.submit 里按 reply_max_age_minutes 判）。
        bf = cfg.backfill or {}
        if bf.get("enabled", True):
            last = store.max_ts("in")
            cap_hours = float(bf.get("max_hours", 24) or 24)
            since = last if last else (time.time() - 3600)
            since = max(since - 60, time.time() - cap_hours * 3600)
            catch = adapter.catch_up(since, sessions=25,
                                     per_chat=int(bf.get("max_per_chat", 40) or 40))
            if catch:
                log.info("离线补收：%s 条（自 %s）", len(catch),
                         time.strftime("%m-%d %H:%M", time.localtime(since)))
                _note_backfill(data_dir, since, len(catch))
                for m in catch:
                    bridge.submit(m)
            else:
                log.info("离线补收：没有遗漏的消息")
        else:
            since = max(store.max_ts("in") or (time.time() - 1800), time.time() - 900)
            catch = adapter.catch_up(since)
            log.info("启动补收（关闭离线模式）：%s 条", len(catch))
            for m in catch:
                bridge.submit(m)
    except Exception as e:
        log.warning("启动补收失败：%r", e)

    stop = {"now": False}

    def _sig(signum, frame):
        stop["now"] = True

    try:
        signal.signal(signal.SIGINT, _sig)
        signal.signal(signal.SIGTERM, _sig)
    except Exception:
        pass

    last_check = time.time()
    last_rand = 0.0
    last_bf_req = 0.0
    last_now_req = 0.0
    last_cfg = 0.0
    last_follow = 0.0
    mute_skip_logged = False
    while not stop["now"]:
        time.sleep(1)
        if bridge._stop.is_set():
            log.error("桥内部已停止（多半是防封熔断），退出")
            break
        # 面板改了说话节奏/限流之类：每 20 秒碰一次，让它当场生效（不用等有人说话）
        if time.time() - last_cfg > 20:
            last_cfg = time.time()
            try:
                bridge._behavior_conf()
                bridge.idle_tick()
            except Exception as e:
                log.warning("读面板配置失败：%r", e)

        # 追发：主动发完对方没回应，按 17–73 分钟随机间隔再补，最多补到 max_total 条
        if time.time() - last_follow > 30:
            last_follow = time.time()
            try:
                _followup_tick()
            except Exception as e:
                log.warning("追发出错：%r", e)

        # 随机主动：每 5 分钟掷一次骰子（真实概率在 data/proactive.json 的 random 段）
        if time.time() - last_rand > 300:
            last_rand = time.time()

            try:
                try:
                    rows = store.db.execute(
                        "select alias, wxid from contacts where alias is not null and alias != '' "
                        "and wxid not like 'filehelper%' and wxid not like '%@chatroom'").fetchall()
                    skip = _once_load()
                    pool = [a for a, w in rows
                            if w not in skip and not _is_system(w)
                            and not (bridge.chat_muted(w) if bridge else False)]
                except Exception:
                    pool = []
                if not bridge.muted_now():  # 静音时不主动发（一样会抢前台）
                    for note in scheduler.random_tick(compose=_compose, target_pool=pool):
                        log.info("随机主动：%s", note)
            except Exception as e:
                log.warning("随机主动出错：%r", e)

        # 手动触发主动：面板按钮或微信里 `/主动 张三`（不带内容）会写这个请求文件
        try:
            req = data_dir / "proactive_now.json"
            if req.exists() and req.stat().st_mtime > last_now_req:
                last_now_req = req.stat().st_mtime
                who = ""
                try:
                    who = str(json.loads(req.read_text(encoding="utf-8-sig")).get("who") or "").strip()
                except Exception as e:
                    log.warning("手动主动：请求文件读不出来 %r", e)
                try:
                    req.unlink()
                except OSError:
                    pass
                if who:
                    log.info("手动主动：收到请求，去找「%s」", who)
                    _manual_proactive(who)
                else:
                    log.warning("手动主动：请求里没写目标，忽略")
        except Exception as e:
            log.warning("手动主动处理出错：%r", e)
        # 面板「补收最近未接收的消息」：写请求文件，这里看到就执行一次
        try:
            req = data_dir / "backfill.json"
            if req.exists() and req.stat().st_mtime > last_bf_req:
                last_bf_req = req.stat().st_mtime
                bf = cfg.backfill or {}
                cap = float(bf.get("max_hours", 24) or 24)
                since = max((store.max_ts("in") or time.time()) - 60,
                            time.time() - cap * 3600)
                got = adapter.catch_up(since, sessions=25,
                                       per_chat=int(bf.get("max_per_chat", 40) or 40))
                log.info("手动补收：%s 条（自 %s）", len(got),
                         time.strftime("%m-%d %H:%M", time.localtime(since)))
                _note_backfill(data_dir, since, len(got))
                for m in got:
                    bridge.submit(m)
                req.unlink(missing_ok=True)
        except Exception as e:
            log.warning("手动补收失败：%r", e)

        if time.time() - last_check > 60:
            last_check = time.time()
            # 先看看微信是不是还活着（卡死/被踢/窗口没了都会让发送卡 75 秒）
            try:
                if bridge.muted_now():
                    # /停 = 完全静默：不碰微信窗口，也不拉起客户端。以前这里照样跑
                    # 自愈，微信一缩托盘就被当成「已退出」重启，登录页被反复拉起。
                    if not mute_skip_logged:
                        mute_skip_logged = True
                        log.info("静音中：跳过微信窗口自愈（不碰窗口）")
                else:
                    mute_skip_logged = False
                    okwin = wxrecover.ensure_online(adapter.send_fail_streak())
                    if not okwin:
                        log.error("微信没能自愈（重启失败或没登上），先停下等人看一眼")
                        return 3
                    if wxrecover.main_window()[0] == 0:
                        adapter.invalidate_gui()
            except Exception as e:
                log.warning("微信自愈检查失败：%r", e)
            try:
                info = adapter.db.get_self_info()
                if not info.get("username"):
                    log.error("微信掉线了，退出等重启")
                    return 2
                # 心跳：把监听水位和线程池状态写进日志，出问题能一眼看出卡在哪
                wm = {}
                try:
                    lst = getattr(adapter, "_listener", None)
                    if lst is not None:
                        wm = lst.watermark()
                except Exception:
                    pass
                heard = getattr(bridge, "last_msg_ts", 0) or 0
                ago = "从未" if not heard else f"{int((time.time() - heard) / 60)} 分钟前"
                log.info("心跳：在线 %s｜水位 %s｜线程池 %s｜最近收到 %s",
                         info.get("username"), wm or "-",
                         getattr(bridge._pool, "_work_queue").qsize(), ago)
            except Exception as e:
                log.warning("探活失败：%r", e)

            # 兜底补收：监听线程万一卡住/漏推，这里每 60 秒按库里水位回扫一遍，
            # 保证"有消息就一定进桥"。重复的会被 msgid 去重掉，不会重复回。
            try:
                since = (store.max_ts("in") or (time.time() - 900)) - 180
                missed = adapter.catch_up(since, sessions=20, per_chat=40)
                if missed:
                    log.warning("补收回 %d 条（监听可能漏推）", len(missed))
                    for m in missed:
                        bridge.submit(m)
            except Exception as e:
                log.warning("补收失败：%r", e)

    log.info("收尾……")
    adapter.stop()
    bridge.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
