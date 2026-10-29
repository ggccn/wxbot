"""微信客户端界面操作（非聊天）——通讯录、设置、资料页。

只做「精确定位 + 点击」这一件事：所有控件都靠 UIA 里的名字/类名找，
不猜像素，找不到就明确报错，绝不盲点。

前置条件：`wechatauto` 已热激活 UIA 树（桥在跑就一定是激活状态）。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

import uiautomation as auto
from paths import DATA_DIR

log = logging.getLogger("wxui")

_RETRY = 3

# uiautomation 不是线程安全的；桥的多个线程都可能碰界面，统一串行。
UI_LOCK = threading.RLock()


class UINotFound(RuntimeError):
    pass


def _wake_uia(hwnd: int) -> bool:
    """热写 accessibility gate，让微信 4.x 把 mmui 控件树物化出来。"""
    try:
        from wechatauto import accessibility

        ok = accessibility.wake([hwnd])
        log.info("热激活 UIA：%s", ok)
        return bool(ok)
    except Exception as e:
        log.warning("热激活 UIA 失败：%r", e)
        return False


class WxUI:
    def __init__(self, main_hwnd: Optional[int] = None):
        import ctypes

        import uiautomation as auto

        self.auto = auto
        self.main_hwnd = main_hwnd or ctypes.windll.user32.FindWindowW("Qt51514QWindowIcon", "微信")
        if not self.main_hwnd:
            self.main_hwnd = ctypes.windll.user32.FindWindowW("Qt51514QWindowIcon", None)
        if not self.main_hwnd:
            raise UINotFound("找不到微信主窗口")
        # 微信 4.x 冷启动时控件树是空壳，需要热写一次 gate。
        # 独立脚本（wxctl）也得自己唤醒，不能指望桥那边已经做过。
        self._woken = _wake_uia(self.main_hwnd)
        self.root = auto.ControlFromHandle(self.main_hwnd)

    # ---------------- 基础 ----------------
    def _walk(self, ctrl, depth=0, max_depth=24):
        if depth > max_depth:
            return
        try:
            kids = ctrl.GetChildren()
        except Exception:
            return
        for k in kids:
            yield k
            yield from self._walk(k, depth + 1, max_depth)

    def find(self, name: str, cls: Optional[str] = None, exact: bool = True,
             root=None, timeout: float = 0.0) -> Optional[object]:
        """按 UIA 的 Name（和可选 ClassName）找控件。"""
        deadline = time.time() + timeout
        while True:
            base = root if root is not None else self.root
            for c in self._walk(base):
                try:
                    cname = c.Name or ""
                    ccls = c.ClassName or ""
                except Exception:
                    continue
                hit = (cname == name) if exact else (name in cname)
                if hit and (cls is None or ccls == cls):
                    return c
            if time.time() >= deadline:
                return None
            time.sleep(0.4)

    def center(self, ctrl) -> tuple[int, int]:
        r = ctrl.BoundingRectangle
        return (r.left + r.right) // 2, (r.top + r.bottom) // 2

    def click(self, ctrl, settle: float = 0.8) -> bool:
        try:
            x, y = self.center(ctrl)
        except Exception as e:
            log.warning("拿不到控件位置：%s", e)
            return False
        import ctypes

        u = ctypes.windll.user32
        u.SetCursorPos(int(x), int(y))
        time.sleep(0.1)
        u.mouse_event(0x0002, 0, 0, 0, 0)
        time.sleep(0.05)
        u.mouse_event(0x0004, 0, 0, 0, 0)
        time.sleep(settle)
        return True

    def focus(self) -> None:
        import ctypes
        from ctypes import wintypes as wt

        u = ctypes.windll.user32
        u.ShowWindow(self.main_hwnd, 9)  # SW_RESTORE

        # 窗口跑到屏幕外（实测出现过 left=-212，左侧 Tab 栏整条看不见）时，
        # 所有按坐标/按控件的点击都会落空——先把它拉回屏幕内。
        try:
            r = wt.RECT()
            u.GetWindowRect(self.main_hwnd, ctypes.byref(r))
            sw = u.GetSystemMetrics(0)
            sh = u.GetSystemMetrics(1)
            w, h = r.right - r.left, r.bottom - r.top
            nx = min(max(0, r.left), max(0, sw - w))
            ny = min(max(0, r.top), max(0, sh - h - 60))
            if (nx, ny) != (r.left, r.top):
                u.SetWindowPos(self.main_hwnd, 0, nx, ny, 0, 0, 0x0001 | 0x0004)
                log.info("微信窗口挪回屏幕内：(%s,%s) → (%s,%s)", r.left, r.top, nx, ny)
                time.sleep(0.3)
        except Exception as e:
            log.debug("窗口位置校正失败：%r", e)

        # 后台进程直接 SetForegroundWindow 经常被系统拒（窗口不会真的到前台，
        # 于是后面的鼠标点击全落在别的窗口上——「点不动」就是这么来的）。
        # 先敲一下 Alt 解锁前台限制，再 SetForegroundWindow。
        try:
            u.keybd_event(0x12, 0, 0, 0)
            u.keybd_event(0x12, 0, 2, 0)
        except Exception:
            pass
        u.SetForegroundWindow(self.main_hwnd)
        time.sleep(0.15)
        if u.GetForegroundWindow() != self.main_hwnd:
            try:
                u.SetWindowPos(self.main_hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002)
                u.SetForegroundWindow(self.main_hwnd)
                u.SetWindowPos(self.main_hwnd, -2, 0, 0, 0, 0, 0x0001 | 0x0002)
            except Exception:
                pass
        time.sleep(0.4)

    # ---------------- 导航 ----------------
    def tab(self, name: str) -> bool:
        """切主界面左侧 Tab：微信 / 通讯录 / 收藏 / 发现。"""
        self.focus()
        tabbar = self.find("导航", cls="mmui::MainTabBar") or self.root
        btn = self.find(name, cls="mmui::XTabBarItem", root=tabbar, timeout=2.0)
        if btn is None:
            return False
        return self.click(btn, settle=1.0)

    def open_more(self) -> bool:
        """点左下角「更多」（三横线），弹出菜单。"""
        self.focus()
        btn = self.find("更多", cls="mmui::MainTabBarSettingView", timeout=2.0)
        if btn is None:
            btn = self.find("更多", cls="mmui::XButton", timeout=1.0)
        if btn is None:
            return False
        return self.click(btn, settle=1.0)

    def dump(self, path: str, max_depth: int = 16) -> int:
        lines = []

        def walk(c, d=0):
            if d > max_depth:
                return
            try:
                lines.append("  " * d + f"{c.ControlTypeName} | {c.ClassName} | {c.Name[:80]!r}")
                for k in c.GetChildren():
                    walk(k, d + 1)
            except Exception:
                pass

        walk(self.root)
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        return len(lines)

    # ---------------- 戳一戳 ----------------
    def poke(self, who: str, gui=None) -> bool:
        """对当前会话里的对方「拍一拍」。

        微信 4.x 的拍一拍菜单是自绘的、UIA 读不到，只能：右键对方头像 → 截图 →
        放大 OCR 找「拍一拍」→ 点它。wechatauto 自带的那份用整屏 OCR，实测识别
        不到（字太小），这里改成裁剪 + 3 倍放大再 OCR。
        """
        import ctypes

        u = ctypes.windll.user32
        with UI_LOCK:
            if gui is None:
                return False
            uia = gui._get_uia()
            if uia is None:
                log.warning("UIA 驱动不可用，戳不了")
                return False
            if not gui.ensure_visible():
                log.warning("微信窗口不可见（锁屏？），戳不了")
                return False
            row = uia._find_friend_row()
            if row is None:
                log.info("会话里没有对方的可见消息，戳不了")
                return False
            try:
                r = row.BoundingRectangle
            except Exception:
                return False
            # 头像在行最左侧（约 20-40px 宽），右键它才会出「拍一拍」
            ax = int(r.left + 22)
            ay = int(r.top + (r.bottom - r.top) * 0.5)
            for attempt in range(2):
                uia._set_cursor(ax, ay)
                time.sleep(0.25)
                uia._right_click()
                time.sleep(1.0)
                hit = self._find_poke_item(ax, ay)
                if hit:
                    uia._set_cursor(*hit)
                    time.sleep(0.2)
                    uia._left_click()
                    time.sleep(0.6)
                    log.info("戳了 %s 一下", who)
                    return True
                u.keybd_event(0x1B, 0, 0, 0)
                u.keybd_event(0x1B, 0, 2, 0)
                time.sleep(0.4)
            return False

    # ---------------- 收藏表情 ----------------
    PANEL_CLASS = "Qt51514QWindowToolSaveBits"

    def _emoji_panel(self, timeout: float = 2.5):
        """表情面板是独立窗口，类名 Qt51514QWindowToolSaveBits。"""
        import ctypes

        u = ctypes.windll.user32
        deadline = time.time() + timeout
        while True:
            hits = []

            @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
            def cb(h, l):
                if not u.IsWindowVisible(h):
                    return True
                buf = ctypes.create_unicode_buffer(256)
                u.GetClassNameW(h, buf, 256)
                if buf.value == self.PANEL_CLASS:
                    hits.append(h)
                return True

            u.EnumWindows(cb, None)
            for h in hits:
                try:
                    ctl = auto.ControlFromHandle(h)
                    r = ctl.BoundingRectangle
                    if r.right - r.left > 100:
                        return ctl
                except Exception as e:
                    log.warning("表情面板取控件失败（hwnd=%s）：%r", h, e)
                    continue
            if hits:
                log.debug("表情面板窗口在，但拿不到控件（hits=%s）", hits)
            if time.time() >= deadline:
                return None
            time.sleep(0.2)

    def open_emoji_panel(self):
        panel = self._emoji_panel(0.6)
        for attempt in range(3):
            if panel is not None:
                return panel
            with UI_LOCK:
                # 会话切换后控件树会重建，先刷新根节点，否则找不到输入框工具条
                try:
                    self.root = auto.ControlFromHandle(self.main_hwnd)
                except Exception:
                    pass
                btn = self.find("发送表情", exact=False, timeout=2)
                if btn is None:
                    log.warning("找不到「发送表情」按钮")
                    return None
                self.click(btn, settle=0.6)
            panel = self._emoji_panel(2.0)
            if panel is None:
                log.info("表情面板没打开（第 %s 次），重试一次", attempt + 1)
        return panel

    def close_emoji_panel(self) -> None:
        import ctypes

        u = ctypes.windll.user32
        u.keybd_event(0x1B, 0, 0, 0)
        u.keybd_event(0x1B, 0, 2, 0)

    def favorite_emojis(self, panel=None) -> list:
        """面板里「自定义表情」那一格的所有收藏表情控件，按行列排序。"""
        p = panel or self._emoji_panel(0.5)
        if p is None:
            return []
        try:
            for c in self._walk(p):
                if c.ClassName == "mmui::EmoticonToolbarItem" and (c.Name or "") == "自定义表情":
                    self.click(c, settle=0.7)
                    break
        except Exception:
            pass
        items = []
        try:
            for c in self._walk(p):
                if c.ClassName == "mmui::FavEmoticonItemView":
                    items.append(c)
        except Exception:
            return []
        try:
            items.sort(key=lambda c: (c.BoundingRectangle.top, c.BoundingRectangle.left))
        except Exception:
            pass
        return items

    def send_favorite_emoji(self, index=None) -> bool:
        """发一张微信里收藏的表情（真·表情消息，不是图片文件）。index 为空就随机。"""
        import random

        with UI_LOCK:
            panel = self.open_emoji_panel()
            if panel is None:
                log.warning("表情面板没打开")
                return False
            items = self.favorite_emojis(panel)
            if not items:
                log.warning("收藏里没找到表情（面板里没有 FavEmoticonItemView）")
                self.close_emoji_panel()
                return False
            if index is None:
                index = random.randrange(len(items))
            index = max(0, min(index, len(items) - 1))
            ok = self.click(items[index], settle=0.8)
            log.info("发了收藏表情第 %s 张（共 %s 张）", index + 1, len(items))
            return bool(ok)

    def _find_poke_item(self, cx: int, cy: int) -> Optional[tuple[int, int]]:
        """右键后截鼠标周围一小块、放大 5 倍做 OCR，找「拍一拍」并返回屏幕坐标。

        实测整窗/整屏 OCR 都识别不到这个菜单项（字太小，屏幕缩放后只有十几像素），
        裁一小块放大 5 倍就能稳定读出「拍一拍」。
        """
        try:
            from pathlib import Path

            from PIL import ImageGrab

            import media_in
        except Exception:
            return None
        left, top = max(0, cx - 30), max(0, cy - 60)
        right, bottom = cx + 220, cy + 90
        try:
            crop = ImageGrab.grab(bbox=(left, top, right, bottom), all_screens=True)
        except Exception:
            return None
        scale = 5
        big = crop.resize((crop.width * scale, crop.height * scale))
        tmp = DATA_DIR / "_menu_ocr.png"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        big.save(tmp)
        words = media_in.ocr_words(str(tmp))
        joined = "".join(w[0] for w in words).replace(" ", "")
        if "拍一拍" not in joined:
            log.info("右键菜单 OCR 没读到「拍一拍」（读到：%s）", joined[:40])
            return None
        for text, x, y, w, h in words:
            if "拍" in text:
                return (left + int((x + w / 2) / scale), top + int((y + h / 2) / scale))
        return None

    # ---------------- 好友申请 ----------------
    def _open_new_friends(self) -> bool:
        """进「通讯录」→ 点「新的朋友」，把申请列表显示出来。

        注意：已经在这个页面时再点一次会把页面收回去，所以先看看有没有
        申请行，有就直接当已经打开了。
        """
        self.tab("通讯录")
        if self._request_rows(wait=0.0):
            return True
        row = self.find("新的朋友", cls="mmui::ContactsCellGroupView", timeout=3.0)
        if row is None:
            row = self.find("新的朋友", timeout=2.0)
        if row is None:
            log.warning("通讯录里没找到「新的朋友」入口")
            return False
        self.click(row, settle=1.2)
        return True

    def _request_rows(self, wait: float = 3.0) -> list:
        """当前「新的朋友」页里的待验证申请行，按屏幕从上到下排序。"""
        rows = []
        deadline = time.time() + wait
        while True:
            rows = []
            for c in self._walk(self.root, max_depth=16):
                try:
                    if c.ClassName == "mmui::ContactsCellNewFriendView" and (c.Name or "").strip():
                        nm = c.Name
                        if "已添加" in nm or "已通过" in nm:
                            continue  # 已经处理过的，不是待办
                        r = c.BoundingRectangle
                        rows.append((r.top, c))
                except Exception:
                    continue
            if rows or time.time() >= deadline:
                break
            time.sleep(0.4)
        rows.sort(key=lambda x: x[0])
        return [c for _, c in rows]

    def pending_friend_requests(self) -> list[str]:
        """返回待通过的申请（「新的朋友」页里的行）。"""
        with UI_LOCK:
            if not self._open_new_friends():
                return []
            time.sleep(0.35)  # 页面切过去要一点时间，立刻扫会扫空
            return [c.Name for c in self._request_rows()]

    def accept_friends(
        self, limit: int = 5, dry_run: bool = False, names: Optional[list[str]] = None
    ) -> dict:
        """通过好友申请。dry_run=True 只走到「确定」按钮前面，不真点。

        names 给了就只处理名字里含这些片段的申请（比如 ["张三"]），别的陌生人不动。
        返回 {"seen": [...], "accepted": n, "dry_run": bool}
        """
        seen: list[str] = []
        accepted = 0
        with UI_LOCK:
            if not self._open_new_friends():
                return {"seen": [], "accepted": 0, "dry_run": dry_run, "error": "进不了新的朋友页"}
            if names:
                # 名字匹配要严：之前用不分大小写的子串匹配，"z" 把 "zh…" 也匹配上了，
                # 结果同意了错的人（实测）。先按"以这个词开头、区分大小写"精确匹配，
                # 匹配到多个就算歧义，宁可不动手，也不能点错。
                all_rows = self._request_rows()
                exact = [c for c in all_rows
                         if any((c.Name or "").startswith(n) for n in names)]
                loose = [c for c in all_rows
                         if any(n.lower() in (c.Name or "").lower() for n in names)]
                cand = exact or loose
                if len(cand) > 1:
                    return {"seen": [c.Name for c in cand], "accepted": 0,
                            "dry_run": dry_run, "ambiguous": True}
                if not cand:
                    return {"seen": [c.Name for c in all_rows], "accepted": 0,
                            "dry_run": dry_run, "ambiguous": False}
            while accepted < limit:
                rows = self._request_rows()
                if names:
                    rows = [c for c in rows if any(n in (c.Name or "") for n in names)]
                row = None
                for c in rows:
                    if c.Name not in seen:
                        row = c
                        break
                if row is None:
                    break
                name = row.Name
                seen.append(name)
                self.click(row, settle=1.2)
                go = self.find("前往验证", cls="mmui::XOutlineButton", timeout=3.0)
                if go is None:
                    # 右侧详情面板有时要等一会儿才刷出来，或者第一次点击只是"选中"，
                    # 再点一次就有了（实测 zh 这条第一次点完没有按钮）。
                    self.click(row, settle=1.0)
                    go = self.find("前往验证", cls="mmui::XOutlineButton", timeout=6.0)
                if go is None:
                    log.warning("申请 %s 点开后没找到「前往验证」", name)
                    break
                self.click(go, settle=2.0)

                import ctypes

                u = ctypes.windll.user32
                hwnd = u.FindWindowW("Qt51514QWindowIcon", "通过朋友验证")
                if not hwnd:
                    log.warning("没等来「通过朋友验证」窗口")
                    break
                u.SetForegroundWindow(hwnd)
                time.sleep(0.4)
                vr = self.auto.ControlFromHandle(hwnd)
                ok = self.find("确定", cls="mmui::XOutlineButton", root=vr, timeout=2.0)
                cancel = self.find("取消", cls="mmui::XOutlineButton", root=vr, timeout=1.0)
                if ok is None:
                    log.warning("验证窗口里没有「确定」")
                    if cancel is not None:
                        self.click(cancel, settle=0.6)
                    break
                if dry_run:
                    log.info("[dry-run] 停在「确定」前，申请：%s", name)
                    if cancel is not None:
                        self.click(cancel, settle=0.6)
                    break
                self.click(ok, settle=1.6)
                accepted += 1
                log.info("已通过好友申请：%s", name)
            self.tab("微信")
        return {"seen": seen, "accepted": accepted, "dry_run": dry_run}
