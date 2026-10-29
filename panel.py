"""本地小面板：改「连发 / 自动表情 / 定时主动 / 性格人设 / 节奏」。

跑法（双击也行）：
    E:\\dsk\\wxbot\\panel.cmd
然后浏览器打开 http://127.0.0.1:8765

面板只写这几个文件，桥按 mtime 热加载，改完一两分钟内生效：
    data\\behavior.json    连发 + 自动表情
    data\\proactive.json   定时主动（开关、每日上限、间隔、时间窗、条目）
    data\\settings.json    节奏档 natural/calm/fast
    E:\\wxbot_home\\PERSONA.md   性格（人设正文）
    stickers\\              表情库（抓新图会落盘到这里）
"""
from __future__ import annotations

import html
import json
import re
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote
from paths import NOTES_DIR, DATA_DIR, PERSONA_FILE, MEMES_FILE

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DATA_DIR = DATA
NOTES_DIR = NOTES_DIR
NOTES_DIR.mkdir(parents=True, exist_ok=True)
BEHAVIOR = DATA / "behavior.json"
PROACTIVE = DATA / "proactive.json"
SETTINGS = DATA / "settings.json"
PERSONA = PERSONA_FILE
MEMES = MEMES_FILE

# 前端小脚本：① 回到页面时恢复滚动位置（否则每次删图都跳到头顶）
#            ② 表情库的删/移/删整档/补货走 fetch，就地更新，不整页刷新
_JS = """
<script>
(function(){
  var KEY='panel-scroll';
  if(!location.hash){
    var y=sessionStorage.getItem(KEY);
    if(y!==null){ requestAnimationFrame(function(){ window.scrollTo(0, +y); }); }
  }
  window.addEventListener('submit', function(){ sessionStorage.setItem(KEY, String(window.scrollY)); }, true);
  var ACT={'/sticker/del':1,'/sticker/move':1,'/sticker/droptag':1};
  function toast(msg,bad){
    var d=document.createElement('div'); d.className='toast'+(bad?' tbad':''); d.textContent=msg;
    document.body.appendChild(d); setTimeout(function(){ d.remove(); }, 2600);
  }
  function total(delta){
    var h=document.getElementById('libcount'); if(!h) return;
    h.textContent=String(Math.max(0,(parseInt(h.textContent,10)||0)+delta));
  }
  document.addEventListener('submit', function(ev){
    var f=ev.target; if(!f||!f.getAttribute) return;
    var act; try { act=new URL(f.action, location.href).pathname; } catch(e){ return; }
    if(!ACT[act]) return;
    ev.preventDefault();
    var btn=f.querySelector('button'); if(btn) btn.disabled=true;
    var body=new URLSearchParams(new FormData(f));
    fetch(act,{method:'POST',body:body,headers:{'Content-Type':'application/x-www-form-urlencoded'}})
      .then(function(r){ return r.text(); })
      .then(function(t){
        var m=t.match(/<div class='(ok|bad)'>([^<]*)<\\/div>/);
        toast(m?m[2]:'完成', m && m[1]==='bad');
        if(act==='/sticker/droptag'){
          var blk=f.closest('.tagblk');
          if(blk){ var n=blk.querySelectorAll('.st').length; blk.remove(); total(-n); }
          return;
        }
        var cell=f.closest('.st'), box=f.closest('.tagblk');
        if(cell) cell.remove();
        if(box){
          var c=box.querySelectorAll('.st').length, s=box.querySelector('.taghead .muted');
          if(s) s.textContent=c+' 张';
          if(!c) box.remove();
        }
        if(act==='/sticker/del') total(-1);
      })
      .catch(function(e){ toast('操作失败：'+e, true); })
      .then(function(){ if(btn) btn.disabled=false; });
  }, true);
})();
</script>
<style>.toast{position:fixed;left:50%;bottom:28px;transform:translateX(-50%);background:#1f3a22;border:1px solid #2f7d32;color:#dfe7ef;padding:10px 16px;border-radius:10px;font-size:14px;z-index:99;box-shadow:0 6px 24px rgba(0,0,0,.45)}.toast.tbad{background:#3a1f1f;border-color:#8a3a3a}</style>
"""

BEHAVIOR_DEFAULTS = {
    "backfill_reply_minutes": 240,
    "bubbles_enabled": True,
    "bubble_chance": 0.45,
    "bubble_max": 3,
    "bubble_gap_min": 0.8,
    "bubble_gap_max": 2.4,
    "auto_sticker_chance": 0.15,
    "auto_sticker_tags": ["可爱", "开心", "害羞", "收到"],
    "group_mode": "smart",
    "group_keywords": ["小鱼", "鲸鱼", "小助手"],
    "group_at_sender": False,
    "group_chime_chance": 0.05,
    "group_chime_cooldown_minutes": 12,
    "group_thread_minutes": 3,
    # 下面这些以前只存在 config.toml，面板改不了；现在都走 behavior.json 热加载
    "private_mode": "",
    "private_prefix": "小鱼",
    "quiet_enabled": False,
    "quiet_start": "23:30",
    "quiet_end": "08:00",
    "rate_per_min": 20,
    "rate_per_hour": 240,
    "rate_global_min": 90,
    "merge_window_seconds": 1.2,
    "memes_enabled": False,
    "curiosity_enabled": True,
    "curiosity_chance": 0.3,
    "curiosity_cooldown_turns": 4,
    "curiosity_follow_topic": True,
    "curiosity_lull_only": True,
    "curiosity_lull_seconds": 90,
    "curiosity_short_chars": 6,
    "curiosity_no_switch": True,
    "curiosity_ping_enabled": True,
    "curiosity_ping_chance": 0.3,
    "curiosity_ping_after_minutes": 1,
    "curiosity_ping_cooldown_minutes": 60,
}
PROACTIVE_DEFAULTS = {
    "enabled": False,
    "max_per_day": 3,
    "min_interval_minutes": 180,
    "window": "09:00-22:00",
    "items": [],
}
RHYTHMS = ("natural", "calm", "fast")

# 每张卡只管自己这些字段，物理上不可能互相覆盖
CARD_KEYS = {
    "more": {
        "private_mode", "private_prefix", "quiet_enabled", "quiet_start", "quiet_end",
        "rate_per_min", "rate_per_hour", "rate_global_min", "merge_window_seconds",
        "curiosity_enabled", "curiosity_chance", "curiosity_cooldown_turns",
        "curiosity_follow_topic", "curiosity_lull_only", "curiosity_lull_seconds",
        "curiosity_short_chars", "curiosity_no_switch",
        "curiosity_ping_enabled", "curiosity_ping_chance",
        "curiosity_ping_after_minutes", "curiosity_ping_cooldown_minutes",
        "memes_enabled",
    },
    "main": {
        "bubbles_enabled", "bubble_chance", "bubble_max",
        "bubble_gap_min", "bubble_gap_max",
        "auto_sticker_chance", "auto_sticker_tags",
        "group_mode", "group_keywords", "group_at_sender",
        "group_chime_chance", "group_chime_cooldown_minutes", "group_thread_minutes",
        "rhythm",
    },
}
RHYTHM_CN = {"natural": "正常（1~3 秒）", "calm": "慢（4~8 秒）", "fast": "快（0.6~1.5 秒）"}


