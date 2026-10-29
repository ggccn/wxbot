"""表情包库：本地存图 + 按标签挑 + 去网上找新的。

目录：`E:\\dsk\\wxbot\\stickers\\<标签>\\xxx.png|gif|jpg`，索引在 `stickers\\index.json`。

发送走 `gui.send_image(path, who)`（剪贴板粘贴，微信当图片消息发出去）。
真正作为「微信表情」发出去要往表情面板里塞，那套没有稳定接口；发成图片是最接近的做法。
"""
from __future__ import annotations

import hashlib
import html
import json
import logging
import random
import re
import urllib.parse
import urllib.request
from pathlib import Path

log = logging.getLogger("stickers")

ROOT = Path(__file__).resolve().parent
STICKER_DIR = ROOT / "stickers"
INDEX = STICKER_DIR / "index.json"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"


# ---------------- 索引 ----------------
def _load() -> dict:
    try:
        if INDEX.exists():
            return json.loads(INDEX.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("表情索引坏了：%s", e)
    return {}


def _save(idx: dict) -> None:
    STICKER_DIR.mkdir(parents=True, exist_ok=True)
    tmp = INDEX.with_suffix(".tmp")
    tmp.write_text(json.dumps(idx, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(INDEX)


def _sync(idx: dict) -> dict:
    """以磁盘为准：目录里有什么就是什么。

    索引里残留的旧条目（比如图被删了、标签目录没了）一律丢掉，否则 pick() 会
    选中一张不存在的图，发送直接失败。
    """
    STICKER_DIR.mkdir(parents=True, exist_ok=True)
    fresh: dict[str, list[str]] = {}
    for d in STICKER_DIR.iterdir():
        if not d.is_dir():
            continue
        files = sorted(f.name for f in d.iterdir() if f.suffix.lower() in (".png", ".gif", ".jpg", ".jpeg", ".webp"))
        if files:
            fresh[d.name] = files
    if fresh != {k: v for k, v in idx.items() if v}:
        try:
            _save(fresh) if "_save" in globals() else None
        except Exception:
            pass
    return fresh


def tags() -> dict:
    return _sync(_load())


def pick(tag: str) -> Path | None:
    """按标签随机挑一张；标签不存在返回 None。"""
    idx = tags()
    if tag not in idx:
        # 模糊匹配：标签名包含关键词也算
        cands = [k for k in idx if tag and (tag in k or k in tag)]
        if not cands:
            return None
        tag = cands[0]
    name = random.choice(idx[tag])
    p = STICKER_DIR / tag / name
    return p if p.exists() else None


def add_file(src: str | Path, tag: str, name: str | None = None) -> Path:
    src = Path(src)
    tag = re.sub(r"[^\w\u4e00-\u9fa5-]", "_", tag) or "其他"
    d = STICKER_DIR / tag
    d.mkdir(parents=True, exist_ok=True)
    data = src.read_bytes()
    digest = hashlib.sha1(data).hexdigest()[:10]
    if name is None:
        # 内容哈希做文件名：同一张图不会重复入库（收到的表情会被自动收藏）
        existing = list(d.glob(f"h{digest}.*"))
        if existing:
            return existing[0]
        name = f"h{digest}{src.suffix or '.png'}"
    dst = d / name
    if not dst.exists():
        dst.write_bytes(data)
    idx = _load()
    idx.setdefault(tag, [])
    if dst.name not in idx[tag]:
        idx[tag].append(dst.name)
    _save(idx)
    return dst


# ---------------- 网上找 ----------------


def _download(url: str, dst: Path) -> Path | None:
    """把一个图片网址抓下来存到 dst（webp 自动转 png）。失败返回 None。"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "image/*,*/*"})
        with urllib.request.urlopen(req, timeout=25) as r:
            data = r.read()
        if not data or len(data) < 2000:
            log.warning("下载太短，丢掉：%s", url)
            return None
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            import io

            from PIL import Image

            img = Image.open(io.BytesIO(data)).convert("RGB")
            dst = dst.with_suffix(".png")
            img.save(dst)
            return dst
        dst.write_bytes(data)
        return dst
    except Exception as e:
        log.warning("下载失败 %s：%r", url, e)
        return None
