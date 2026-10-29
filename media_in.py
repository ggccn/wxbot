"""看图和听音：把微信里收到的图片/表情变成一段文字，交给大脑。

为什么不做成「把图给多模态模型」：DSH headless 没有图片输入通道（`dsh headless` 只吃文本），
所以这里用本机能力先把图「读」成文字：
- OCR（Windows.Media.Ocr，中文可用）——表情包、截图、聊天记录图都能出字；
- 人脸/肤色检测（OpenCV）——判断"图里有没有人"；
- 动图/尺寸/主色——判断是不是表情包、是不是长截图。
大脑拿到这段描述再说人话，效果比瞎猜强得多。
"""
from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

log = logging.getLogger("media")

MEDIA_DIR = Path(__file__).resolve().parent / "media"


# ---------------- 从 CDN 直接下 ----------------
def _unescape(url: str) -> str:
    import html

    return html.unescape(url).replace("\\/", "/")


def _ext_of(data: bytes, default: str = ".png") -> str:
    if data[:4] == b"\x89PNG":
        return ".png"
    if data[:3] in (b"GIF",):
        return ".gif"
    if data[:2] == b"\xff\xd8":
        return ".jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return default


def _decrypt_image(data: bytes, aeskey_hex: str) -> bytes:
    """微信图片：AES-128-ECB 解密（key 就是 XML 里的 aeskey），尾部补 0。"""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    key = bytes.fromhex(aeskey_hex)
    dec = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    out = dec.update(data) + dec.finalize()
    return out.rstrip(b"\x00")


def grab_from_msg(content: str, save_dir: Path, tag: str = "cdn") -> str | None:
    """直接从消息 XML 里的 CDN 地址下原图/表情——纯后台，不碰桌面。

    表情（<emoji>）：cdnurl 就是原文件（png/gif），不需要密钥。
    图片（<img>）：cdnbigimgurl/cdnmidimgurl + aeskey，AES-128-ECB 解密。
    """
    if not content:
        return None
    import re
    import urllib.request

    text = content

    # 表情
    m = re.search(r'cdnurl\s*=\s*"([^"]+)"', text)
    if "<emoji" in text and m:
        url = _unescape(m.group(1))
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            data = urllib.request.urlopen(req, timeout=25).read()
        except Exception as e:
            log.info("CDN 下表情失败：%r", e)
            return None
        if len(data) < 200:
            return None
        save_dir.mkdir(parents=True, exist_ok=True)
        dst = save_dir / f"{tag}-{abs(hash(url)) % 10**10}{_ext_of(data)}"
        dst.write_bytes(data)
        log.info("CDN 拿到表情：%s（%d 字节）", dst.name, len(data))
        return str(dst)

    # 图片
    url = ""
    for key in ("cdnbigimgurl", "cdnmidimgurl", "cdnthumburl"):
        mm = re.search(key + r'\s*=\s*"([^"]+)"', text)
        if mm:
            cand = _unescape(mm.group(1))
            # 微信 4.x 的 <img> 里这三项常是 fileid（一长串 hex），不是 URL
            if cand.startswith(("http://", "https://")):
                url = cand
                if key != "cdnthumburl":
                    break
    if not url or "<img" not in text:
        return None
    ak = re.search(r'aeskey\s*=\s*"([0-9a-fA-F]{32})"', text)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        data = urllib.request.urlopen(req, timeout=30).read()
    except Exception as e:
        log.info("CDN 下图失败：%r", e)
        return None
    if ak:
        try:
            data = _decrypt_image(data, ak.group(1))
        except Exception as e:
            log.info("图片解密失败：%r", e)
            return None
    if len(data) < 200:
        return None
    save_dir.mkdir(parents=True, exist_ok=True)
    dst = save_dir / f"{tag}-{abs(hash(url)) % 10**10}{_ext_of(data, '.jpg')}"
    dst.write_bytes(data)
    log.info("CDN 拿到图片：%s（%d 字节）", dst.name, len(data))
    return str(dst)