def _read(path: Path, defaults: dict) -> dict:
    data = dict(defaults)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            for k in defaults:
                if k in raw:
                    data[k] = raw[k]
    except Exception:
        pass
    return data


def _write_behavior(conf: dict) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    cur = _read(BEHAVIOR, BEHAVIOR_DEFAULTS)
    cur.update({k: conf[k] for k in BEHAVIOR_DEFAULTS if k in conf})
    BEHAVIOR.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_proactive(conf: dict) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    cur = {}
    try:
        cur = json.loads(PROACTIVE.read_text(encoding="utf-8"))
    except Exception:
        cur = {}
    for k in PROACTIVE_DEFAULTS:
        if k in conf:
            cur[k] = conf[k]
    PROACTIVE.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")


LAST = DATA / "panel_last.json"


def _profile_now() -> str:
    try:
        return json.loads(SETTINGS.read_text(encoding="utf-8")).get("rhythm_profile", "natural")
    except Exception:
        return "natural"


def _note_save(card: str, changed: dict) -> None:
    try:
        DATA.mkdir(parents=True, exist_ok=True)
        LAST.write_text(json.dumps({"at": time.strftime("%Y-%m-%d %H:%M:%S"),
                                    "card": card,
                                    "changed": changed}, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    except Exception:
        pass


def _fmt_changed(changed: dict) -> str:
    if not changed:
        return "保存成功（和原来一样，没有变化）"
    return "已生效：" + "、".join(list(changed.keys())[:8])


def _write_settings(profile: str) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    cur = {}
    try:
        cur = json.loads(SETTINGS.read_text(encoding="utf-8"))
    except Exception:
        cur = {}
    cur["rhythm_profile"] = profile
    SETTINGS.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")


def _norm_note_text(raw: str) -> str:
    r"""把编辑器交上来的文本洗干净：统一换行、去掉行尾空白、折叠连续空行。

    为什么要有这个：从浏览器 textarea 保存回来的文本，换行会一轮一轮翻倍
    （空行堆叠），几百字的文本存几次就几千行，面板里几乎没法改。
    """
    s = (raw or "").replace("\r\n", "\n").replace("\r", "\n")
    out = []
    blank = 0
    for line in s.split("\n"):
        line = line.rstrip()
        if not line:
            blank += 1
            if blank > 1:
                continue
        else:
            blank = 0
        out.append(line)
    return "\n".join(out).strip() + "\n"


def _parse_multipart(raw: bytes, ctype: str) -> tuple[dict, dict]:
    """极简 multipart/form-data 解析：返回 (普通字段, {文件字段: (文件名, 字节)})。"""
    m = re.search(r'boundary="?([^";]+)"?', ctype)
    if not m:
        return {}, {}
    boundary = b"--" + m.group(1).encode()
    fields: dict[str, str] = {}
    files: dict[str, tuple[str, bytes]] = {}
    for part in raw.split(boundary):
        part = part.strip(b"\r\n")
        if not part or part == b"--" or b"\r\n\r\n" not in part:
            continue
        head, body = part.split(b"\r\n\r\n", 1)
        if body.endswith(b"\r\n"):
            body = body[:-2]
        hdrs = head.decode("utf-8", "replace")
        nm = re.search(r'name="([^"]*)"', hdrs)
        if not nm:
            continue
        fn = re.search(r'filename="([^"]*)"', hdrs)
        if fn and fn.group(1):
            files[nm.group(1)] = (fn.group(1), body)
        else:
            fields[nm.group(1)] = body.decode("utf-8", "replace")
    return fields, files


def _save_upload(name: str, data: bytes) -> Path | None:
    """把上传的字节落到临时文件；webp 转 png（微信粘贴发图只认常见格式）。"""
    if not data or len(data) > 12 * 1024 * 1024:
        return None
    suffix = Path(name).suffix.lower() or ".png"
    tmp = DATA / "upload_tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    dst = tmp / ("up" + suffix)
    dst.write_bytes(data)
    try:
        from PIL import Image
    except Exception:
        return dst
    try:
        with Image.open(dst) as im:
            fmt = (im.format or "").upper()
            if suffix == ".webp" or fmt == "WEBP":
                out = dst.with_suffix(".png")
                im.convert("RGBA").save(out, format="PNG")
                dst.unlink(missing_ok=True)
                return out
            if fmt in ("JPEG", "PNG", "GIF"):
                return dst
    except Exception:
        return None
    return None


def _sticker_tags() -> dict:
    try:
        import sys

        sys.path.insert(0, str(ROOT))
        import stickers

        return {k: len(v) for k, v in stickers.tags().items()}
    except Exception:
        return {}


def _sticker_list() -> dict:
    try:
        import sys

        sys.path.insert(0, str(ROOT))
        import stickers

        return stickers.tags()
    except Exception:
        return {}


def _sticker_path(tag: str, name: str) -> Path | None:
    """只允许 stickers/<tag>/<name> 这种，防目录穿越。"""
    base = (ROOT / "stickers").resolve()
    p = (base / tag / name).resolve()
    try:
        p.relative_to(base)
    except Exception:
        return None
    return p if p.is_file() else None



_CUR_NOTE = {"name": ""}


def _extra_cards() -> str:
    bl = _read(BEHAVIOR, BEHAVIOR_DEFAULTS)
    try:
        import config as _cfg_mod
        pref = _cfg_mod.load(str(ROOT / "config.toml")).private
    except Exception:
        pref = "auto"
    """新增卡片：随机主动 / 每日上限 / 笔记编辑。全部热加载，保存即生效。"""
    import json as _j

    def _rd(pth):
        try:
            return _j.loads(Path(pth).read_text(encoding="utf-8"))
        except Exception:
            return {}

    pro = _rd(DATA_DIR / "proactive.json")
    r = pro.get("random") or {}
    lim = _rd(DATA_DIR / "limits.json")
    notes = sorted(NOTES_DIR.glob("*.md"))
    # 用微信备注名显示，别让人看一屏 wxid；没有登记的退回文件名
    alias = {}
    try:
        import sqlite3
        con = sqlite3.connect(DATA_DIR / "wechat.db")
        for wxid, al in con.execute("select wxid, alias from contacts"):
            if wxid and al:
                alias[f"{wxid}.md"] = al
        con.close()
    except Exception:
        pass
    if not _CUR_NOTE["name"] and notes:
        _CUR_NOTE["name"] = notes[0].name
    # 老的名字式笔记（张三.md）和新的 wxid 式（wxid_xxx.md）会同时出现，
    # 把名字式的标成「旧」，按 wxid 优先排序，免得选错。
    def _is_legacy(n):
        stem = n.stem.lower()
        if stem.endswith("@chatroom"):
            return False
        return not stem.startswith(("wxid_", "yangxing"))

    notes.sort(key=lambda n: (_is_legacy(n), n.stem))
    opts = "".join(
        f'<option value="{n.name}"{" selected" if n.name == _CUR_NOTE["name"] else ""}>'
        f'{"群｜" if n.name.endswith("@chatroom.md") else ""}'
        f'{(alias.get(n.name) or n.stem)}{"（旧）" if _is_legacy(n) else ""}</option>'
        for n in notes)
    cur = ""
    if _CUR_NOTE["name"]:
        try:
            cur = (NOTES_DIR / _CUR_NOTE["name"]).read_text(encoding="utf-8")
        except Exception:
            cur = ""
    if not notes:
        cur = "（还没有任何笔记）"
    # 离线补收：电脑关机/桥没跑时收到的消息，按微信本地库往回捞
    bf_cur = {}
    try:
        bf_cur = json.loads((DATA / "backfill_last.json").read_text(encoding="utf-8"))
    except Exception:
        bf_cur = {}
    bf_info = ""
    if bf_cur:
        bf_info = (f'<p class="muted">上次补收：{bf_cur.get("when", "?")}，'
                   f'捞回 {bf_cur.get("count", 0)} 条（自 {bf_cur.get("since", "?")}）</p>')

    # ---------- 好感 / 心情（默认关闭，behavior.json 里 mood_enabled=true 才显示）----------
    mood_card = ""
    try:
        if not _read(BEHAVIOR, BEHAVIOR_DEFAULTS).get("mood_enabled"):
            raise LookupError("mood-disabled")
        md = _read(DATA / "mood.json", {"contacts": {}, "global": {"mood": 60.0}})
        gm = float((md.get("global") or {}).get("mood", 60.0))
        rows = []
        for wid, st in list((md.get("contacts") or {}).items())[:30]:
            aff = float(st.get("affinity", 50.0))
            nm = alias.get(wid + ".md") or wid[-12:]
            rows.append((nm, aff))
        rows.sort(key=lambda x: -x[1])
        body = "".join(
            f'<tr><td>{n}</td><td>{a:.0f}</td><td>{"█" * max(1, int(a / 10))}</td></tr>'
            for n, a in rows) or '<tr><td colspan="3" class="muted">还没有记录</td></tr>'
        mood_card = f"""
  <div class="card"><h2>好感 / 心情</h2>
    <p class="muted">心情是全局的（0-100，现在 <b>{gm:.0f}</b>）；好感是每个人一份。
    被敷衍、被冲会掉，聊得暖会涨；8 小时没说话情绪自己回中性。</p>
    <table class="tbl"><tr><th>人</th><th>好感</th><th></th></tr>{body}</table>
    <form method="post" action="/mood/reset">
      <button class="btn ghost" type="submit">心情回到 60</button>
      <span class="muted">（好感不动）</span>
    </form>
  </div>"""
    except LookupError:
        pass
    except Exception as _e:
        mood_card = f'<div class="card"><h2>好感 / 心情</h2><p class="muted">读不到：{_e!r}</p></div>'
    bf_card = f"""
<div class="card">
  <h2>离线补收（电脑没开时收到的消息）</h2>
  <p class="muted">桥启动时会自动按“本地库里最后一条消息的时间”往回捞，最多 24 小时；
  超过 4 小时的只进上下文、不回复。下面这个按钮可以手动再捞一次。</p>
  <form method="post" action="/backfill">
    <button type="submit">补收最近未接收的消息</button>
  </form>
  {bf_info}
</div>
"""

    bl2 = _read(BEHAVIOR, BEHAVIOR_DEFAULTS)
    more = f"""
<div class="card"><h2>私聊 / 免打扰 / 限流 / 好奇心</h2>
<form method="post" action="/save/more">
  <input type="hidden" name="_cb" value="1">
  <input type="hidden" name="_card" value="more">
  <p>私聊：<select name="private_mode">
    <option value="" {"selected" if not bl2.get("private_mode") else ""}>跟配置文件（{pref}）</option>
    <option value="auto" {"selected" if bl2.get("private_mode") == "auto" else ""}>全都回</option>
    <option value="prefix" {"selected" if bl2.get("private_mode") == "prefix" else ""}>只回带前缀的</option>
    <option value="off" {"selected" if bl2.get("private_mode") == "off" else ""}>私聊不回</option>
  </select>　前缀 <input name="private_prefix" value="{bl2.get("private_prefix", "")}" size="6"></p>
  <label><input type="checkbox" name="quiet_enabled" value="1" {"checked" if bl2.get("quiet_enabled") else ""}> 免打扰</label>
  <input name="quiet_start" value="{bl2.get("quiet_start", "23:30")}" size="6"> 到
  <input name="quiet_end" value="{bl2.get("quiet_end", "08:00")}" size="6">（只回主人）
  <p>限流：每人每分钟 <input name="rate_per_min" value="{bl2.get("rate_per_min", 20)}" size="4"> 条、
     每小时 <input name="rate_per_hour" value="{bl2.get("rate_per_hour", 240)}" size="5"> 条；
     全号每分钟 <input name="rate_global_min" value="{bl2.get("rate_global_min", 90)}" size="4"> 条</p>
  <p>连发合并窗口 <input name="merge_window_seconds" value="{bl2.get("merge_window_seconds", 1.2)}" size="4"> 秒（同一人连着发，多久内合成一条回）</p>
  <label><input type="checkbox" name="curiosity_enabled" value="1" {"checked" if bl2.get("curiosity_enabled") else ""}> 让她对人有好奇心（主动问点私人的事）</label>
  概率 <input name="curiosity_chance" value="{bl2.get("curiosity_chance", 0.3)}" size="4">
  冷却 <input name="curiosity_cooldown_turns" value="{bl2.get("curiosity_cooldown_turns", 4)}" size="3"> 轮
  <p><label><input type="checkbox" name="curiosity_follow_topic" value="1" {"checked" if bl2.get("curiosity_follow_topic", True) else ""}> 只顺着当前话题追问细节（关掉 = 从话题清单里挑一个新方向，以前那种"硬换话题"）</label></p>
  <p><label><input type="checkbox" name="curiosity_lull_only" value="1" {"checked" if bl2.get("curiosity_lull_only", True) else ""}> 只在冷场时才问</label>
     间隔 <input name="curiosity_lull_seconds" value="{bl2.get("curiosity_lull_seconds", 90)}" size="4"> 秒以上，或他这句不超过
     <input name="curiosity_short_chars" value="{bl2.get("curiosity_short_chars", 6)}" size="3"> 个字</p>
  <p><label><input type="checkbox" name="curiosity_no_switch" value="1" {"checked" if bl2.get("curiosity_no_switch", True) else ""}> 禁止用「对了 / 话说 / 说起来 / 说真的」把话题拐走</label></p>
  <p class="muted">对话收尾追问：对方停下不说话后，她按概率问一句「想更了解你」的问题</p>
  <p><label><input type="checkbox" name="curiosity_ping_enabled" value="1" {"checked" if bl2.get("curiosity_ping_enabled") else ""}> 打开</label> 概率 <input name="curiosity_ping_chance" value="{bl2.get("curiosity_ping_chance", 0.3)}" size="4"> 停 <input name="curiosity_ping_after_minutes" value="{bl2.get("curiosity_ping_after_minutes", 1)}" size="3"> 分钟算结束 CD <input name="curiosity_ping_cooldown_minutes" value="{bl2.get("curiosity_ping_cooldown_minutes", 60)}" size="4"> 分钟</p>
  <p><label><input type="checkbox" name="memes_enabled" value="1" {"checked" if bl2.get("memes_enabled") else ""}> 启用梗库（E:\\wxbot_home\\memes.md）</label></p>
  <button>保存（立即生效）</button>
</form></div>
"""
    return mood_card + more + bf_card + f"""<div class="card"><h2>主动发消息 ② 随机（按概率自己找人聊）</h2>
<p class="muted">每 5 分钟掷一次骰子，命中就自己找个人说句话。</p>
<form method="post" action="/proactive">
  <label><input type="checkbox" name="enabled" value="1" {'checked' if r.get('enabled') else ''}> 开启随机主动</label>
  <p>每次检查命中概率 <input name="chance" value="{r.get('chance', 0.05)}" size="5">（每 5 分钟掷一次）</p>
  <p>每天最多 <input name="per_day" value="{r.get('per_day', 2)}" size="3"> 条；两条至少隔
     <input name="min_interval_minutes" value="{r.get('min_interval_minutes', 240)}" size="4"> 分钟</p>
  <p>只发给（空=有备注的好友随机，多个用逗号）：<br>
     <input name="targets" value="{','.join(r.get('targets') or [])}" style="width:100%"></p>
  <button>保存（立即生效）</button>
</form>
<form method="post" action="/proactive-now" style="margin-top:12px;border-top:1px solid #333;padding-top:10px">
  <p class="muted">手动让她现在就去找某个人（不看概率/额度，她凭记忆自己编一句，几秒后发出）：</p>
  <p>目标（昵称/备注/群名）：<input name="who" placeholder="例如 张三" size="18">
     <button>让她主动找 TA</button></p>
</form></div>

<div class="card"><h2>每日消息上限</h2>
<form method="post" action="/limits">
  <p>全号每天 <input name="daily_total" value="{lim.get('daily_total', 240)}" size="6"> 条；
     每人每天 <input name="daily_per_contact" value="{lim.get('daily_per_contact', 60)}" size="6"> 条</p>
  <label><input type="checkbox" name="unlimited" value="1" {'checked' if lim.get('unlimited') else ''}> 不设上限</label>
  <button>保存（立即生效）</button>
</form></div>

<div class="card"><h2>笔记（她对这个人的长期记忆）</h2>
<form method="get" action="/"> <select name="note">{opts}</select> <button>打开</button></form>
<form method="post" action="/note">
  <input type="hidden" name="name" value="{_CUR_NOTE['name']}">
  <textarea name="content" rows="14" style="width:100%">{cur}</textarea>
  <button>保存（下一条消息就按新记忆说话）</button>
</form></div>"""


def _inject_cbs(html):
    """给每个 /save 表单塞一个隐藏字段 _cbs，列出它渲染了哪些复选框。

    浏览器不会提交未勾选的复选框，所以"表单里没有这个键"有两种含义：
    这张卡没有这个框（要保留原值），或者用户把它取消了（要写 False）。
    有了 _cbs 就能分清这两种情况。
    """
    as_bytes = isinstance(html, (bytes, bytearray))
    if as_bytes:
        html = bytes(html).decode("utf-8", "replace")
    out = []
    pos = 0
    for m in re.finditer(r'<form[^>]*action="/save/(main|more)"[^>]*>', html):
        end = html.find("</form>", m.end())
        if end < 0:
            break
        block = html[m.end():end]
        names = re.findall(r'<input[^>]*type="checkbox"[^>]*name="([^"]+)"', block)
        names += re.findall(r'<input[^>]*name="([^"]+)"[^>]*type="checkbox"', block)
        names = list(dict.fromkeys(names))
        out.append(html[pos:m.end()])
        out.append('<input type="hidden" name="_cbs" value="' + ",".join(names) + '">')
        pos = m.end()
    out.append(html[pos:])
    res = "".join(out)
    return res.encode("utf-8") if as_bytes else res


def _page(msg: str = "", err: str = "") -> bytes:
    b = _read(BEHAVIOR, BEHAVIOR_DEFAULTS)
    try:
        profile = json.loads(SETTINGS.read_text(encoding="utf-8")).get("rhythm_profile", "natural")
    except Exception:
        profile = "natural"
    try:
        persona_text = PERSONA.read_text(encoding="utf-8")
    except Exception:
        persona_text = ""
    try:
        memes_text = MEMES.read_text(encoding="utf-8")
    except Exception:
        memes_text = ""

    def sel(v, cur):
        return " selected" if str(v) == str(cur) else ""

    def selr(v, cur):
        return " checked" if str(v) == str(cur) else ""

    last_line = ""
    try:
        _last = json.loads((DATA / "panel_last.json").read_text(encoding="utf-8"))
        _ch = "、".join(list((_last.get("changed") or {}).keys())[:6]) or "无变化"
        last_line = f"<p class='muted'>上次保存：{_last.get('at','')}（{_last.get('card','')}）→ {html.escape(_ch)}</p>"
    except Exception:
        last_line = ""

    lib = _sticker_tags()
    lib_html = "、".join(f"{html.escape(k)}×{v}" for k, v in sorted(lib.items())) or "（空）"

    # 表情库管理区：每张图带缩略图 + 删 / 换标签，每个标签可以整档删
    all_tags = sorted(lib.keys())
    cards = []
    for tag in all_tags:
        names = _sticker_list().get(tag, [])
        cells = []
        for name in names:
            nm = html.escape(name)
            tg = html.escape(tag)
            cells.append(
                "<div class='st'>"
                f"<a href='/img?t={quote(tag)}&n={quote(name)}' target='_blank'>"
                f"<img src='/img?t={quote(tag)}&n={quote(name)}' loading='lazy'></a>"
                f"<div class='muted' style='word-break:break-all'>{nm}</div>"
                "<div class='rowbtns'>"
                "<form method='post' action='/sticker/del'>"
                f"<input type='hidden' name='tag' value='{tg}'><input type='hidden' name='name' value='{nm}'>"
                "<button class='del' type='submit'>删</button></form>"
                "<form method='post' action='/sticker/move'>"
                f"<input type='hidden' name='tag' value='{tg}'><input type='hidden' name='name' value='{nm}'>"
                "<input type='text' name='to' placeholder='换标签' size='5'>"
                "<button class='ghost' type='submit'>移</button></form>"
                "</div></div>"
            )
        cards.append(
            "<div class='tagblk'>"
            f"<div class='taghead'><b>{html.escape(tag)}</b> <span class='muted'>{len(names)} 张</span>"
                    "<form method='post' action='/sticker/add' enctype='multipart/form-data' style='display:inline;margin-left:8px'>"
            f"<input type='hidden' name='tag' value='{html.escape(tag)}'>"
            "<label class='up'>传一张<input type='file' name='file' accept='image/*,.gif,.webp' onchange=\"this.form.submit()\"></label>"
            "</form>"
            "<form method='post' action='/sticker/droptag' style='display:inline;margin-left:8px'>"
            f"<input type='hidden' name='tag' value='{html.escape(tag)}'>"
            "<button class='del' type='submit' onclick=\"return confirm('删掉整档 "
            + html.escape(tag).replace('"', "") + " ？')\">删整档</button></form></div>"
            "<div class='grid'>" + ("".join(cells) or "<span class='muted'>（空）</span>") + "</div></div>"
        )
    stickers_html = "".join(cards) or "<p class='muted'>表情库是空的。</p>"

    rhythm_opts = "".join(
        f"<label class='opt'><input type='radio' name='rhythm' value='{r}'{selr(r, profile)}>"
        f"{RHYTHM_CN[r]}</label>" for r in RHYTHMS)

    tags_val = "、".join(b.get("auto_sticker_tags") or [])
    gk_val = "、".join(b.get("group_keywords") or [])
    banner = ""
    if msg:
        banner = f"<div class='ok'>{html.escape(msg)}</div>"
    if err:
        banner += f"<div class='bad'>{html.escape(err)}</div>"

    doc = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>微信桥 · 面板</title>
<style>
:root{{color-scheme:dark}}
body{{font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;background:#16181d;color:#e6e6e6;margin:0;padding:24px}}
h1{{font-size:18px;margin:0 0 4px}} h2{{font-size:15px;margin:0 0 12px}}
.wrap{{max-width:780px;margin:0 auto}}
.card{{background:#1e2126;border:1px solid #2c3038;border-radius:12px;padding:16px 18px;margin:14px 0}}
.muted{{color:#8b929e;font-size:12px}}
label.row{{display:flex;align-items:center;gap:10px;margin:8px 0}}
input[type=number],input[type=time],input[type=text]{{background:#12141a;border:1px solid #333844;color:#e6e6e6;border-radius:8px;padding:6px 8px}}
input[type=number]{{width:80px}} input[type=text]{{width:100%}}
textarea{{width:100%;min-height:220px;background:#12141a;border:1px solid #333844;color:#e6e6e6;border-radius:8px;padding:10px;font:13px/1.7 Consolas,"Microsoft YaHei",monospace}}
button{{background:#2f7d32;border:0;color:#fff;border-radius:8px;padding:8px 16px;font-size:14px;cursor:pointer}}
button.ghost{{background:#2a2f38}} button.del{{background:#5a2b2b;padding:2px 10px;font-size:12px}}
.list{{width:100%;border-collapse:collapse;margin:6px 0}} .list td,.list th{{border-bottom:1px solid #2c3038;padding:6px;text-align:left;font-size:13px}}
.ok{{background:#1f3a22;border:1px solid #2f7d32;padding:8px 12px;border-radius:8px;margin-bottom:8px}}
.bad{{background:#3a1f1f;border:1px solid #8a3a3a;padding:8px 12px;border-radius:8px;margin-bottom:8px}}
.opt{{display:inline-block;margin-right:16px}}
.hint{{color:#8b929e;font-size:12px;margin-top:6px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(108px,1fr));gap:10px}}
.st{{background:#12141a;border:1px solid #2c3038;border-radius:10px;padding:6px}}
.st img{{width:100%;height:96px;object-fit:contain;border-radius:6px;display:block}}
.rowbtns{{display:flex;gap:4px;align-items:center;margin-top:4px}}
.rowbtns form{{display:flex;gap:3px;align-items:center;margin:0}}
.rowbtns input[type=text]{{width:56px;padding:2px 4px;font-size:12px}}
.rowbtns button{{padding:2px 8px;font-size:12px}}
.tagblk{{margin:14px 0}}
.taghead{{margin-bottom:8px}}
label.up{{display:inline-block;background:#2a2f38;color:#cdd4de;border-radius:6px;padding:3px 9px;font-size:12px;cursor:pointer}}
label.up input{{display:none}}
</style></head><body><div class="wrap">
<h1>微信桥面板</h1>
{last_line}
<p class="muted">改完点对应区块的保存；桥最长一分钟内热加载，不用重启。面板只监听本机。</p>
{banner}

<form id="f-main" method="post" action="/save/main">
  <input type="hidden" name="_cb" value="1">
  <input type="hidden" name="_card" value="main">
  <div class="card">
    <h2>一次多回几条（连发）</h2>
    <label class="row"><input type="checkbox" name="bubbles_enabled" {"checked" if b.get("bubbles_enabled") else ""}> 允许把一句回复拆成几条发</label>
    <label class="row">拆成几条：最多 <input type="number" name="bubble_max" min="1" max="6" value="{b.get("bubble_max", 3)}"> 条</label>
    <label class="row">拆开的概率：<input type="number" name="bubble_chance" min="0" max="1" step="any" value="{b.get("bubble_chance", 0.45)}">（0.45 ≈ 一半不到）</label>
    <label class="row">两条之间停 <input type="number" name="bubble_gap_min" min="0.2" max="10" step="any" value="{b.get("bubble_gap_min", 0.8)}"> ~
      <input type="number" name="bubble_gap_max" min="0.2" max="20" step="any" value="{b.get("bubble_gap_max", 2.4)}"> 秒</label>
    <p class="hint">她也会自己决定：回复里换行写了 2~3 行，就按行连发。</p>
  </div>

  <div class="card">
    <h2>顺手发张表情包</h2>
    <label class="row">每轮回复后，有 <input type="number" name="auto_sticker_chance" min="0" max="1" step="any" value="{b.get("auto_sticker_chance", 0.15)}"> 的概率自己补一张（0 = 不发）</label>
    <label class="row">只从这些标签里挑：<input type="text" name="auto_sticker_tags" value="{html.escape(tags_val)}" style="width:320px"></label>
    <p class="hint">用中文顿号/逗号分隔，比如：可爱、开心、害羞。库里没有的标签会被跳过。<br>现在就这些：{lib_html}</p>
    <p class="hint">想让她在正经话题上别甩表情，就把这个概率调小。</p>
  </div>

  <div class="card">
    <h2>群聊里怎么说话</h2>
    <label class="row">回复规则：
      <select name="group_mode">
        <option value="smart" {"selected" if (b.get("group_mode") or "smart") == "smart" else ""}>聪明模式（@ 或明显在叫她 → 回；偶尔主动插一句）</option>
        <option value="at" {"selected" if b.get("group_mode") == "at" else ""}>只回 @ 她的</option>
        <option value="keyword" {"selected" if b.get("group_mode") == "keyword" else ""}>只回含关键词的</option>
        <option value="off" {"selected" if b.get("group_mode") == "off" else ""}>群里完全不回</option>
      </select>
    </label>
    <label class="row">她的名字/外号：<input type="text" name="group_keywords" value="{html.escape(gk_val)}" style="width:320px"></label>
    <label class="row"><input type="checkbox" name="group_at_sender" {"checked" if b.get("group_at_sender") else ""}> 回复时 @ 一下说话的人（默认不 @）</label>
    <label class="row">没人叫她时插一句的概率：<input type="number" name="group_chime_chance" min="0" max="1" step="any" value="{b.get("group_chime_chance", 0.05)}">（0.05 ≈ 二十分之一）</label>
    <label class="row">同一个群两次插话至少隔 <input type="number" name="group_chime_cooldown_minutes" min="1" max="240" value="{b.get("group_chime_cooldown_minutes", 12)}"> 分钟</label>
    <label class="row">她回过话后 <input type="number" name="group_thread_minutes" min="0" max="60" value="{b.get("group_thread_minutes", 0)}"> 分钟内，别人接着说都算在跟她聊（**0 = 关闭**，默认关）</label>
    <p class="hint">名字用中文顿号/逗号分隔。聪明模式下，含名字、以「喂/哎/那个谁」开头、或问她「你在吗/你觉得呢」的，都算在对她说话。</p>
  </div>

  <div class="card">
    <h2>说话节奏</h2>
    {rhythm_opts}
  </div>

  <button type="submit">保存以上设置</button>
  <a href="/" style="margin-left:12px;color:#8b929e">刷新</a>
</form>


<form method="post" action="/sticker/add" enctype="multipart/form-data">
  <div class="card">
    <h2>自己加表情</h2>
    <label class="row">放进 <input type="text" name="tag" value="亲手" style="width:120px"> 这个标签（没有就新建）
      　选图 <input type="file" name="file" accept="image/*,.gif,.webp"></label>
    <label class="row">或者填图片网址 <input type="text" name="url" placeholder="https://…/xxx.gif" style="width:420px"></label>
    <button type="submit">加进库</button>
    <p class="hint">支持 jpg / png / gif / webp（webp 会自动转 png，微信粘贴发图才认）。
      单张 &lt; 12MB。每个标签那一行也有「传一张」，选完立刻传。</p>
  </div>
</form>

<div class="card">
  <h2>表情库（<span id="libcount">{sum(len(v) for v in _sticker_list().values())}</span> 张）</h2>
  <p class="hint">点图可在新窗口看大图。删掉的会立刻从库里消失；「换标签」把这张挪到另一个情绪档（没有就新建）。
    桥每分钟热加载一次，不用重启。</p>
  {stickers_html}
</div>

<form method="post" action="/persona">
  <div class="card">
    <h2>性格 / 人设</h2>
    <p class="hint">这就是她每句话都会照着演的那份人设（<code>E:\\wxbot_home\\PERSONA.md</code>）。
      保存后下一句就生效。改坏了可以清空——清空就退回 AGENTS.md 的默认风格。</p>
    <textarea name="persona">{html.escape(persona_text)}</textarea>
    <p class="hint">字数 {len(persona_text)}（建议 300~1500 字，太长会挤掉别的上下文）</p>
    <button type="submit">保存人设</button>
    <button type="submit" class="ghost" name="clear" value="1"
      onclick="return confirm('清空人设？她会退回默认风格。')">清空</button>
  </div>
</form>

<form method="post" action="/memes">
  <div class="card">
    <h2>梗库（接梗）</h2>
    <p class="hint">一行一个，格式 <code>缩写｜意思｜怎么接</code>。她看懂了才接得住；碰到库里没有的，
      会先瞎猜一句、再上网查，查完自己往这里补一行。太长只塞前 900 字进上下文。</p>
    <textarea name="memes">{html.escape(memes_text)}</textarea>
    <p class="hint">现在 {len(memes_text)} 字</p>
    <button type="submit">保存梗库</button>
  </div>
</form>

<p class="muted" style="margin-top:18px">文件：data\\behavior.json · data\\proactive.json · data\\settings.json · E:\\wxbot_home\\PERSONA.md · E:\\wxbot_home\\memes.md</p>
</div></body></html>""" + _extra_cards() + _JS
    return doc.encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "WxBridgePanel/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code: int, body: bytes, ctype: str = "text/html; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, where: str):
        self.send_response(303)
        self.send_header("Location", where)
        self.end_headers()

    def do_GET(self):
        path, _, qs = self.path.partition("?")
        if path == "/img":
            self._do_img()
            return
        if path == "/saved":
            self._send(200, _page("已保存，桥会在一分钟内生效"))
        elif path in ("/", "/index.html"):
            # 「打开某人的笔记」就是 GET /?note=xxx.md —— 这里必须真的切过去，
            # 否则下拉框换了、下面的文本框还是上一个人的。
            want = (parse_qs(qs).get("note") or [""])[-1]
            if want and "/" not in want and "\\" not in want:
                _CUR_NOTE["name"] = want
            self._send(200, _inject_cbs(_page()))
        else:
            self._send(404, b"not found", "text/plain; charset=utf-8")

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        ctype = self.headers.get("Content-Type") or ""
        files = {}
        if ctype.startswith("multipart/form-data"):
            body = self.rfile.read(n)
            form, files = _parse_multipart(body, ctype)
        else:
            raw = self.rfile.read(n).decode("utf-8", "replace")
            form = {k: v[-1] for k, v in parse_qs(raw, keep_blank_values=True).items()}
        path = self.path.split("?")[0]
        if path == "/backfill":
            # 只在 data/ 下建一个请求文件；桥的主循环看到会执行一次补收，然后自己删掉
            req = DATA / "backfill.json"
            req.write_text(json.dumps({"requested": time.strftime("%Y-%m-%d %H:%M:%S")}),
                           encoding="utf-8")
            self._redirect("/saved?m=" + _q("已请求补收，几秒后看日志"))
            return
        if path == "/proactive":
            self._do_proactive(form)
            return
        if path == "/proactive-now":
            who = (form.get("who") or "").strip()
            if not who:
                self._redirect("/saved?m=" + _q("先写个名字再点"))
                return
            req = DATA / "proactive_now.json"
            req.write_text(json.dumps({"who": who, "at": time.time()},
                                      ensure_ascii=False), encoding="utf-8")
            self._redirect("/saved?m=" + _q(f"已让她去找「{who}」，几秒后发出（看日志/微信）"))
            return
        if path == "/limits":
            self._do_limits(form)
            return
        if path == "/note":
            self._do_note(form)
            return
        if path == "/sticker/add":
            self._do_sticker_add(form, files)
            return
        if path == "/persona":
            self._do_persona(form)
            return
        if path == "/memes":
            self._do_memes(form)
            return
        if path == "/sticker/del":
            self._do_sticker_del(form)
            return
        if path == "/sticker/move":
            self._do_sticker_move(form)
            return
        if path == "/sticker/droptag":
            self._do_sticker_droptag(form)
            return
        card = form.get("_card", "")
        if path == "/save/more":
            card = "more"
        elif path == "/save/main":
            card = "main"
        self._do_save(form, card)

    # ---------- 三个动作 ----------
    def _do_img(self):
        qs = parse_qs(self.path.partition("?")[2])
        tag = (qs.get("t") or [""])[0]
        name = (qs.get("n") or [""])[0]
        p = _sticker_path(tag, name)
        if p is None:
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        ctype = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".gif": "image/gif",
            ".webp": "image/webp",
        }.get(p.suffix.lower(), "application/octet-stream")
        self._send(200, p.read_bytes(), ctype)

    def _do_sticker_del(self, form: dict):
        tag = (form.get("tag") or "").strip()
        name = (form.get("name") or "").strip()
        p = _sticker_path(tag, name)
        if p is None:
            self._redirect("/err?m=" + _q("找不到这张图"))
            return
        try:
            p.unlink()
            msg = f"已删除 {tag}/{name}"
        except Exception as e:
            msg = f"删除失败：{e}"
        self._redirect("/saved?m=" + _q(msg))

    def _do_sticker_move(self, form: dict):
        tag = (form.get("tag") or "").strip()
        name = (form.get("name") or "").strip()
        to = (form.get("to") or "").strip().replace("/", "").replace("\\", "")[:20]
        p = _sticker_path(tag, name)
        if p is None:
            self._redirect("/err?m=" + _q("找不到这张图"))
            return
        if not to:
            self._redirect("/err?m=" + _q("目标标签不能空"))
            return
        try:
            (ROOT / "stickers" / to).mkdir(parents=True, exist_ok=True)
            p.replace(ROOT / "stickers" / to / p.name)
            msg = f"已移到「{to}」"
        except Exception as e:
            msg = f"移动失败：{e}"
        self._redirect("/saved?m=" + _q(msg))

    def _do_sticker_droptag(self, form: dict):
        tag = (form.get("tag") or "").strip()
        base = (ROOT / "stickers").resolve()
        if not tag:
            self._redirect("/err?m=" + _q("没写标签"))
            return
        d = (ROOT / "stickers" / tag).resolve()
        try:
            d.relative_to(base)
        except Exception:
            self._redirect("/err?m=" + _q("标签不合法"))
            return
        if d == base:
            self._redirect("/err?m=" + _q("不能删整个表情库"))
            return
        if not d.is_dir():
            self._redirect("/err?m=" + _q("没有这个标签"))
            return
        try:
            import shutil

            shutil.rmtree(d)
            msg = f"已删掉整档「{tag}」"
        except Exception as e:
            msg = f"删除失败：{e}"
        self._redirect("/saved?m=" + _q(msg))

    def _do_proactive(self, form: dict):
        """只改 proactive.json 里的 random 段，别把定时那段冲掉。"""
        try:
            cur = json.loads(PROACTIVE.read_text(encoding="utf-8")) if PROACTIVE.exists() else {}
        except Exception:
            cur = {}
        if not isinstance(cur, dict):
            cur = {}
        cur.setdefault("enabled", False)
        cur.setdefault("max_per_day", 3)
        cur.setdefault("min_interval_minutes", 180)
        cur.setdefault("window", "09:00-22:00")
        cur.setdefault("items", [])
        try:
            chance = max(0.0, min(1.0, float(form.get("chance") or 0.05)))
        except Exception:
            chance = 0.05
        try:
            per_day = int(float(form.get("per_day") or 2))
        except Exception:
            per_day = 2
        try:
            gap = int(float(form.get("min_interval_minutes") or 240))
        except Exception:
            gap = 240
        targets = [t.strip() for t in (form.get("targets") or "").replace("，", ",").split(",") if t.strip()]
        old = cur.get("random") or {}
        cur["random"] = {
            "enabled": "enabled" in form,
            "chance": chance,
            "per_day": max(0, per_day),
            "min_interval_minutes": max(1, gap),
            "targets": targets,
        }
        # 当天的运行态保留，别因为改个参数就清零
        if "random_state" not in cur and old.get("enabled") is not None:
            cur["random_state"] = {"day": "", "count": 0, "last_ts": 0.0, "sent": []}
        PROACTIVE.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
        self._redirect("/saved?m=" + _q("随机主动已保存"))

    def _do_limits(self, form: dict):
        try:
            total = int(float(form.get("daily_total") or 240))
        except Exception:
            total = 240
        try:
            per = int(float(form.get("daily_per_contact") or 60))
        except Exception:
            per = 60
        data = {"unlimited": "unlimited" in form, "daily_total": max(0, total),
                "daily_per_contact": max(0, per)}
        (DATA_DIR / "limits.json").write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                              encoding="utf-8")
        self._redirect("/saved?m=" + _q("上限已保存"))

    def _do_mood_reset(self):
        try:
            p = DATA / "mood.json"
            d = _read(p, {"contacts": {}, "global": {}})
            d.setdefault("global", {})["mood"] = 60.0
            d["global"]["ts"] = 0.0
            p.write_text(json.dumps(d, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            self._redirect("/saved?m=心情已回到%2060")
        except Exception as e:
            self._redirect("/saved?m=" + quote(f"恢复失败：{e!r}"))

    def _do_note(self, form: dict):
        name = (form.get("name") or "").strip()
        if not name or "/" in name or "\\" in name or not name.endswith(".md"):
            self._redirect("/err?m=" + _q("笔记文件名不对"))
            return
        text = _norm_note_text(form.get("content") or "")
        if len(text) > 20000:
            self._redirect("/err?m=" + _q(f"这份笔记太长了（{len(text)} 字），先精简到 2 万字以内"))
            return
        (NOTES_DIR / name).write_text(text, encoding="utf-8")
        _CUR_NOTE["name"] = name
        self._redirect("/saved?m=" + _q(f"笔记已保存：{name}"))

    def _do_sticker_add(self, form: dict, files: dict):
        tag = (form.get("tag") or "").strip() or "亲手"
        url = (form.get("url") or "").strip()
        name, data = files.get("file") or ("", b"")
        try:
            import sys

            sys.path.insert(0, str(ROOT))
            import stickers

            if data:
                p = _save_upload(name or "up.png", data)
                if p is None:
                    self._redirect("/err?m=" + _q("这个文件我认不出来（支持 jpg/png/gif/webp）"))
                    return
                got = stickers.add_file(p, tag)
                p.unlink(missing_ok=True)
                self._redirect("/saved?m=" + _q(f"已加进「{tag}」：{got.name}"))
                return
            if url.startswith(("http://", "https://")):
                dst = DATA / "upload_tmp" / "fromurl.webp"
                if not stickers._download(url, dst):
                    self._redirect("/err?m=" + _q("这个网址没下下来（也可能是防盗链）"))
                    return
                src = dst.with_suffix(".png") if dst.with_suffix(".png").exists() else dst
                got = stickers.add_file(src, tag)
                src.unlink(missing_ok=True)
                dst.unlink(missing_ok=True)
                self._redirect("/saved?m=" + _q(f"已从网址加进「{tag}」：{got.name}"))
                return
            self._redirect("/err?m=" + _q("没选文件，也没填网址"))
        except Exception as e:
            self._redirect("/err?m=" + _q(f"加图失败：{e}"))

    def _do_persona(self, form: dict):
        try:
            if form.get("clear"):
                PERSONA.write_text("", encoding="utf-8")
                msg = "人设已清空，退回 AGENTS.md 的默认风格"
            else:
                text = _norm_note_text(form.get("persona") or "")
                if len(text) > 8000:
                    self._redirect("/saved?m=" + _q(f"人设太长了（{len(text)} 字），先精简到 8000 字以内再存"))
                    return
                PERSONA.parent.mkdir(parents=True, exist_ok=True)
                PERSONA.write_text(text, encoding="utf-8")
                msg = f"人设已保存（{len(text)} 字），下一句就生效"
        except Exception as e:
            msg = f"保存失败：{e}"
        self._redirect("/saved?m=" + _q(msg))

    def _do_memes(self, form: dict):
        try:
            text = _norm_note_text(form.get("memes") or "")
            if len(text) > 8000:
                self._redirect("/saved?m=" + _q(f"梗库太长了（{len(text)} 字），先精简到 8000 字以内再存"))
                return
            MEMES.parent.mkdir(parents=True, exist_ok=True)
            MEMES.write_text(text, encoding="utf-8")
            msg = f"梗库已保存（{len(text)} 字），下一句就生效"
        except Exception as e:
            msg = f"保存失败：{e}"
        self._redirect("/saved?m=" + _q(msg))

    def _do_save(self, form: dict, card: str = ""):
        keys = CARD_KEYS.get(card)
        if keys:
            form = {k: v for k, v in form.items() if k in keys or k.startswith("_")}
        _old = _read(BEHAVIOR, BEHAVIOR_DEFAULTS)

        def num(k, d, lo=None, hi=None):
            """表单里没这个键，就用当前文件里的值（而不是默认值），这样两张卡分开保存不会互相覆盖。"""
            if k not in form:
                try:
                    v = float(_old.get(k, d))
                except Exception:
                    v = float(d)
            else:
                try:
                    v = float(form.get(k, d))
                except Exception:
                    v = float(d)
            if lo is not None:
                v = max(lo, v)
            if hi is not None:
                v = min(hi, v)
            return v

        def txt(k, d):
            return form[k] if k in form else _old.get(k, d)

        raw_tags = (form.get("auto_sticker_tags") or "").replace("，", "、").replace(",", "、")
        tags = [t.strip() for t in raw_tags.split("、") if t.strip()][:8]
        raw_kw = (form.get("group_keywords") or "").replace("，", "、").replace(",", "、")
        kws = [t.strip() for t in raw_kw.split("、") if t.strip()][:12]
        gmode = form.get("group_mode") or _old.get("group_mode") or "smart"
        if gmode not in ("smart", "at", "keyword", "off"):
            gmode = "smart"

        has_cb = form.get("_cb") == "1"

        _cbs = set(t for t in (form.get("_cbs") or "").split(",") if t)

        def _cbval(key: str) -> bool:
            # 这张卡渲染过这个框 → 表单里没有就是用户取消了；没渲染过 → 保留原值
            if key in _cbs:
                return key in form
            if key in form:
                return bool(form[key])
            return bool(_old.get(key, False))

        _before = _read(BEHAVIOR, BEHAVIOR_DEFAULTS)
        _before_profile = _profile_now()
        _payload = {
            "bubbles_enabled": _cbval("bubbles_enabled"),
            "bubble_chance": num("bubble_chance", 0.45, 0, 1),
            "bubble_max": int(num("bubble_max", 3, 1, 6)),
            "bubble_gap_min": num("bubble_gap_min", 0.8, 0.2, 20),
            "bubble_gap_max": num("bubble_gap_max", 2.4, 0.2, 30),
            "auto_sticker_chance": num("auto_sticker_chance", 0.15, 0, 1),
            "auto_sticker_tags": tags or _old.get("auto_sticker_tags") or BEHAVIOR_DEFAULTS["auto_sticker_tags"],
            "group_mode": gmode,
            "group_keywords": kws or _old.get("group_keywords") or BEHAVIOR_DEFAULTS["group_keywords"],
            "group_at_sender": _cbval("group_at_sender"),
            "group_chime_chance": num("group_chime_chance", 0.05, 0, 1),
            "group_chime_cooldown_minutes": int(num("group_chime_cooldown_minutes", 12, 1, 240)),
            "group_thread_minutes": int(num("group_thread_minutes", 6, 0, 60)),
            "private_mode": txt("private_mode", _old.get("private_mode", "")),
            "private_prefix": txt("private_prefix", _old.get("private_prefix", "")),
            "quiet_enabled": _cbval("quiet_enabled"),
            "quiet_start": txt("quiet_start", _old.get("quiet_start", "23:30")),
            "quiet_end": txt("quiet_end", _old.get("quiet_end", "08:00")),
            "rate_per_min": int(num("rate_per_min", 20, 1, 600)),
            "rate_per_hour": int(num("rate_per_hour", 240, 1, 5000)),
            "rate_global_min": int(num("rate_global_min", 90, 1, 2000)),
            "merge_window_seconds": num("merge_window_seconds", 1.2, 0, 10),
            "memes_enabled": _cbval("memes_enabled"),
            "curiosity_enabled": _cbval("curiosity_enabled"),
            "curiosity_chance": num("curiosity_chance", 0.3, 0, 1),
            "curiosity_cooldown_turns": int(num("curiosity_cooldown_turns", 4, 0, 50)),
            "curiosity_follow_topic": _cbval("curiosity_follow_topic"),
            "curiosity_lull_only": _cbval("curiosity_lull_only"),
            "curiosity_lull_seconds": num("curiosity_lull_seconds", 90, 5, 86400),
            "curiosity_short_chars": int(num("curiosity_short_chars", 6, 0, 100)),
            "curiosity_no_switch": _cbval("curiosity_no_switch"),
            "curiosity_ping_enabled": _cbval("curiosity_ping_enabled"),
            "curiosity_ping_chance": num("curiosity_ping_chance", 0.3, 0, 1),
            "curiosity_ping_after_minutes": num("curiosity_ping_after_minutes", 1, 0.5, 600),
            "curiosity_ping_cooldown_minutes": num("curiosity_ping_cooldown_minutes", 60, 1, 2880),
        }
        _write_behavior(_payload)
        _after = _read(BEHAVIOR, BEHAVIOR_DEFAULTS)

        changed = {}
        for k, v in _after.items():
            if _before.get(k) != v:
                changed[k] = v
        if "rhythm" in form:
            prof = form.get("rhythm") or "natural"
            prof = prof if prof in RHYTHMS else "natural"
            _write_settings(prof)
            if _before_profile != prof:
                changed["节奏档"] = prof
        _note_save(card or "all", changed)
        self._redirect("/saved?m=" + _q(_fmt_changed(changed)))


def _q(s: str) -> str:
    from urllib.parse import quote

    return quote(s)


class Handler2(Handler):
    def do_GET(self):
        path, _, qs = self.path.partition("?")
        if path in ("/saved", "/err"):
            m = parse_qs(qs).get("m", [""])[-1]
            if path == "/err":
                self._send(200, _inject_cbs(_page(err=m)))
            else:
                self._send(200, _inject_cbs(_page(msg="已保存，桥会在一分钟内生效" + (f"；{m}" if m else ""))))
            return
        super().do_GET()


def main(port: int = 8765, open_browser: bool = True) -> int:
    # 单实例：抢不到锁就退出（Windows 上允许两个进程绑同一端口，靠端口判断不可靠）
    import msvcrt

    DATA.mkdir(parents=True, exist_ok=True)
    lock_path = DATA / "panel.lock"
    lock_f = open(lock_path, "w")
    try:
        msvcrt.locking(lock_f.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        print(f"已经有一个面板在跑（{lock_path} 被占用），本进程退出")
        return 0
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler2)
    url = f"http://127.0.0.1:{port}"
    print(f"面板：{url}  （Ctrl+C 关闭）")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    import sys

    p = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    raise SystemExit(main(p, open_browser="--no-open" not in sys.argv))
