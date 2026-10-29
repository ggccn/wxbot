"""让另一个「干净的脑袋」核对事实。

用法：
    .venv\\Scripts\\python.exe ask_ai.py "蜜雪冰城柠檬水多少钱"

设计：每次开一个**全新会话**，只问事实、要求"不确定就说不确定"，
不复用任何聊天上下文——同一个模型换个空上下文重答，能筛掉相当一部分
"顺着上文硬编"的错误。
"""
from __future__ import annotations

import sys
from pathlib import Path
from paths import HOME_DIR
import os

# 被 DSH 的 shell 工具调用时，stdout 是管道；强制 UTF-8，
# 否则 Windows 默认 GBK 会把中文吐成乱码。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

DSH_CMD = os.environ.get("DSH_CMD", "dsh")
AGENT_HOME = str(HOME_DIR)

SYSTEM = (
    "你是事实核对员。只回答客观事实，一两句话，不要寒暄、不要解释推理过程。\n"
    "规则：\n"
    "1. 不确定，或者这个问题本身没有统一答案（例如『看地区』『看门店』），"
    "就回答「不确定」，并补一句你需要知道什么才能答（比如『哪个城市』）。\n"
    "2. 新闻里的数字必须说明是哪一地、哪一年，别当成通用值。\n"
    "3. 不要编造具体数字、日期、人名。\n"
)


def ask(question: str, timeout: int = 180) -> str:
    from brain import Brain

    brain = Brain(DSH_CMD, AGENT_HOME, profile="headless", timeout=timeout)
    reply, _ = brain.ask(SYSTEM + "\n问题：" + question, session_id=None)
    return (reply or "").strip()


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print("用法: ask_ai.py 问题")
        return 2
    q = " ".join(args)
    try:
        ans = ask(q)
    except Exception as e:
        print("核对失败：%r" % (e,))
        return 1
    print(ans)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