# ---------------- 下载 ----------------
def grab(
    db,
    user: str,
    local_id: int,
    subdir: str = "in",
    prefer_original: bool = False,
    chat_name: str | None = None,
) -> str | None:
    """把一条消息的媒体文件落到本地，返回路径；下不了返回 None。

    prefer_original：图片的话先要本机最高档（`_h.dat` > `.dat`，绝不给缩略图），
    实在只有压缩版就去界面点一下「查看原图」把原件拉下来——手机发的高清图在本机
    通常只缓存了压缩版，不点这一下 AI 看到的永远是糊的。
    """
    import os

    from wechatauto.media import MediaDownloader

    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    save_dir = MEDIA_DIR / subdir
    save_dir.mkdir(parents=True, exist_ok=True)
    try:
        dl = MediaDownloader(db, save_dir=str(save_dir))
        # 图片密钥只在微信看过大图后驻留内存，先试一次自动探测，能拿到就顺带用它
        try:
            if getattr(dl, "image_key", None) in (None, "", b""):
                dl.detect_image_key(refresh=True)
        except Exception:
            pass

        path = None
        # 图片：先要本机最高档，别把 _t.dat 缩略图当成果交出去
        for tier in ("full", "best"):
            try:
                path = dl.download_image(user, int(local_id), str(save_dir), tier=tier) or path
            except Exception:
                pass
        if not path:
            path = dl.download_media(user, int(local_id), save_dir=str(save_dir))

        if path and prefer_original:
            try:
                orig = dl.download_image_original(
                    user,
                    int(local_id),
                    str(save_dir),
                    chat_name=chat_name or user,
                    timeout=25.0,
                )
                if orig and os.path.getsize(orig) > os.path.getsize(path) * 1.15:
                    path = orig
            except Exception as e:
                log.info("拉原图失败（继续用本机那份）：%r", e)

        if path:
            return to_openable(str(path))
    except Exception as e:
        log.info("媒体下载失败（多半是图片密钥没就位）：%r", e)
    return None


def to_openable(path: str) -> str:
    """把微信自己的图片格式（.wxgf，里面是 HEVC 单帧）转成能打开的 png。

    `read_image` / PIL / OCR 都不认 wxgf，直接给过去只会得到"打不开"。
    做法：找到 HEVC 的 VPS 起始码，把后面的裸流丢给 ffmpeg 解一帧。
    """
    import os
    import subprocess

    if not path or not os.path.exists(path):
        return path
    try:
        from PIL import Image

        Image.open(path).verify()
        return path
    except Exception:
        pass

    if not path.lower().endswith((".wxgf", ".hevc", ".h265")):
        # 不认识的扩展名也试一把，免得微信换名字
        if path.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")):
            return path

    try:
        data = open(path, "rb").read()
        i = data.find(b"\x00\x00\x00\x01\x40\x01")   # HEVC VPS
        if i < 0:
            i = data.find(b"\x00\x00\x00\x01")
        if i < 0:
            log.info("wxgf 里没找到 HEVC 起始码：%s", path)
            return path
        tmp = os.path.splitext(path)[0] + ".h265"
        with open(tmp, "wb") as f:
            f.write(data[i:])
        out = os.path.splitext(path)[0] + ".png"
        import imageio_ffmpeg

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        subprocess.run(
            [exe, "-y", "-f", "hevc", "-i", tmp, "-frames:v", "1", "-update", "1", out],
            capture_output=True,
            timeout=60,
        )
        try:
            os.remove(tmp)
        except OSError:
            pass
        if os.path.exists(out) and os.path.getsize(out) > 1024:
            log.info("wxgf 转 png 成功：%s", os.path.basename(out))
            return out
    except Exception as e:
        log.info("wxgf 转换失败：%r", e)
    return path


