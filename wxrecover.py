"""救微信：卡死检测、关掉挡路的弹窗、必要时重启并自动点「登录」。

微信 4.x 的界面线程很容易被自动化拖死（这次是「搜索聊天记录」窗口把主窗卡住），
主窗一 hung，桥的每次发送都会卡 75 秒再失败。这里给桥一个自愈入口。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import logging
import pathlib
import subprocess
import time
from paths import DATA_DIR

log = logging.getLogger("wxrecover")

MAIN_TITLE = "微信"
WECHAT_EXE = r"E:\wx\Weixin\Weixin.exe"
_last_restart = 0.0
_splash_warned = False
_minimized_logged = False   # 最小化只提醒一次，别每分钟刷日志
_tray_logged = False        # 托盘/窗口隐藏只提醒一次

STRAY_TITLES = {"搜索聊天记录", "搜索", "设置", "通过朋友验证", "错误报告", "提示", "添加朋友"}


def _enum():
    u = ctypes.windll.user32
    out = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def cb(h, _l):
        try:
            cls = ctypes.create_unicode_buffer(64)
            u.GetClassNameW(h, cls, 64)
            if not cls.value.startswith("Qt51514"):
                return True
            if not u.IsWindowVisible(h):
                return True
            t = ctypes.create_unicode_buffer(128)
            u.GetWindowTextW(h, t, 128)
            r = wt.RECT()
            u.GetWindowRect(h, ctypes.byref(r))
            out.append((int(h), t.value, r.right - r.left, r.bottom - r.top,
                        (r.left, r.top)))
        except Exception:
            pass
        return True

    u.EnumWindows(cb, None)
    return out


def main_window():
    """找主窗：宽的那扇是主窗；最小化时 rect 会变成 ~160x28，也要认出来。"""
    u = ctypes.windll.user32
    for h, t, w, hh, _pos in _enum():
        if t != MAIN_TITLE:
            continue
        if w >= 500 and hh >= 380:   # 微信最小可缩到 520×452
            return h, w, hh
        if u.IsIconic(h):          # 最小化：尺寸不可信，但它是登录后的主窗
            return h, w, hh
    return 0, 0, 0


def is_hung(hwnd: int) -> bool:
    u = ctypes.windll.user32
    res = ctypes.c_ulong(0)
    ok = u.SendMessageTimeoutW(hwnd, 0, 0, 0, 0x0002, 1500, ctypes.byref(res))
    return not ok or res.value == 0


def close_strays() -> list[str]:
    u = ctypes.windll.user32
    closed = []
    for h, t, _w, _hh, _pos in _enum():
        if t in STRAY_TITLES:
            u.PostMessageW(h, 0x0010, 0, 0)
            closed.append(t)
    if closed:
        time.sleep(1.0)
    return closed


def _click(h: int, x: int, y: int) -> None:
    u = ctypes.windll.user32
    u.SetForegroundWindow(h)
    time.sleep(0.3)
    u.SetCursorPos(x, y)
    time.sleep(0.15)
    u.mouse_event(0x0002, 0, 0, 0, 0)
    u.mouse_event(0x0004, 0, 0, 0, 0)


def ensure_size(target=(520, 452)) -> bool:
    """只保底：小于微信自身下限(520×452)才放大；用户拉大拉小都不动它。"""
    h, w, hh = main_window()
    if not h:
        return False
    if w >= target[0] and hh >= target[1]:
        return True
    u = ctypes.windll.user32
    r = wt.RECT()
    u.GetWindowRect(h, ctypes.byref(r))
    u.ShowWindow(h, 9)
    time.sleep(0.3)
    x = max(0, min(r.left, max(0, u.GetSystemMetrics(0) - target[0])))
    y = max(0, min(r.top, max(0, u.GetSystemMetrics(1) - target[1] - 60)))
    u.SetWindowPos(h, 0, x, y, target[0], target[1], 0x0004 | 0x0010 | 0x0040)
    time.sleep(0.4)
    return True


_TOPMOST_SAVED: list[int] = []


def demote_topmost(keep: int = 0) -> int:
    """把置顶且盖住微信的窗口（模拟器/播放器）临时降下来，返回降了几个。

    MuMu 这类窗口是 WS_EX_TOPMOST，SetForegroundWindow 抢不过它，
    键盘输入会继续进到模拟器里，于是"发送失败：输入框没清空"。
    """
    u = ctypes.windll.user32
    demoted = 0

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def cb(h, _l):
        nonlocal demoted
        try:
            h = int(h)
            if h == keep or not u.IsWindowVisible(h):
                return True
            if u.IsIconic(h):
                return True
            ex = u.GetWindowLongW(h, -20)  # GWL_EXSTYLE
            if not (ex & 0x00000008):      # WS_EX_TOPMOST
                return True
            r = wt.RECT()
            u.GetWindowRect(h, ctypes.byref(r))
            if r.right - r.left < 300 or r.bottom - r.top < 200:
                return True
            # 只降级「真的压在微信上面」的窗口，别动用户自己开着的游戏/模拟器
            mh, _mw, _mhh = main_window()
            if mh:
                mr = wt.RECT()
                u.GetWindowRect(mh, ctypes.byref(mr))
                overlap = (r.left < mr.right and r.right > mr.left and
                           r.top < mr.bottom and r.bottom > mr.top)
                if not overlap:
                    return True
            u.SetWindowPos(h, -2, 0, 0, 0, 0, 0x0002 | 0x0001 | 0x0010)  # NOTOPMOST
            _TOPMOST_SAVED.append(h)
            demoted += 1
        except Exception:
            pass
        return True

    u.EnumWindows(cb, None)
    if demoted:
        log.info("临时降级 %d 个置顶窗口（免得挡住微信）", demoted)
        time.sleep(0.2)
    return demoted


def restore_topmost() -> None:
    u = ctypes.windll.user32
    for h in _TOPMOST_SAVED:
        try:
            if u.IsWindow(h):
                u.SetWindowPos(h, -1, 0, 0, 0, 0, 0x0002 | 0x0001 | 0x0010)  # TOPMOST
        except Exception:
            pass
    _TOPMOST_SAVED.clear()


def _invoke_login_button(hwnd: int) -> bool:
    """用 UIA 直接 Invoke「登录」按钮 —— 被别的窗口盖住时鼠标点不进去，这个能。"""
    try:
        import uiautomation as auto

        root = auto.ControlFromHandle(hwnd)

        def walk(c, depth=0, maxd=18):
            try:
                if (c.Name or "") == "登录" and c.ControlTypeName == "ButtonControl":
                    return c
            except Exception:
                pass
            if depth > maxd:
                return None
            try:
                kids = c.GetChildren()
            except Exception:
                return None
            for k in kids:
                got = walk(k, depth + 1, maxd)
                if got:
                    return got
            return None

        btn = walk(root)
        if not btn:
            return False
        btn.GetInvokePattern().Invoke()
        log.info("已用 UIA Invoke 点「登录」")
        return True
    except Exception as e:
        log.warning("UIA 点登录失败：%r", e)
        return False


def restart(timeout: float = 90.0) -> bool:
    """杀掉微信 → 重新启动 → 自动点「登录」→ 等主窗出现。"""
    log.warning("微信卡死，重启客户端")
    subprocess.run(["taskkill", "/F", "/IM", "Weixin.exe"],
                   capture_output=True, creationflags=0x08000000)
    time.sleep(3.0)
    subprocess.Popen([WECHAT_EXE], creationflags=0x08000000)
    t0 = time.time()
    clicked = False
    while time.time() - t0 < timeout:
        time.sleep(1.5)
        for h, t, w, hh, pos in _enum():
            if t != MAIN_TITLE:
                continue
            if w >= 600:                      # 已经进主界面
                ensure_size()
                log.info("微信已重新登录（主窗 %s×%s）", w, hh)
                return True
            if not clicked and w < 400:
                demote_topmost()
                ok = _invoke_login_button(h)
                if not ok:
                    _click(h, pos[0] + w // 2, pos[1] + int(hh * 0.75))
                restore_topmost()
                clicked = True
                log.info("已尝试登录，等服务端放行")
    log.error("重启微信超时（%.0fs），需要人工看一眼", timeout)
    return False


def _try_login_window(timeout: float = 20.0) -> bool:
    """停在登录窗（宽 < 400）时点一次「登录」，不杀进程。返回是否点到了。"""
    u = ctypes.windll.user32
    for h, t, w, hh, pos in _enum():
        if t != MAIN_TITLE or w >= 600 or w < 200:
            continue
        log.info("微信停在登录窗（%s×%s），点一次「登录」", w, hh)
        try:
            u.ShowWindow(h, 9)
            u.SetForegroundWindow(h)
        except Exception:
            pass
        demote_topmost()
        if _invoke_login_button(h):
            log.info("已用 UIA 点「登录」")
        else:
            _click(h, pos[0] + w // 2, pos[1] + int(hh * 0.75))
            log.info("已用鼠标点「登录」")
        restore_topmost()
        t0 = time.time()
        while time.time() - t0 < timeout:
            time.sleep(1.0)
            if main_window()[0]:
                return True
        return False
    return False



LOGIN_STATE = DATA_DIR / "login_pending.json"


def _login_window():
    """返回停在登录界面的那个「微信」窗（约 296×388，且不是最小化状态）。"""
    u = ctypes.windll.user32
    for h, t, w, hh, _pos in _enum():
        if t != MAIN_TITLE:
            continue
        if u.IsIconic(h):
            continue
        if 280 <= w <= 320 and 350 <= hh <= 420:   # 「正在进入/登录」小窗固定约 296×388
            return h, w, hh
    return 0, 0, 0


def _login_pending_since() -> float:
    try:
        return float(json.loads(LOGIN_STATE.read_text(encoding="utf-8")).get("since", 0))
    except Exception:
        return 0.0


def _mark_login_pending() -> None:
    LOGIN_STATE.parent.mkdir(parents=True, exist_ok=True)
    LOGIN_STATE.write_text(json.dumps({"since": time.time()}, ensure_ascii=False), encoding="utf-8")


def _clear_login_pending() -> None:
    try:
        LOGIN_STATE.unlink(missing_ok=True)
    except Exception:
        pass


def weixin_pids() -> list[int]:
    """当前还在跑的 Weixin.exe（主进程+子进程）。查不到就返回空表。"""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq Weixin.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=8, creationflags=0x08000000)
    except Exception as e:
        log.debug("查微信进程失败：%r", e)
        return []
    pids: list[int] = []
    for line in (out.stdout or "").splitlines():
        parts = [p.strip().strip('"') for p in line.split('","')]
        if len(parts) >= 2 and parts[0].lower() == "weixin.exe":
            try:
                pids.append(int(parts[1]))
            except ValueError:
                pass
    return pids


def _launch_client() -> bool:
    """确认微信进程真的没了，才启动客户端（不杀任何东西）。"""
    global _last_restart, _tray_logged
    pids = weixin_pids()
    if pids:
        # 进程在 = 微信只是缩到托盘 / 主窗隐藏（4.x 关窗就是缩托盘），
        # 这时再 Popen 一次会触发单实例激活，把窗口弹到前台 ——
        # 用户看到的就是「/停 之后微信登录页被反复拉起」。所以绝不启动。
        if not _tray_logged:
            _tray_logged = True
            log.info("微信进程还在（pid %s），只是窗口不可见：不动它，发消息时会自己还原",
                     ",".join(str(p) for p in pids[:3]))
        return True
    _tray_logged = False
    if time.time() - _last_restart < 600:
        log.warning("10 分钟内刚启动过微信，先等着")
        return True
    _last_restart = time.time()
    log.warning("确认没有微信进程，启动客户端")
    try:
        subprocess.Popen([WECHAT_EXE], creationflags=0x08000000)
    except Exception as e:
        log.error("启动微信失败：%r", e)
        return False
    return True

def ensure_online(fail_streak: int = 0) -> bool:
    """能不能干活：没主窗 → 重启；连续发送失败 → 重启；否则只收拾弹窗和尺寸。

    注意：不要用 SendMessageTimeout(WM_NULL) 判卡死——微信 4.x 主窗经常不回复
    这条消息但界面完全正常（截图能证明），照着它重启会把好的客户端杀掉。
    """
    global _last_restart, _tray_logged
    closed = close_strays()
    if closed:
        log.info("关掉挡路弹窗：%s", "、".join(closed))
    h, _w, _hh = main_window()
    if h:
        # 主窗已经在 = 已登录；此时还挂着的「微信」小窗是多余的登录提示，关掉
        lh, lw, lhh = _login_window()
        if lh:
            global _splash_warned
            if not _splash_warned:
                _splash_warned = True
                log.warning("主窗已登录，但还挂着一个「正在进入」窗（%s×%s）：忽略它，绝不再点登录", lw, lhh)
    if h and ctypes.windll.user32.IsIconic(h):
        # 最小化 = 已登录、人在用别的东西。这里**不要**还原：心跳每 60 秒一次，
        # 一还原用户就看到微信反复自己弹出来。真要发消息时，发送链路
        # （adapters/wxa_adapter.py 的 _bring_to_front）自己会 SW_RESTORE。
        global _minimized_logged
        if not _minimized_logged:
            _minimized_logged = True
            log.info("微信主窗最小化：已登录，保持不动（发消息时会自己还原）")
        return True
    if h:
        globals()["_minimized_logged"] = False
        _tray_logged = False
    if not h:
        lh, lw, lhh = _login_window()
        if lh:
            # 停在登录窗：点一次登录，然后等人在手机上确认——绝不反复杀进程
            since = _login_pending_since()
            if not since:
                _mark_login_pending()
                log.warning("微信在登录窗（%s×%s），点一次「登录」后等手机确认", lw, lhh)
                _try_login_window(timeout=20.0)
                h2, _w2, _hh2 = main_window()
                if h2:
                    _clear_login_pending()
                    ensure_size()
                    log.info("登录成功，微信主窗已就绪")
                    return True
            waited = (time.time() - _login_pending_since()) / 60 if _login_pending_since() else 0
            if waited > 30:
                log.error("登录请求已等 %.0f 分钟仍无人确认，请用手机点「登录」（期间不再重启微信）", waited)
            else:
                log.warning("在等手机确认登录（已 %.1f 分钟），不重启微信", waited)
            return True
        pids = weixin_pids()
        if pids:
            # 进程在、窗口不可见（托盘/隐藏）：微信没死，别碰它。
            # 以前这里直接 _launch_client()，每 2 分钟 Popen 一次，
            # 单实例激活把窗口弹到前台 —— 用户看到「登录页被反复拉起」。
            if not _tray_logged:
                _tray_logged = True
                log.info("微信在托盘/窗口隐藏（pid %s）：主窗不可见，不动它",
                         ",".join(str(p) for p in pids[:3]))
            return True
        log.warning("找不到微信主窗，微信进程也没了（已退出）")
        return _launch_client()
    ensure_size()
    if fail_streak >= 3:
        if time.time() - _last_restart < 600:
            log.warning("连续 %d 次发送失败，但 10 分钟内重启过，先等一等", fail_streak)
            return True
        log.warning("连续 %d 次发送失败，重启微信", fail_streak)
        _last_restart = time.time()
        return restart()
    return True
