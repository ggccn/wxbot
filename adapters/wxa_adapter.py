"""微信 4.x 适配器（wechatauto-replica）。

- 收消息：读本地解密后的数据库（增量轮询，1 秒一次），不需要注入进程；
- 发消息：驱动官方客户端界面（UIA 优先、坐标+OCR 兜底），等价于真人点键盘。

这层只做「微信长什么样」→「桥认识的 Msg」的转换，防封策略在 safety.py。
"""
from __future__ import annotations
import html as html_lib

import json
import os
from pathlib import Path

import ctypes
import logging
import re
import threading
import time

import wxrecover

log = logging.getLogger("wxa")

# uiautomation / COM 都是"线程亲和"的：同一个 WeChatGUI 对象不能跨线程用，
# 否则报 COMError(-2147220991, '事件无法调用任何订户')。所以：
#   1) 每个线程各自持有一份 WeChatGUI（thread-local）；
#   2) 真正操作界面时再串行化（_UI_LOCK），避免两个线程抢前台。
_UI_LOCK = threading.RLock()


def _ui_guard(fn):
    """串行化所有会动界面的动作（发送/表情/戳/通过好友）。"""

    def wrapper(*a, **kw):
        with _UI_LOCK:
            return fn(*a, **kw)

    wrapper.__name__ = getattr(fn, "__name__", "wrapper")
    return wrapper


def _ensure_com() -> None:
    try:
        import pythoncom

        pythoncom.CoInitialize()
    except Exception:
        pass