# ---------------- OCR ----------------
def _clean(text: str) -> str:
    """Windows OCR 会在汉字之间塞空格，粘回去。"""
    text = text.replace("\r", "\n")
    # 汉字之间的单个空格去掉
    text = re.sub(r"(?<=[\u4e00-\u9fa5])\s+(?=[\u4e00-\u9fa5])", "", text)
    # 标点前后的空格也去掉（OCR 常留 " 出去走走 ？"）
    text = re.sub(r"\s+(?=[，。？！、：；）」』])", "", text)
    text = re.sub(r"(?<=[（「『])\s+", "", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


async def _ocr_async(path: str) -> str:
    from winsdk.windows.globalization import Language
    from winsdk.windows.graphics.imaging import BitmapDecoder
    from winsdk.windows.media.ocr import OcrEngine
    from winsdk.windows.storage import FileAccessMode, StorageFile

    f = await StorageFile.get_file_from_path_async(path)
    stream = await f.open_async(FileAccessMode.READ)
    dec = await BitmapDecoder.create_async(stream)
    bmp = await dec.get_software_bitmap_async()
    engine = None
    for tag in ("zh-Hans-CN", "zh-CN", "en-US"):
        lang = Language(tag)
        if OcrEngine.is_language_supported(lang):
            engine = OcrEngine.try_create_from_language(lang)
            if engine is not None:
                break
    if engine is None:
        return ""
    res = await engine.recognize_async(bmp)
    return _clean(res.text or "")


def ocr(path: str) -> str:
    try:
        return asyncio.run(_ocr_async(path))
    except Exception as e:
        log.debug("OCR 失败：%r", e)
        return ""


async def _ocr_words_async(path: str) -> list[tuple[str, int, int, int, int]]:
    from winsdk.windows.globalization import Language
    from winsdk.windows.graphics.imaging import BitmapDecoder
    from winsdk.windows.media.ocr import OcrEngine
    from winsdk.windows.storage import FileAccessMode, StorageFile

    f = await StorageFile.get_file_from_path_async(path)
    stream = await f.open_async(FileAccessMode.READ)
    dec = await BitmapDecoder.create_async(stream)
    bmp = await dec.get_software_bitmap_async()
    engine = None
    for tag in ("zh-Hans-CN", "zh-CN", "en-US"):
        lang = Language(tag)
        if OcrEngine.is_language_supported(lang):
            engine = OcrEngine.try_create_from_language(lang)
            if engine is not None:
                break
    if engine is None:
        return []
    res = await engine.recognize_async(bmp)
    out = []
    for line in res.lines:
        for w in line.words:
            r = w.bounding_rect
            out.append((w.text, int(r.x), int(r.y), int(r.width), int(r.height)))
    return out


def ocr_words(path: str) -> list[tuple[str, int, int, int, int]]:
    """带坐标的 OCR：返回 [(文字, x, y, w, h), ...]（图片像素坐标）。"""
    try:
        return asyncio.run(_ocr_words_async(path))
    except Exception as e:
        log.debug("OCR(words) 失败：%r", e)
        return []


# ---------------- 图像分析 ----------------
def _faces(path: str) -> int:
    try:
        import cv2
        import numpy as np

        # cv2.imread 不支持中文路径（微信的图经常在中文目录里），走 imdecode
        buf = np.frombuffer(Path(path).read_bytes(), dtype=np.uint8)
        img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
        if img is None:
            return 0
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        faces = cascade.detectMultiScale(gray, 1.1, 4)
        return len(faces)
    except Exception as e:
        log.debug("人脸检测失败：%r", e)
        return 0


def snap_chat_bottom(
    ui, subdir: str = "in", height: int = 260, hwnd: int = 0, left_only: bool = False
) -> str | None:
    """兜底看图：微信 4.x 的图片/表情常常下不下来（图片密钥要"看过大图"才驻留内存），
    但气泡就明明白白画在屏幕上——直接截会话底部那条消息，交给视觉工具看。

    优先按 UIA 的「消息」列表定位；UIA 拿不到就退回主窗口聊天区的底部。
    left_only=True 只截左半边（对方的气泡在左，自己发的绿气泡在右）。
    返回 PNG 路径；截不到返回 None。
    """
    try:
        from PIL import ImageGrab
    except Exception:
        return None

    import time as _t

    box = None
    ctrl = None
    for name in ("消息",):
        try:
            ctrl = ui.find(name, timeout=1.2) if ui is not None else None
        except Exception:
            ctrl = None
        if ctrl is not None:
            break
    if ctrl is not None:
        try:
            r = ctrl.BoundingRectangle
            x0, y0, x1, y1 = int(r.left), int(r.top), int(r.right), int(r.bottom)
            if y1 - y0 >= 80 and x1 - x0 >= 200:
                box = (x0, max(y0, y1 - height), x1, y1)
        except Exception:
            box = None

    if box is None and hwnd:
        try:
            import ctypes
            import ctypes.wintypes as wt

            r = wt.RECT()
            ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r))
            w = r.right - r.left
            h = r.bottom - r.top
            if w > 300 and h > 300:
                # 聊天区在会话列表右边；底下约 1/5 是输入框，要避开。
                # 高度给足，保证「最新那条」整条都在画面里。
                x0 = int(r.left + w * 0.34)
                x1 = int(r.right - w * 0.02)
                if left_only:
                    x1 = x0 + int((x1 - x0) * 0.52)
                y1 = int(r.bottom - h * 0.20)
                y0 = max(r.top, y1 - max(height, 300))
                if x1 - x0 > 200 and y1 - y0 > 80:
                    box = (x0, y0, x1, y1)
        except Exception:
            box = None

    if box is None:
        return None

    try:
        MEDIA_DIR.mkdir(parents=True, exist_ok=True)
        out_dir = MEDIA_DIR / subdir
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"snap-{int(_t.time() * 1000)}.png"
        ImageGrab.grab(bbox=box, all_screens=True).save(path)
        # 再往下裁一刀：只留「对方那条气泡」，别把自己的绿色气泡和别人的话一起交给视觉模型，
        # 否则它会以为对方发来的是聊天记录截图。
        tight = crop_incoming(path)
        return str(tight or path)
    except Exception as e:
        log.info("截图兜底失败：%r", e)
        return None


