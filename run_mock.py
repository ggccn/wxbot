"""不联微信的联调入口：命令行里直接跟桥对话。

用法：
    python run_mock.py              # 进入对话
    python run_mock.py -m "你好"     # 只问一句，打印回复后退出（自动化测试用）
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import config as cfgmod  # noqa: E402
from adapters.mock import MockAdapter  # noqa: E402
from bridge import Bridge  # noqa: E402
from brain import Brain  # noqa: E402
from store import Store  # noqa: E402


def build(cfg, db_path: str, verbose: bool = True):
    store = Store(db_path)
    brain = Brain(cfg.dsh_cmd, cfg.agent_home, cfg.profile, cfg.timeout_seconds)
    adapter = MockAdapter(verbose=verbose)
    bridge = Bridge(cfg, brain, store, adapter.send, self_wxid="wxid_me")
    return store, brain, adapter, bridge


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-c", "--config", default=str(ROOT / "config.toml"))
    ap.add_argument("-m", "--message", default="")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    cfg = cfgmod.load(args.config)
    db = ROOT / "data" / "mock.db"
    db.parent.mkdir(exist_ok=True)
    store, brain, adapter, bridge = build(cfg, str(db))

    if args.message:
        t0 = time.time()
        bridge.submit(adapter.make(args.message))
        bridge._pool.shutdown(wait=True)  # 等所有回复发完
        print(f"[用时 {time.time() - t0:.1f}s]")
        return 0

    print("=" * 50)
    print("模拟对话（/exit 退出）。你发的每句话都会被当成私聊。")
    print("=" * 50)
    while True:
        try:
            text = input("\n你> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not text:
            continue
        if text in ("/exit", "/quit"):
            break
        bridge.submit(adapter.make(text))
        time.sleep(0.3)
    bridge.shutdown()
    print("再见。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