class WxaAdapter:
    def __init__(
        self,
        interval: float = 1.0,
        bot_names: list[str] | None = None,
        screenshot_fallback: bool = False,
        fetch_original: bool = True,
    ):
        from wechatauto import WeChatDB  # 延迟导入，免得没装也炸

        self.db = WeChatDB()
        info = self.db.get_self_info()
        self.wxid: str = info.get("username") or self.db.wxid
        self.nick: str = info.get("nick_name") or ""
        self.interval = interval
        self.screenshot_fallback = screenshot_fallback
        self.fetch_original = fetch_original
        self.bot_names = [n for n in (bot_names or []) if n]
        self._gui = None
        self._listener = None
        log.info("微信账号：%s（%s）", self.nick, self.wxid)

    # ---------------- 发送 ----------------
    _BN_PATH = Path(__file__).resolve().parent.parent / "data" / "behavior.json"

    def _dynamic_names(self) -> list[str]:
        """点名用的名字：启动时的配置 + behavior.json 里实时改的关键词（换昵称不用重启）。"""
        try:
            st = os.stat(self._BN_PATH)
            if getattr(self, "_bn_mtime", 0) != st.st_mtime:
                d = json.loads(self._BN_PATH.read_text(encoding="utf-8"))
                self._bn_cache = [str(x) for x in (d.get("group_keywords") or []) if x]
                self._bn_mtime = st.st_mtime
            if getattr(self, "_bn_cache", None):
                return list(dict.fromkeys(list(self.bot_names or []) + self._bn_cache))
        except Exception:
            pass
        return list(self.bot_names or [])

    _gui_gen = 0

    _send_fails = 0

    def send_fail_streak(self) -> int:
        return int(getattr(self, "_send_fails", 0))

    def invalidate_gui(self) -> None:
        """微信被重启过：让各线程下次用时重建 WeChatGUI（hwnd 变了）。"""
        self._gui_gen = int(getattr(self, "_gui_gen", 0)) + 1
        log.info("GUI 缓存已作废，下次发送会重新连微信窗口")

    @property
    def gui(self):
        """每个线程一份 WeChatGUI（COM 对象不能跨线程）。"""
        if getattr(self, "_tls", None) is None:
            self._tls = threading.local()
        self.quiet = None  # callable() -> True 时不抢前台
        g = getattr(self._tls, "gui", None)
        if g is None:
            _ensure_com()
            from wechatauto import WeChatGUI

            g = WeChatGUI()
            self._tls.gui = g
            log.info("微信窗口已连接（线程 %s）：hwnd=%s", threading.current_thread().name,
                     getattr(g, "main_hwnd", "?"))
            try:
                self._ensure_window_size()
            except Exception:
                pass
        return g

    def display_name(self, target: str) -> str:
        """把 wxid / 群 id 换成界面上能搜到的名字。"""
        if target in ("filehelper",):
            return "文件传输助手"
        try:
            if target.endswith("@chatroom"):
                return self.db.group_id_to_name(target) or target
            return self.db.get_nickname(target) or target
        except Exception:
            return target

    def _target_exists(self, name: str) -> bool:
        """界面上能不能真的找到这个人/这个群（找不到就别发，免得发错会话）。"""
        if not name:
            return False
        if name == "文件传输助手":
            return True
        try:
            for c in self.db.search_contact(name) or []:
                cand = (c.get("nickname") or c.get("remark") or c.get("username") or "")
                if cand == name or (c.get("username") or "").endswith(name):
                    return True
                if name in (c.get("remark") or "", c.get("nickname") or ""):
                    return True
        except Exception as e:
            log.debug("查聊天对象失败 %s：%s", name, e)
        return False

    def address_book_summary(self) -> dict:
        """她自己的通讯录有几号人：好友数（wxid_ 开头，排除群和公众号）+ 群数。"""
        out = {"friends": 0, "groups": 0}
        try:
            out["groups"] = len(self.db.get_groups() or [])
        except Exception:
            pass
        try:
            con = self.db._contact_conn()
            if con is not None:
                try:
                    out["friends"] = con.execute(
                        "select count(*) from contact where username like 'wxid_%'"
                    ).fetchone()[0]
                finally:
                    con.close()
        except Exception as e:
            log.debug("数好友失败：%r", e)
        return out

    def is_contact(self, wxid: str) -> bool:
        """wxid 是不是本号好友（用于防封的 allow_friends 模式）。"""
        if not wxid or wxid.endswith("@chatroom"):
            return False
        if wxid == "filehelper":
            return True
        try:
            for c in self.db.search_contact(wxid) or []:
                if (c.get("username") or "") == wxid:
                    return True
        except Exception as e:
            log.debug("查好友失败 %s：%s", wxid, e)
        return False

    def _resolve_target(self, name: str) -> Optional[str]:
        """把界面上的名字换成 wxid/群 id。找不到返回 None。

        群和最近会话都查一遍：刚被拉进的群，`search_contact` 有时查不到，
        但 `get_sessions`/`get_groups` 里有，按 id 发就不会串到别的会话。
        """
        if not name:
            return None
        if name == "文件传输助手":
            return "filehelper"
        if name.startswith("wxid") or name.endswith("@chatroom") or name == "filehelper":
            return name
        try:
            for g in self.db.get_groups() or []:
                if (g.get("name") or "") == name:
                    return g.get("username")
        except Exception as e:
            log.debug("查群失败 %s：%s", name, e)
        try:
            for s in self.db.get_sessions() or []:
                uname = s.get("username") or ""
                if uname == name:
                    return uname
                disp = s.get("nickname") or s.get("remark") or ""
                if disp == name:
                    return uname
        except Exception as e:
            log.debug("查会话失败 %s：%s", name, e)
        try:
            for c in self.db.search_contact(name) or []:
                if name in ((c.get("nickname") or ""), (c.get("remark") or "")):
                    return c.get("username")
        except Exception as e:
            log.debug("查联系人失败 %s：%s", name, e)

        # 名字没完全对上（群名常带「（3）」这种后缀，人只说主名）：
        # 做一次模糊匹配，但只接受唯一命中，避免又发错人。
        q = re.sub(r"\s+", "", name).lower()
        if q:
            cands = {}
            try:
                for g in self.db.get_groups() or []:
                    gn = re.sub(r"\s+", "", (g.get("name") or "")).lower()
                    if gn and (gn.startswith(q) or q in gn):
                        cands[g.get("username")] = g.get("name")
            except Exception:
                pass
            try:
                for c in self.db.search_contact(name) or []:
                    for k in ("nickname", "remark"):
                        v = (c.get(k) or "").strip().lower()
                        if v and (v.startswith(q) or q in v):
                            cands[c.get("username")] = c.get("remark") or c.get("nickname")
            except Exception:
                pass
            if len(cands) == 1:
                wxid2, disp = next(iter(cands.items()))
                log.info("「%s」模糊匹配到「%s」（%s）", name, disp, wxid2)
                return wxid2
            if len(cands) > 1:
                log.warning("「%s」对得上多个：%s，不发", name, list(cands.values()))
        return None

    def send(self, target: str, text: str, aters: str = "") -> bool:
        try:
            ok = self._send_wrapper(target, text, aters)
        finally:
            try:
                wxrecover.restore_topmost()
            except Exception:
                pass
        self._send_fails = 0 if ok else int(getattr(self, "_send_fails", 0)) + 1
        if ok and getattr(self, "_send_fails", 0) == 0 and getattr(self, "_had_fails", 0):
            self._had_fails = 0
        if not ok:
            self._had_fails = 1
        return ok

    def _send_wrapper(self, target: str, text: str, aters: str = "") -> bool:
        self._ensure_window_size()
        with _UI_LOCK:
            return self._send_locked(target, text, aters)

    def _send_locked(self, target: str, text: str, aters: str = "") -> bool:
        try:
            wxrecover.demote_topmost()
        except Exception:
            pass
        try:
            strays = wxrecover.close_strays()
            if strays:
                log.info("发送前关掉挡路弹窗：%s", "、".join(strays))
        except Exception:
            pass
        looks_like_id = target.startswith("wxid") or target.endswith("@chatroom") or target == "filehelper"
        who_id = target if looks_like_id else self._resolve_target(target)
        if who_id is None:
            log.error("找不到聊天对象「%s」，不发（避免发错人）", target)
            return False
        who = self.display_name(who_id)
        body = text
        if aters:
            name = aters if not aters.startswith("wxid") else self.display_name(aters)
            body = f"@{name}\u2005{text}"
        fg = self._foreground()
        ok = False
        try:
            self._quiet_foreground()
            # 先自己用 UIA 发一次（一次粘贴一次回车，落库为准），这条路不会重发；
            # 只有它明确失败（或者群聊要 @）才退回库的 send_msg。
            if not aters and self._send_text_uia(who, body):
                self._last_chat = self.display_name(target) if not str(target).startswith(("wxid", "@")) else who
                return True
            # 兜底也不能再用库的 send_msg：它判"输入框没清空"时会连按 3 次回车/
            # 点发送，每次都真的发出去了 —— 2026-10-27 10:48 那条「人呢，晾我
            # 三十多分钟了」就是这么发了三遍（间隔 12 秒）。这里自己发。
            ok = self._send_ocr(who, body)
        except Exception as e:
            text_err = repr(e)
            if ("COMError" in text_err or "-2147220991" in text_err) and not getattr(self._tls, "retried", False):
                log.warning("发送撞到 COM 错误，重建本线程的微信句柄后重试一次 → %s", who)
                self._tls.gui = None
                self._tls.retried = True
                try:
                    return self._send_locked(target, text, aters)
                finally:
                    self._tls.retried = False
            log.error("发送异常 → %s：%r", who, e)
            return False
        finally:
            self._give_back_desktop(fg)
        if not ok:
            log.warning("发送失败 → %s（OCR 兜底也没成功）", who)
        return bool(ok)

    # ---------------- 群信息 ----------------
    def group_info(self, room: str) -> dict:
        """群名 + 成员名单（缓存 10 分钟，成员不常变）。"""
        now = time.time()
        cache = getattr(self, "_group_cache", None)
        if cache is None:
            cache = self._group_cache = {}
        hit = cache.get(room)
        if hit and now - hit[0] < 600:
            return hit[1]
        info = {"name": "", "members": []}
        try:
            info["name"] = self.db.group_id_to_name(room) or ""
        except Exception:
            pass
        try:
            for m in self.db.get_group_members(room) or []:
                name = (m.get("remark") or m.get("nick_name") or m.get("username") or "").strip()
                if name:
                    info["members"].append({"name": name, "wxid": m.get("username") or "",
                                            "owner": bool(m.get("is_owner"))})
        except Exception as e:
            log.debug("取群成员失败 %s：%r", room, e)
        cache[room] = (now, info)
        return info

    # ---------------- 自己发文本：别让库的重试循环重发 ----------------
    def _db_chat(self, chat: str) -> str:
        """查库要用 wxid；界面上拿到的是备注名/昵称，这里换一下（不然永远查不到）。"""
        c = str(chat or "").strip()
        if not c:
            return c
        low = c.lower()
        if (low.startswith("wxid") or low == "filehelper" or low == "weixin"
                or low.endswith("@chatroom") or low.endswith("@openim")):
            return c
        try:
            rid = self._resolve_target(c)
            return rid or c
        except Exception:
            return c

    def _out_watermark(self, chat: str) -> int:
        chat = self._db_chat(chat)
        try:
            msgs = self.db.get_new_messages(chat, since_seq=0) or []
            return int(msgs[-1].get("sort_seq") or 0) if msgs else 0
        except Exception:
            return 0

    def _landed(self, chat: str, body: str, mark: int, timeout: float = 8.0) -> bool:
        """在数据库里等自己这条消息出现——落库了才算发出去。"""
        probe = (body or "").strip()[:12]
        if not probe:
            return False
        chat = self._db_chat(chat)
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                msgs = self.db.get_new_messages(chat, since_seq=mark) or []
            except Exception:
                msgs = []
            for m in msgs:
                if m.get("sender_id") == 2 and probe in (m.get("content") or ""):
                    return True
            time.sleep(0.35)
        return False

    def _send_text_uia(self, who: str, body: str) -> bool:
        """只走 UIA：一次粘贴 + 一次回车，落库才算成功，失败最多再来一次。

        库的 send_msg 在 UIA 判失败时会退到"点击发送"的老路径，并且带 3 次重试
        + 75 秒总时限；那里"点击没生效"和"其实已经发出去了"分不清，实测会把同
        一条消息发两遍（用户看到的"重复发消息"就是这么来的）。
        """
        try:
            gui = self.gui
            uia = gui._get_uia()
        except Exception as e:
            log.debug("UIA 不可用：%r", e)
            return False
        if uia is None:
            return False
        mark = self._out_watermark(who)
        try:
            if uia.current_chat() != who and not uia.open_chat(who):
                return False
            sent = uia.send_text(body)
            if sent:
                # send_text 内部已经回读输入框比对过内容、再按一次回车，属于
                # "要么没发、要么只发了一条"。这里**不能**再用数据库复核去否决
                # 它：库落盘有延迟，实测复核为假阴性后我们退回库的重试路径，
                # 于是同一条消息被发了第二遍（20:52:32 与 20:52:42 两条一模一样）。
                if not self._landed(who, body, mark, 0.3):
                    log.info("已按回车发送，但 3 秒内没在库里等到（库有延迟，不代表没发）")
                return True
            # 到这里是明确没发：send_text 会把输入框清空且不按回车，重试安全
            log.info("UIA 第一次没发出去（读回为空/内容不符），重建句柄再试一次")
            self.invalidate_gui()
            time.sleep(0.6)
            try:
                gui2 = self.gui
                uia2 = gui2._get_uia()
                if uia2 is not None:
                    try:
                        from wechatauto import accessibility

                        accessibility.wake([int(getattr(gui2, "main_hwnd", 0) or 0)])
                    except Exception:
                        pass
                    if uia2.open_chat(who) and uia2.send_text(body):
                        return True
            except Exception as e2:
                log.warning("UIA 重试异常 → %s：%r", who, e2)
            if uia.back_to_chat_tab() and uia.open_chat(who) and uia.send_text(body):
                return True
        except Exception as e:
            log.warning("UIA 发送异常 → %s：%r", who, e)
        return False

    def _send_ocr(self, who: str, body: str) -> bool:
        """坐标/OCR 兜底：自己粘贴、只按一次回车，以数据库落地为准，最多重来一次。

        库的 guia.send_msg 在"发送点击没生效（输入框没清空）"时会重试 3 次，
        可每次都其实发出去了 → 同一条消息三连发。这里把"确认"放到数据库侧：
        回车后 8 秒内看到自己这条落库就算成功；没落但输入框已空，也算成功
        （库有延迟）；只有明确没发出去才重来一次。
        """
        try:
            from wechatauto.guia import VK_A, VK_DELETE, VK_RETURN
        except Exception:
            VK_A, VK_DELETE, VK_RETURN = 0x41, 0x2E, 0x0D
        for attempt in (1, 2):
            mark = self._out_watermark(who)
            try:
                gui = self.gui
                if not gui.ensure_visible():
                    log.warning("微信窗口没醒过来，发不出去 → %s", who)
                    return False
                if not (who == getattr(gui, "_current_chat", None) and gui._chat_is_open(who)):
                    if not gui.open_chat(who):
                        time.sleep(1.0)
                        if not gui.open_chat(who):
                            log.warning("会话打不开（侧栏和搜索都没命中）→ %s", who)
                            return False
                    gui._current_chat = who
                    time.sleep(0.8)
                if not gui.input_text(body):
                    log.warning("输入框没粘进去（第 %d 次）→ %s", attempt, who)
                    time.sleep(1.0)
                    continue
                # 只按一次回车。库的 click_send 在"框里还有字"时会连按三次。
                gui._input.key(VK_RETURN)
            except Exception as e:
                log.warning("发送异常（第 %d 次）→ %s：%r", attempt, who, e)
                return False
            if self._landed(who, body, mark, 8.0):
                return True
            log.warning("回车后 8 秒没在库里等到（第 %d 次）→ %s", attempt, who)
            try:
                if not self.gui._input_box_has_text():
                    log.info("输入框已清空，按发送成功算（库还没落）→ %s", who)
                    return True
            except Exception:
                pass
            if attempt == 1:
                try:
                    self.gui._input.key(VK_A, ctrl=True)
                    self.gui._input.key(VK_DELETE)
                except Exception:
                    pass
                time.sleep(1.5)
        return False

    # ---------------- 桌面归还 ----------------
    @staticmethod
    def _foreground() -> int:
        try:
            return int(ctypes.windll.user32.GetForegroundWindow() or 0)
        except Exception:
            return 0

    def _is_quiet(self) -> bool:
        """静默模式（主人发了「/停」）：一律不抢前台、不弹窗、不动窗口尺寸。"""
        try:
            return bool(self.quiet and self.quiet())
        except Exception:
            return False

    def _ensure_window_size(self) -> None:
        """不强制尺寸了：只在窗口小于微信自身上限(520×452)时兜底放大，然后按当前尺寸校准布局。

        用户要求「拉大缩小都不影响功能」：所以真正的关键不是把窗口钉死成某个尺寸，
        而是每次尺寸变化后让库重新校准一次布局（否则点击比例全错、发送卡 75 秒）。
        """
        if self._is_quiet():
            return
        try:
            u = ctypes.windll.user32
            import wxrecover as _wr
            hwnd, _mw, _mhh = _wr.main_window()
            if not hwnd:
                return
            r = wintypes.RECT()
            u.GetWindowRect(hwnd, ctypes.byref(r))
            w, h = r.right - r.left, r.bottom - r.top
            if w < 520 or h < 452:      # 低于微信自己的下限，会被它弹回去，干脆不试
                u.ShowWindow(hwnd, 9)
                time.sleep(0.2)
                u.SetWindowPos(hwnd, 0, max(0, r.left), max(0, r.top), 520, 452,
                               0x0004 | 0x0010 | 0x0040)
                time.sleep(0.4)
                r = wintypes.RECT()
                u.GetWindowRect(hwnd, ctypes.byref(r))
                w, h = r.right - r.left, r.bottom - r.top
            if (w, h) == getattr(self, "_layout_size", None):
                return
            log.info("窗口尺寸 %s×%s（有变化），重新校准布局", w, h)
            try:
                self.gui.calibrate_layout(save=True)
            except Exception as e:
                log.warning("校准布局失败（继续试发）：%r", e)
            self._layout_size = (w, h)
            self._last_chat = ""     # 逼下一次重新打开聊天页，避免沿用旧坐标
        except Exception as e:
            log.debug("尺寸/布局检查失败：%r", e)

    def _quiet_foreground(self) -> None:
        """把微信提到前台，但不让库去最小化用户的其他窗口。"""
        if self._is_quiet():
            return
        try:
            gui = self.gui
            u = ctypes.windll.user32
            hwnd = int(getattr(gui, "main_hwnd", 0) or 0)
            if hwnd:
                from ctypes import wintypes as wt

                if u.IsIconic(hwnd):
                    u.ShowWindow(hwnd, 9)      # SW_RESTORE
                # 窗口被拖到屏幕外时（实测 left=-212），点击全部落空、会话都打不开
                r = wt.RECT()
                u.GetWindowRect(hwnd, ctypes.byref(r))
                sw, sh = u.GetSystemMetrics(0), u.GetSystemMetrics(1)
                w, h = r.right - r.left, r.bottom - r.top
                nx = min(max(0, r.left), max(0, sw - w))
                ny = min(max(0, r.top), max(0, sh - h - 60))
                if (nx, ny) != (r.left, r.top):
                    u.SetWindowPos(hwnd, 0, nx, ny, 0, 0, 0x0001 | 0x0004)
                    log.info("微信窗口挪回屏幕内：(%s,%s) → (%s,%s)", r.left, r.top, nx, ny)
                # 窗口太小（实测被拖到 794×561）时，输入框和发送按钮的比例全乱，
                # 库会「输入框探测连续失败 → 发送点击没生效」，一条消息卡一分半。
                u.SetForegroundWindow(hwnd)
            # 骗过 ensure_visible 的 15 秒缓存，跳过「自动最小化遮挡窗口」
            gui._last_visible_ok = True
            gui._last_visible_ts = time.time()
        except Exception as e:
            log.debug("前置微信失败（会走库的默认流程）：%r", e)

    def _give_back_desktop(self, fg: int) -> None:
        """把桌面还回去：取消微信置顶、还原被它最小化的遮挡窗口、恢复原来的前台窗口。

        wechatauto 发送前会「自动最小化遮挡窗口 + 把微信钉在最上层」，用完不主动
        收尾，桌面就一直被它占着。每发完一条都收一次，用户只会看到一两秒的闪动。
        """
        u = ctypes.windll.user32
        try:
            gui = self._gui
            if gui is not None:
                gui.release_foreground()
        except Exception as e:
            log.debug("还原遮挡窗口失败：%r", e)
        try:
            if fg and fg != u.GetForegroundWindow():
                u.SetForegroundWindow(fg)
        except Exception:
            pass

    # ---------------- 界面操作 ----------------
    @_ui_guard
    def accept_friends(
        self, limit: int = 5, dry_run: bool = False, names: list[str] | None = None
    ) -> dict:
        """通过待处理的好友申请（走 UIA 精确点击，见 wxui.py）。"""
        from wxui import WxUI

        ui = WxUI()
        return ui.accept_friends(limit=limit, dry_run=dry_run, names=names)

    @_ui_guard

    def send_image(self, path: str, target: str) -> bool:
        self._ensure_window_size()
        """发一张本地图片/表情（剪贴板粘贴路线，微信按图片消息发出）。"""
        who = self.display_name(target)
        fg = self._foreground()
        try:
            self._quiet_foreground()
            # 库里的 send_image 会拿路径做 .lower()，传 Path 会炸，必须先转成 str
            resp = self.gui.send_image(str(path), who, verify=False)
        except Exception as e:
            log.error("发表情异常 → %s：%r", who, e)
            return False
        finally:
            self._give_back_desktop(fg)
        ok = bool(resp.is_success) if hasattr(resp, "is_success") else bool(resp)
        if not ok:
            log.warning("表情没发出去 → %s：%s", who, resp)
        return ok

    @_ui_guard

    def send_favorite(self, target: str, index: Optional[int] = None) -> bool:
        self._ensure_window_size()
        """发微信「收藏」里的表情（真正的动画表情，不是图片）。"""
        from wechatauto.wx import Chat

        from wxui import WxUI

        who = self.display_name(target)
        fg = self._foreground()
        ok = False
        try:
            gui = self.gui
            self._quiet_foreground()
            # 刚给这个人发过字 → 会话已经开着，别再切一次（切一次就是一两秒）
            same_chat = getattr(self, "_last_chat", "") == who
            if not same_chat:
                Chat(who, gui=gui, db=self.db).Show()
                time.sleep(0.25)
                self._last_chat = who
            ui = WxUI()
            if not same_chat:
                ui.focus()
            panel = ui.open_emoji_panel()
            if panel is None:
                log.warning("打不开收藏表情面板 → %s", who)
                return False
            items = ui.favorite_emojis(panel)
            if not items:
                ui.close_emoji_panel()
                log.warning("收藏表情是空的 → %s", who)
                return False
            ok = ui.send_favorite_emoji(index)
        except Exception as e:
            log.error("发收藏表情异常 → %s：%r", who, e)
            return False
        finally:
            self._give_back_desktop(fg)
        if ok:
            log.info("发了个收藏表情 → %s", who)
        return ok

    @_ui_guard
    def poke(self, target: str) -> bool:
        """戳一戳（右键对方头像 → 拍一拍）。需要窗口可见、会话已打开。"""
        from wechatauto.wx import Chat

        from wxui import WxUI

        who = self.display_name(target)
        fg = self._foreground()
        try:
            gui = self.gui
            self._quiet_foreground()
            Chat(who, gui=gui, db=self.db).Show()
            time.sleep(0.8)
            ok = WxUI().poke(who, gui=gui)
        except Exception as e:
            log.error("戳一戳异常 → %s：%r", who, e)
            return False
        finally:
            self._give_back_desktop(fg)
        if not ok:
            log.warning("戳一戳没成 → %s（会话里要有对方的消息，窗口不能锁屏）", who)
        return ok

    # ---------------- 接收 ----------------
    should_skip = None  # callable(sender) -> True 表示这条消息直接不读

    def start(self, on_msg) -> None:
        """开始监听，把每条新消息转成 Msg 交给 on_msg。"""
        from wechatauto.db import Listener

        lst = Listener(self.db, interval=self.interval)

        def cb(m: dict, _lst) -> None:
            # 静音时只要不是主人，就别读了：不下载图、不做 OCR、不构建上下文，
            # 更不会碰界面（用户打游戏时不能被抢前台）。
            if self.should_skip is not None:
                try:
                    if self.should_skip(m.get("sender_username") or m.get("username") or ""):
                        return
                except Exception:
                    pass
            try:
                msg = self._convert(m)
            except Exception as e:  # 单条消息解析出错不能拖垮监听，也不能让这条消息"消失"
                log.exception("消息转换失败：%r", e)
                from bridge import Msg

                chat = m.get("username") or ""
                sender = m.get("sender_username") or chat
                msg = Msg(
                    msgid=f"{m.get('shard', '')}:{m.get('local_id')}:{chat}",
                    sender=sender,
                    room=chat if chat.endswith("@chatroom") else "",
                    text="[对方发来一条我没认出来的消息（可能是卡片/文件/特殊表情）]",
                    is_group=chat.endswith("@chatroom"),
                    alias="",
                )
            try:
                if msg is not None:
                    on_msg(msg)
            except Exception:  # 交给 on_msg 的异常同样要记下来
                log.exception("消息分发失败")

        lst.add_all(cb, discover=True)
        lst.start()
        self._listener = lst
        log.info("开始监听新消息（数据库增量轮询）")

    def catch_up(self, since_ts: float, sessions: int = 25, per_chat: int = 60) -> list:
        """补收：把「桥没在跑的那段时间」里进来的消息捞回来。

        库的监听只保证"不重复推送"，但它的水位是在**入队**时就提交的；进程要是
        在回复前挂掉/被重启，那条消息就再也不会来了（用户实测过：三个人里两个
        没回，就是这段空窗丢的）。所以启动时按"本地库最后一条消息的时间"往前捞。
        """
        out = []
        try:
            rows = self.db.get_sessions(limit=sessions)
        except Exception as e:
            log.warning("补收：拿不到会话列表 %r", e)
            return out
        for s in rows:
            user = s.get("username") or ""
            if not user:
                continue
            if self.should_skip is not None:
                try:
                    if self.should_skip(user):
                        continue
                except Exception:
                    pass
            try:
                msgs = self.db.get_new_messages(user, since_seq=0) or []
            except Exception:
                continue
            for m in msgs[-per_chat:]:
                try:
                    ts = float(m.get("create_time") or 0)
                except (TypeError, ValueError):
                    continue
                if ts <= since_ts:
                    continue
                # get_new_messages 返回的行里没有 username（是按 user 参数查的库），
                # 不补上的话 _convert 会当成"没有会话"：图片/表情取不到、群里还会回错人
                m.setdefault("username", user)
                # 转换前先判重：_convert 会下载图片/滚动会话（抢鼠标），
                # 已经处理过的消息每轮重放一遍就会一直抢桌面。
                known = getattr(self, "is_known", None)
                if known is not None:
                    try:
                        mid = "%s:%s:%s" % (m.get("shard"), m.get("local_id"), user)
                        if known(mid):
                            continue
                    except Exception:
                        pass
                try:
                    msg = self._convert(m)
                except Exception:
                    continue
                if msg is not None:
                    msg.ts = ts
                    msg.stale = (time.time() - ts) > 600  # 10 分钟前算"迟到"
                    out.append((ts, msg))
        out.sort(key=lambda pair: pair[0])
        return [msg for _, msg in out]

    _REFER_RE = re.compile(r"<refermsg>(.*?)</refermsg>", re.S)
    _REF_NAME_RE = re.compile(r"<displayname>(.*?)</displayname>", re.S)
    _REF_TEXT_RE = re.compile(r"<content>(.*?)</content>", re.S)

    @classmethod
    def _quote_prefix(cls, content: str, bot_names: list[str] | None = None) -> str:
        """把「引用（回复）某条消息」抽成一句话，放在正文前面。

        微信里引用别人/引用自己的话，存下来的都是 type=49 的 XML，`<refermsg>` 里带
        被引用的昵称和原文；她原来完全看不到，于是"对方在接哪句"全靠猜。
        """
        if not content or "<refermsg>" not in content:
            return ""
        try:
            m = cls._REFER_RE.search(content)
            if not m:
                return ""
            inner = m.group(1)
            m_name = cls._REF_NAME_RE.search(inner)
            m_text = cls._REF_TEXT_RE.search(inner)
            name = (m_name.group(1) if m_name else "").strip()
            raw = m_text.group(1) if m_text else ""
            raw = html_lib.unescape(raw)
            raw = re.sub(r"<[^>]+>", " ", raw)
            raw = re.sub(r"\s+", " ", raw).strip()
            if not raw:
                return ""
            if len(raw) > 60:
                raw = raw[:60] + "…"
            me = (bot_names or [])
            mine = bool(name) and any(n and n in name for n in me)
            who = "你" if mine else (name or "某条消息")
            return f"[对方引用了{'你说的话' if mine else '「' + who + '」说的话'}：{raw}] "
        except Exception:
            return ""

    def _convert(self, m: dict) -> "object | None":
        from bridge import Msg

        mtype = m.get("type") or ""
        chat = m.get("username") or ""
        sender = m.get("sender_username") or ""
        is_group = chat.endswith("@chatroom")
        from_self = m.get("sender_id") == 2 or sender == self.wxid
        alias = ""
        try:
            alias = self.db.get_nickname(sender) if sender else ""
        except Exception:
            pass
        if is_group and sender and (not alias or alias.startswith("wxid_")):
            # 群成员经常不在 contact 表里（例如刚进群的人），退回群成员昵称
            try:
                info = self.group_info(chat) if hasattr(self, "group_info") else None
                for mem in (info or {}).get("members", []):
                    if mem.get("wxid") == sender and mem.get("name"):
                        alias = mem["name"]
                        break
            except Exception:
                pass
        msgid = f"{m.get('shard', '')}:{m.get('local_id')}:{chat}"

        kind = "text"
        content = (m.get("content") or "").strip()
        # 群消息在库里是「发送人wxid:\n正文」，正文里再带着这个前缀，模型会把它
        # 当成奇怪的名字念出来（实测 11:53 回了「调教？你先练练打字吧」之前，
        # 看到的正文就是 wxid_v464…:\n我看你也是欠调教了呀）。说话人另有字段。
        if is_group and content.startswith("wxid_"):
            head, _, rest = content.partition("\n")
            if rest and head.rstrip(":：").startswith("wxid_"):
                content = rest.strip()
        media = ""
        # 「引用（回复）某条消息」：微信把它塞在 content 的 <refermsg> 里，
        # 她原来完全看不到被引用的原文，就只能瞎接。这里抽成一句话放正文前面。
        names_all = ([self.nick] if getattr(self, "nick", "") else []) + self._dynamic_names()
        quote = self._quote_prefix(self._raw_content(m) or content, names_all)
        if quote:
            import re as _re
            content = _re.sub(r"<refermsg>.*?</refermsg>", " ", content, flags=_re.S)
            content = _re.sub(r"<[^>]+>", " ", content)
            content = _re.sub(r"\s+", " ", content).strip()
            if not content:
                content = "[引用了一条消息]"

        # 拍一拍在各版本里类型不统一（系统消息 10000 / 卡片 49 / XML），
        # 所以先按文案认，再走类型分派——否则会被「卡片类不回」的规则吃掉。
        if "拍了拍" in content:
            return Msg(
                msgid=msgid, sender=sender or chat, room=chat if is_group else "",
                text="（对方拍了拍你）", at_me=True, is_group=is_group,
                alias=alias, from_self=from_self, kind="system", media="",
            )

        if mtype == "文本":
            if not content:
                return None
        elif mtype in ("图片", "动画表情"):
            kind = "sticker" if mtype == "动画表情" else "image"
            media = ""
            # 首选：消息 XML 里就有 CDN 地址，直接 HTTP 下原图——纯后台，不碰桌面。
            try:
                import media_in

                media = media_in.grab_from_msg(
                    self._raw_content(m), media_in.MEDIA_DIR / "in", tag=kind
                ) or ""
            except Exception as e:
                log.info("CDN 下载失败：%r", e)
            # 静默（/停）期间绝不点「查看原图」——那会把微信抬到前台。
            # 但消息本身照读，好让解除静音后她还记得这段里对方说了什么。
            if not media and not self._is_quiet():
                media = self._grab(m, chat)
            if media:
                # 给出本地路径：大脑可以用识图插件（vision_describe）真的去看这张图
                content = f"[{'表情' if kind == 'sticker' else '图片'}文件：{media}]"
                # 顺手用本机 OCR 带上图上文字，视觉工具不可用时也还有个抓手
                try:
                    import media_in

                    words = (media_in.ocr(media) or "").strip()
                    if len(words) >= 2:
                        content += f"\n[图上文字（本机 OCR，可能不准）：{words[:80]}]"
                except Exception:
                    pass
                if kind == "sticker":
                    # 收到别人发的表情就顺手收进库，用久了库就长起来了
                    try:
                        import stickers

                        stickers.add_file(media, "收到")
                    except Exception as e:
                        log.debug("收藏表情失败：%r", e)
            else:
                # 兜底：微信 4.x 有时连 CDN 地址都没给全。截图要抢桌面，默认关掉，
                # 需要时在 config.toml 的 [media] screenshot_fallback = true 打开。
                snap = self._snap(chat) if (self.screenshot_fallback and not self._is_quiet()) else ""
                if snap:
                    media = snap
                    content = f"[{'表情' if kind == 'sticker' else '图片'}截图文件：{snap}]"
                    if kind == "sticker":
                        # 截图裁出来的就是那张表情本身，收进库——以后可以原样发回去，
                        # 这才是「对方喜欢的那种」表情。
                        try:
                            import stickers

                            stickers.add_file(snap, "收到")
                        except Exception as e:
                            log.debug("收藏表情失败：%r", e)
                    try:
                        import media_in

                        words = (media_in.ocr(snap) or "").strip()
                        if len(words) >= 2:
                            content += f"\n[图上文字（本机 OCR，可能不准）：{words[:80]}]"
                    except Exception:
                        pass
                else:
                    content = ("[对方发来一个表情，但这次没取到图；就当个表情自然回应，别问他重发文件]"
                               if kind == "sticker"
                               else "[对方发来一张图，但这次没取到图；自然回应一句，别编图里的内容]")
        elif mtype == "语音":
            kind = "voice"
            content = "[一段语音]"
        elif mtype == "系统消息":
            kind = "system"
            if "拍了拍" not in content and "拍一拍" not in content:
                return None
        elif mtype in ("视频", "文件/链接/卡片", "位置", "音视频通话"):
            if quote and content and content != "[引用了一条消息]":
                kind = "text"        # 引用+附言，当普通文字回
            else:
                return None          # 这些先不回，免得瞎猜
        else:
            return None

        # 收到的表情如果其实是文字表情（[微笑] 这种），当文本处理更自然
        if kind == "sticker" and content and not content.startswith("["):
            kind = "text"

        at_me = False
        if is_group and not from_self:
            names = ([self.nick] if self.nick else []) + self._dynamic_names()
            markers = [f"@{n}" for n in names]
            at_me = any(k and k in content for k in markers)
        if quote and not content.startswith(quote):
            content = quote + content
        return Msg(
            msgid=msgid,
            sender=sender or chat,
            room=chat if is_group else "",
            text=content,
            at_me=at_me,
            is_group=is_group,
            alias=alias,
            from_self=from_self,
            kind=kind,
            media=media,
        )

    def _nudge_scroll(self, chat: str) -> None:
        """把会话滚到底：库要"图片气泡在可视区"才能点原图，窗口不在那条消息上就抓不到。"""
        try:
            import ctypes
            import time as _t

            self.gui.ensure_visible()
            from wechatauto.wx import Chat as _Chat

            _Chat(chat, gui=self.gui, db=self.db).Show()
            _t.sleep(0.5)
            hwnd = int(ctypes.windll.user32.FindWindowW("Qt51514QWindowIcon", "微信") or 0)
            if not hwnd:
                return
            # 先按 End 把焦点拉到最新一条，再往下滚一点
            for _ in range(2):
                ctypes.windll.user32.PostMessageW(hwnd, 0x0100, 0x23, 0)  # WM_KEYDOWN VK_END
                ctypes.windll.user32.PostMessageW(hwnd, 0x0101, 0x23, 0)
            for _ in range(3):
                ctypes.windll.user32.PostMessageW(hwnd, 0x020A, 0, 0xFF880000)  # WM_MOUSEWHEEL 下滚
                _t.sleep(0.2)
            _t.sleep(0.6)
        except Exception as e:
            log.debug("滚动会话失败：%r", e)

    def _grab(self, m: dict, chat: str) -> str:
        try:
            import media_in

            if self._is_quiet():
                # 静默模式：只读本地库，绝不点「查看原图」、绝不滚动会话（会抢前台）
                return media_in.grab(self.db, chat, m.get("local_id") or 0, prefer_original=False) or ""

            kw = dict(prefer_original=self.fetch_original,
                      chat_name=self.display_name(chat) if chat else None)
            lid = m.get("local_id") or 0
            path = media_in.grab(self.db, chat, lid, **kw)
            if not path:
                # 第一次没拿到：多半是这条图不在可视区，滚一下再试
                log.info("取图失败，滚动会话后重试：%s#%s", chat, lid)
                self._nudge_scroll(chat)
                path = media_in.grab(self.db, chat, lid, **kw)
            return path or ""
        except Exception as e:
            log.info("媒体落盘失败：%r", e)
            return ""

    @staticmethod
    def _raw_content(m: dict) -> str:
        """原始消息 XML（有的行 content 被截断过，尽量拿全的）。"""
        for k in ("content", "raw", "xml"):
            v = m.get(k)
            if v:
                return str(v)
        return ""

    def _snap(self, chat: str) -> str:
        if self._is_quiet():
            return ""
        """兜底看图：打开这个会话，截会话底部那条消息（图/表情气泡）。"""
        try:
            import media_in
            import wxui

            gui = self.gui
            try:
                gui.ensure_visible()
                gui._get_uia()          # 顺手把 UIA 热激活，否则控件树是空的
            except Exception:
                pass
            from wechatauto.wx import Chat

            Chat(chat, gui=gui, db=self.db).Show()
            import time as _t

            _t.sleep(0.6)
            # 只截聊天区左下这一块：对方的气泡在左边，自己发的绿色气泡在右边，
            # 整屏截图会让大脑误以为「对方发的是聊天记录截图」。
            try:
                import ctypes

                hwnd = int(ctypes.windll.user32.FindWindowW("Qt51514QWindowIcon", "微信") or 0)
            except Exception:
                hwnd = 0
            return media_in.snap_chat_bottom(wxui.WxUI(), hwnd=hwnd, height=340) or ""
        except Exception as e:
            log.info("截图兜底失败：%r", e)
            return ""

    @staticmethod
    def _describe(path: str) -> str:
        try:
            import media_in

            return media_in.describe(path)
        except Exception as e:
            log.info("看图失败：%r", e)
            return "[一张图，没看清]"

    def stop(self) -> None:
        if self._listener is not None:
            try:
                self._listener.stop()
            except Exception:
                pass
        self._listener = None


def preflight() -> str:
    """启动前自检：微信是否登录、控件树是否可用。返回一句人话。"""
    from wechatauto.db import WeChatDB

    db = WeChatDB()
    info = db.get_self_info()
    if not info.get("username"):
        raise RuntimeError("没有找到已登录的微信账号，请先在桌面上登录微信。")
    return f"微信已登录：{info.get('nick_name') or info.get('username')}"
