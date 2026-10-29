"""把消息交给 DSH headless 跑，拿回回复文本。

每次调用是一个独立进程：
    dsh.cmd headless --json --session-id <sid> -
消息正文从 stdin 传（避免 Windows 命令行引号转义问题），
stdout 是 newline-delimited JSON 事件，我们只关心 session 和 final。
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Optional, Tuple

log = logging.getLogger("brain")

CREATE_NO_WINDOW = 0x08000000


def _read_api_key() -> Optional[str]:
    """没设环境变量时，从 DSH 桌面版凭据文件里取。"""
    if os.environ.get("DEEPSEEK_API_KEY"):
        return os.environ["DEEPSEEK_API_KEY"]
    home = Path(os.environ.get("DSH_HOME") or Path.home() / ".dsh")
    cred = home / ".credentials.yaml"
    if not cred.exists():
        return None
    try:
        for line in cred.read_text(encoding="utf-8").splitlines():
            m = re.match(r"\s*DEEPSEEK_API_KEY\s*:\s*(.+?)\s*$", line)
            if m:
                return m.group(1).strip().strip('"').strip("'")
    except OSError:
        return None
    return None


class BrainError(RuntimeError):
    pass


class Brain:
    def __init__(self, dsh_cmd: str, agent_home: str, profile: str = "headless",
                 timeout: int = 120):
        self.dsh_cmd = dsh_cmd
        self.agent_home = Path(agent_home)
        self.profile = profile
        self.timeout = timeout
        self.agent_home.mkdir(parents=True, exist_ok=True)
        self._env = os.environ.copy()
        key = _read_api_key()
        if key:
            self._env["DEEPSEEK_API_KEY"] = key
        else:
            log.warning("没有找到 DEEPSEEK_API_KEY，模型调用会失败")

    def _argv(self, session_id: Optional[str]) -> list[str]:
        target = self.dsh_cmd
        args = [self.profile, "--json"]
        if session_id:
            args += ["--session-id", session_id]
        args.append("-")
        if target.lower().endswith((".cmd", ".bat")):
            return ["cmd", "/c", target] + args
        return [target] + args

    def ask(self, text: str, session_id: Optional[str] = None) -> Tuple[str, Optional[str]]:
        """返回 (回复文本, 会话id)。异常时抛 BrainError。

        模型偶尔会「只调工具、不说人话」，直接结束一轮且没有最终文本。
        这种情况重试一次（明确要求只回一句话），失败才抛错。
        会话 id 失效（被删/写错）时自动开新会话重来，不让一条坏 id 把人卡死。
        """
        try:
            return self._ask_once(text, session_id)
        except BrainError as e:
            msg = str(e)
            if "does not exist" in msg and session_id:
                log.warning("会话 %s 不存在，改用新会话重来", session_id)
                return self._ask_once(text, None)
            if "没有返回内容" not in msg:
                raise
            log.warning("上一轮没有文字输出，重试一次：%s", e)
            retry = text + "\n（上一轮没有输出文字。这次只回一句中文，不要调用任何工具，不要解释。）"
            return self._ask_once(retry, session_id)

    def _ask_once(self, text: str, session_id: Optional[str] = None) -> Tuple[str, Optional[str]]:
        # DSH 同一个会话同时只允许一个写句柄。上一次如果被超时掐掉、进程没死干净，
        # 后面每次调用都会秒回 "is already owned by an active write handle"，
        # 表现就是"她突然不说话了"。所以这里遇到锁冲突先等一会儿再试。
        for attempt in range(3):
            try:
                return self._run(text, session_id)
            except BrainError as e:
                if "already owned" not in str(e) or attempt == 2:
                    raise
                log.warning("会话被占用，2 秒后重试（第 %d 次）", attempt + 1)
                time.sleep(2)

    def _run(self, text: str, session_id: Optional[str] = None) -> Tuple[str, Optional[str]]:
        argv = self._argv(session_id)
        log.info("ask session=%s chars=%d", session_id or "<new>", len(text))
        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(self.agent_home),
                env=self._env,
                creationflags=CREATE_NO_WINDOW,
            )
        except OSError as e:
            raise BrainError(f"启动失败：{e}") from None

        try:
            out, err_out = proc.communicate(input=text.encode("utf-8"), timeout=self.timeout)
        except subprocess.TimeoutExpired:
            # 只 kill 顶层进程会留下一个还攥着会话锁的子进程，下次调用直接报"被占用"，
            # 所以必须连整棵进程树一起收掉。
            try:
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                               capture_output=True, timeout=20,
                               creationflags=CREATE_NO_WINDOW)
            except Exception:
                pass
            try:
                proc.kill()
            except Exception:
                pass
            raise BrainError(f"超时（>{self.timeout}s）") from None

        stdout = out.decode("utf-8", "replace")
        reply, sid, err = "", session_id, None
        for raw in stdout.splitlines():
            raw = raw.strip()
            if not raw.startswith("{"):
                continue
            try:
                evt = json.loads(raw)
            except json.JSONDecodeError:
                continue
            kind = evt.get("type")
            if kind == "session":
                sid = evt.get("sessionId") or sid
            elif kind == "final":
                reply = evt.get("text") or ""
            elif kind == "turn_end":
                reason = evt.get("reason") or {}
                if reason.get("kind") in ("error", "aborted", "max-steps"):
                    e = reason.get("error") or {}
                    err = e.get("message") or reason.get("kind")
            elif kind == "error":
                err = evt.get("message") or err

        if err and not reply:
            stderr_tail = err_out.decode("utf-8", "replace").strip().splitlines()[-3:]
            raise BrainError(err + (" | " + " / ".join(stderr_tail) if stderr_tail else ""))
        if not reply.strip():
            raise BrainError("模型没有返回内容")
        return reply.strip(), sid
