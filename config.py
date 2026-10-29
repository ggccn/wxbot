"""读 config.toml，转成一个扁平好用的对象。"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Config:
    source: Path
    # identity
    bot_name: str = "小助手"
    # reply
    private: str = "auto"
    private_prefix: str = ""
    group: str = "at"
    group_keywords: list = field(default_factory=list)
    max_chars: int = 800
    ack_after_seconds: int = 12
    ack_text: str = "（收到，我想一下……）"
    group_at_sender: bool = True
    # access
    allow_groups: list = field(default_factory=lambda: ["*"])
    deny_contacts: list = field(default_factory=list)
    deny_groups: list = field(default_factory=list)
    admin_wxids: list = field(default_factory=list)
    # rate limit
    per_contact_per_minute: int = 6
    per_contact_per_hour: int = 60
    global_per_minute: int = 30
    # quiet hours
    quiet_hours: dict = field(default_factory=dict)
    # safety (防封)
    safety: dict = field(default_factory=dict)
    # media（看图方式）
    media: dict = field(default_factory=dict)
    backfill: dict = field(default_factory=dict)
    # pay（转账/红包助手）
    pay: dict = field(default_factory=dict)
    # 运行时节奏档（/节奏 命令会改它，并存到 data/settings.json）
    rhythm_profile: str = "natural"
    # brain
    dsh_cmd: str = ""
    profile: str = "headless"
    agent_home: str = ""
    timeout_seconds: int = 120
    max_workers: int = 3


def load(path: str) -> Config:
    p = Path(path)
    raw = tomllib.loads(p.read_text(encoding="utf-8"))
    c = Config(source=p)
    ident = raw.get("identity", {})
    reply = raw.get("reply", {})
    access = raw.get("access", {})
    rl = raw.get("rate_limit", {})
    brain = raw.get("brain", {})

    c.bot_name = ident.get("bot_name", c.bot_name)
    for k in ("private", "private_prefix", "group", "group_keywords", "max_chars",
              "ack_after_seconds", "ack_text", "group_at_sender"):
        if k in reply:
            setattr(c, k, reply[k])
    for k in ("allow_groups", "deny_contacts", "deny_groups", "admin_wxids"):
        if k in access:
            setattr(c, k, access[k])
    for k in ("per_contact_per_minute", "per_contact_per_hour", "global_per_minute"):
        if k in rl:
            setattr(c, k, rl[k])
    c.quiet_hours = raw.get("quiet_hours", {})
    c.safety = raw.get("safety", {})
    c.media = raw.get("media", {})
    c.backfill = raw.get("backfill", {})
    c.pay = raw.get("pay", {})
    c.rhythm_profile = raw.get("runtime", {}).get("rhythm_profile", c.rhythm_profile)
    for k in ("dsh_cmd", "profile", "agent_home", "timeout_seconds", "max_workers"):
        if k in brain:
            setattr(c, k, brain[k])
    return c