def crop_incoming(path: str) -> str | None:
    """从聊天截屏里裁出「对方那条」气泡（左半边、不是绿色、不是纯背景）。

    做法：抠掉背景色和微信绿，取最大连通块；只接受落在左半边的块。
    失败返回 None（调用方退回整张截屏）。
    """
    try:
        import numpy as np
        from PIL import Image
    except Exception:
        return None
    try:
        import cv2
    except Exception:
        return None

    try:
        with Image.open(path) as im:
            img = im.convert("RGB")
        arr = np.array(img)
    except Exception as e:
        log.debug("裁图打不开：%r", e)
        return None

    h, w = arr.shape[:2]
    if h < 40 or w < 80:
        return None
    # 背景色取四个角的众数
    corners = np.concatenate([
        arr[0:4, 0:4].reshape(-1, 3), arr[0:4, -4:].reshape(-1, 3),
        arr[-4:, 0:4].reshape(-1, 3), arr[-4:, -4:].reshape(-1, 3),
    ])
    bg = np.median(corners, axis=0)

    diff = np.abs(arr.astype(np.int16) - bg.astype(np.int16)).sum(axis=2)
    # 阈值要高一点：聊天气泡外还有一层很淡的渐变，阈值 26 会把整屏连成一块
    mask = (diff > 60).astype(np.uint8)
    # 去掉微信绿（自己的气泡）
    b, g, r = arr[:, :, 0].astype(np.int16), arr[:, :, 1].astype(np.int16), arr[:, :, 2].astype(np.int16)
    green = (g > 120) & (g - r > 40) & (g - b > 40)
    mask[green] = 0
    # 只保留左半边（对方在自己左边）
    mask[:, int(w * 0.62):] = 0
    # 形态学闭运算，把小字的笔画连成块（别膨胀太多，否则整屏连成一块）
    kernel = np.ones((3, 3), np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=2)

    n, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    best = None
    for i in range(1, n):
        x, y, bw, bh, area = stats[i]
        if bw < 40 or bh < 40 or area < 2000:
            continue
        if bw > w * 0.8 or bh > h * 0.85:   # 整屏连成一块的不要
            continue
        if bh > h * 0.8 and bw < 30:        # 滚动条之类
            continue
        if best is None or (y + bh) > (best[1] + best[3]):
            best = (x, y, bw, bh, area)     # 取最靠下的一块：最新那条消息在下面
    if best is None:
        return None
    x, y, bw, bh, _ = best
    pad = 8
    x0, y0 = max(0, x - pad), max(0, y - pad)
    x1, y1 = min(w, x + bw + pad), min(h, y + bh + pad)
    if x1 - x0 < 40 or y1 - y0 < 40:
        return None
    out = Path(path).with_name(Path(path).stem + "-bubble.png")
    Image.fromarray(arr[y0:y1, x0:x1]).save(out)
    return str(out)


def describe(path: str, max_text: int = 160) -> str:
    """把一张图说成一句中文描述（给大脑看的那种）。"""
    p = Path(path)
    if not p.exists():
        return "[图片，但文件没拿到]"
    parts: list[str] = []
    w = h = 0
    animated = False
    try:
        from PIL import Image

        with Image.open(p) as im:
            w, h = im.size
            animated = getattr(im, "n_frames", 1) > 1
    except Exception as e:
        log.debug("打不开图：%r", e)

    if animated:
        parts.append("动图/表情包")
    if w and h:
        ratio = h / max(w, 1)
        if ratio > 2.2:
            parts.append(f"长截图（{w}×{h}）")
        elif max(w, h) <= 320:
            parts.append(f"小图（{w}×{h}），像表情")
        else:
            parts.append(f"{w}×{h}")
    n = _faces(path)
    if n:
        parts.append(f"有 {n} 张人脸")

    text = ocr(path)
    if text:
        text = re.sub(r"\s*\n\s*", " / ", text)[:max_text]
        parts.append(f"图上的字：「{text}」")
    elif not animated:
        parts.append("没识别到文字")

    return "[图片：" + "，".join(parts) + "]"
