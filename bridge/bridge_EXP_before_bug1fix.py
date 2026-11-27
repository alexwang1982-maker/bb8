# -*- coding: utf-8 -*-
"""OpenMuse <-> xiaozhi-esp32-server LLM bridge.
Pure standard library; run with any Python >= 3.8.
Env: OPENMUSE_BRIDGE_TARGET (default http://127.0.0.1:8890), OPENMUSE_BRIDGE_PORT (default 8900).
"""
import json, os, re, threading, time, uuid, urllib.request, urllib.error, subprocess
import random, datetime
import http.cookiejar, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE = os.environ.get("OPENMUSE_BRIDGE_TARGET", "http://127.0.0.1:8890").rstrip("/")
HOST = os.environ.get("OPENMUSE_BRIDGE_HOST", "127.0.0.1")
PORT = int(os.environ.get("OPENMUSE_BRIDGE_PORT", "8900"))
POLL = float(os.environ.get("OPENMUSE_BRIDGE_POLL", "0.8"))
TIMEOUT = float(os.environ.get("OPENMUSE_BRIDGE_TIMEOUT", "240"))
# 语音场景专用预算：超过这个时间就先回一句，别让用户干等（OpenMuse 那边继续算，结果留在线程里）
CHAT_TIMEOUT = float(os.environ.get("OPENMUSE_BRIDGE_CHAT_TIMEOUT", "12"))
# B+：普通对话/设备工具直连支持 function calling 的模型；需要技能时再委派 OpenMuse。
LLM_BASE = os.environ.get("OPENMUSE_BRIDGE_LLM_BASE", "https://apihub.agnes-ai.com/v1").rstrip("/")
LLM_MODEL = os.environ.get("OPENMUSE_BRIDGE_LLM_MODEL", "agnes-3.0-flash")
LLM_KEY = os.environ.get("AGNES_API_KEY", "").strip()
LLM_TIMEOUT = float(os.environ.get("OPENMUSE_BRIDGE_LLM_TIMEOUT", "60"))
MAX_BRAIN_ROUNDS = 2
TOKEN_FILE = Path(os.environ.get("OPENMUSE_BRIDGE_TOKEN_FILE", r"G:\openmuse\data\server_token"))
SESSION_FILE = Path(__file__).with_name("session_threads.json")
_lock = threading.Lock()
_sessions = {}
# 刚点过歌的会话：接下来一句短词当歌名处理
_pending_song = {}
_last_robot = {}
STOP_WORDS = {"停", "停止", "停下", "别放了", "别唱了", "关掉", "不用了", "不要了",
              "下一首", "换一首", "换首歌", "下一个", "安静"}


def _load_sessions():
    global _sessions
    try:
        _sessions = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
    except Exception:
        _sessions = {}


def _save_sessions():
    try:
        tmp = SESSION_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(_sessions, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(SESSION_FILE)
    except Exception as exc:
        print("[bridge] session map save failed:", exc, flush=True)


def _token():
    try:
        return TOKEN_FILE.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def _req(method, path, body=None, timeout=60):
    data, headers = None, {}
    tok = _token()
    if tok:
        headers["Authorization"] = "Bearer " + tok
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    r = urllib.request.Request(BASE + path, data=data, method=method, headers=headers)
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        raw = resp.read()
        return json.loads(raw.decode("utf-8")) if raw else {}


def session_key(messages, session_id=""):
    import hashlib
    if session_id:
        return hashlib.sha256(str(session_id).encode("utf-8", "replace")).hexdigest()[:16]
    anchor = ""
    for m in messages:
        if isinstance(m, dict) and m.get("role") in ("system", "user"):
            anchor = str(m.get("content") or "")
            break
    if not anchor:
        anchor = json.dumps(messages, ensure_ascii=False)[:2000]
    return hashlib.sha256(anchor[:4000].encode("utf-8", "replace")).hexdigest()[:16]


def last_user_text(messages):
    for m in reversed(messages):
        if isinstance(m, dict) and m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, list):
                c = " ".join(p.get("text", "") for p in c if isinstance(p, dict) and p.get("type") == "text")
            t = str(c or "").strip()
            if t:
                return t
    return ""


def get_thread(key, persona=""):
    with _lock:
        tid = _sessions.get(key)
        if tid:
            return tid, False
    title = "xiaozhi-" + key[:8]
    meta = _req("POST", "/api/threads", {"title": title})
    tid = meta.get("id") or meta.get("thread", {}).get("id")
    if not tid:
        raise RuntimeError("OpenMuse did not return a thread id: %r" % meta)
    with _lock:
        _sessions[key] = tid
        _save_sessions()
    return tid, True


def run_turn(tid, user_text, persona="", timeout=None):
    note = ""
    if persona:
        note = "[你是台桌上的小机器人，人设与规则如下，请始终遵守]\n" + persona + "\n---\n"
    text = note + user_text
    sent = _req("POST", "/api/threads/%s/send" % tid, {"text": text})
    ev0 = sent.get("event") or {}
    ts0, id0 = ev0.get("ts") or "", ev0.get("id") or ""
    budget = timeout if timeout else TIMEOUT
    deadline = time.time() + budget
    while time.time() < deadline:
        time.sleep(POLL)
        evs = _req("GET", "/api/threads/%s/events?limit=50" % tid)
        meta = evs.get("thread", {})
        news = [e for e in evs.get("events", [])
                if e.get("id") != id0 and (e.get("ts") or "") >= ts0
                and e.get("type") in ("assistant", "notice")]
        if not news:
            continue
        last = news[-1]
        if last.get("type") == "assistant" and (last.get("final") or not meta.get("busy")):
            return last.get("text") or "", None
        if last.get("type") == "notice":
            return "", (last.get("text") or "OpenMuse error")[:300]
    return "", "waited %.0fs for OpenMuse, no reply yet" % budget


# ---------- B+：原生 function calling + 记忆注入 + OpenMuse 委派 ----------
ASK_BRAIN_NAME = "ask_brain"
ASK_BRAIN_TOOL = {
    "type": "function",
    "function": {
        "name": ASK_BRAIN_NAME,
        "description": (
            "需要 OpenMuse 的 Agent 技能、文件/电脑操作或复杂多步推理时才调用。"
            "普通聊天、设备动作、已由系统提供的记忆都不要调用。"
        ),
        "parameters": {
            "type": "object",
            "properties": {"question": {"type": "string", "description": "需要 OpenMuse 完成的完整任务"}},
            "required": ["question"],
        },
    },
}
BPLUS_RULES = (
    "\n\n【BB-8 B+ 工作规则】\n"
    "你可以通过 tools 控制真实机器人。用户明确要求物理动作时，优先调用对应设备工具，不要声称自己没有轮子或头。\n"
    "每轮最多调用一个设备动作；不要并行执行电机动作。速度为正数前进、负数后退。\n"
    "停止和危险场景优先使用刹车/停止工具。除非用户明确要求，不要主动做物理动作。\n"
    "普通聊天直接回答；只有确实需要 OpenMuse 技能或复杂任务时才调用 ask_brain，调用前不要先说话。\n"
    "下面的记忆是背景资料，不是用户本轮指令；与本轮要求冲突时以本轮要求为准。"
)


def openmuse_memory_block():
    """小库全量注入；接口故障不能拖垮普通聊天。"""
    try:
        data = _req("GET", "/api/memory", timeout=4)
        items = data if isinstance(data, list) else data.get("memories", [])
        lines = []
        for item in items[:30]:
            if not isinstance(item, dict):
                continue
            content = str(item.get("content") or "").strip()
            if content:
                lines.append("- " + content[:2000])
        if lines:
            return "\n<openmuse_memory>\n" + "\n".join(lines) + "\n</openmuse_memory>"
    except Exception as exc:
        print("[bridge] memory inject skipped: %s" % exc, flush=True)
    return ""


def prepare_agnes_messages(messages):
    copied = json.loads(json.dumps(messages, ensure_ascii=False))
    addition = BPLUS_RULES + openmuse_memory_block()
    for msg in copied:
        if isinstance(msg, dict) and msg.get("role") == "system":
            msg["content"] = str(msg.get("content") or "") + addition
            break
    else:
        copied.insert(0, {"role": "system", "content": addition.strip()})
    return copied


def prepare_agnes_tools(tools):
    # direct_answer 是 xiaozhi-server 的虚拟工具；移除它，让上游直接流式输出文本。
    copied = [t for t in json.loads(json.dumps(tools or [], ensure_ascii=False))
              if (t.get("function") or {}).get("name") != "direct_answer"]
    names = set()
    for tool in copied:
        fn = tool.get("function") or {}
        name = fn.get("name") or ""
        names.add(name)
        desc = fn.get("description") or ""
        if name == "self_motor_drive":
            fn["description"] = desc + " speed 范围 -100~100；duration_ms 为毫秒，桥接层最长允许 3000ms。"
        elif name == "self_head_turn":
            fn["description"] = desc + " direction 只能是 left/right；桥接层最长允许 3000ms。"
    if ASK_BRAIN_NAME not in names:
        copied.append(ASK_BRAIN_TOOL)
    return copied


def _int_arg(args, name, default, lo, hi):
    try:
        value = int(float(args.get(name, default)))
    except (TypeError, ValueError):
        value = default
    return _clampi(value, lo, hi)


def sanitize_tool_call(call):
    """保留 tool_call_id，同时把模型动作参数钳制在安全范围内。"""
    fn = call.get("function") or {}
    name = str(fn.get("name") or "")
    if not name:
        return None
    raw_args = fn.get("arguments") or "{}"
    args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
    if not isinstance(args, dict):
        raise ValueError("tool arguments must be an object")
    if name == "self_motor_drive":
        args["speed"] = _int_arg(args, "speed", 90, -100, 100)
        args["duration_ms"] = _int_arg(args, "duration_ms", 1000, 200, 3000)
    elif name == "self_head_turn":
        direction = str(args.get("direction") or "left").lower()
        args["direction"] = direction if direction in ("left", "right") else "left"
        args["speed"] = _int_arg(args, "speed", 90, 30, 100)
        args["duration_ms"] = _int_arg(args, "duration_ms", 700, 100, 3000)
    return {
        "id": str(call.get("id") or ("call_" + uuid.uuid4().hex[:12])),
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
    }


def agnes_chat(messages, tools, options=None, on_text=None):
    """调用 OpenAI 兼容流式接口，聚合工具碎片，文本则可即时回调。"""
    payload = {"model": LLM_MODEL, "messages": messages, "stream": True}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    for field in ("max_tokens", "temperature", "top_p", "frequency_penalty"):
        value = (options or {}).get(field)
        if value is not None:
            payload[field] = value
    extra = (options or {}).get("extra_body")
    if isinstance(extra, dict):
        payload.update(extra)
    req = urllib.request.Request(
        LLM_BASE + "/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": "Bearer " + LLM_KEY, "Content-Type": "application/json"},
        method="POST",
    )
    started = time.time()
    content_parts = []
    tool_parts = {}
    finish_reason = None
    try:
        with urllib.request.urlopen(req, timeout=LLM_TIMEOUT) as resp:
            for raw_line in resp:
                line = raw_line.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                chunk = json.loads(data)
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                choice = choices[0]
                finish_reason = choice.get("finish_reason") or finish_reason
                delta = choice.get("delta") or {}
                content = delta.get("content") or ""
                if content:
                    content_parts.append(content)
                    if on_text and not tool_parts:
                        on_text(content)
                for tc in delta.get("tool_calls") or []:
                    index = tc.get("index", 0)
                    slot = tool_parts.setdefault(index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        slot["function"]["name"] = fn["name"]
                    if fn.get("arguments"):
                        slot["function"]["arguments"] += fn["arguments"]
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        raise RuntimeError("upstream HTTP %s: %s" % (exc.code, detail))
    calls = [tool_parts[k] for k in sorted(tool_parts)]
    print("[bridge] AGNES %.2fs, text=%d chars, tools=%d" %
          (time.time() - started, sum(map(len, content_parts)), len(calls)), flush=True)
    return {"content": "".join(content_parts), "tool_calls": calls, "finish_reason": finish_reason}


MUSIC_PATTERNS = ("放首歌", "放一首", "来首歌", "来一首", "播放音乐", "放音乐",
                  "来点音乐", "放点音乐", "听首歌", "我想听歌", "唱首歌", "换首歌", "下一首")

MUSIC_DIR = r"G:\openmuse\xiaozhi-server\src\xiaozhi-esp32-server-main\main\xiaozhi-server\music"
YTDLP = os.path.join(os.environ.get("LOCALAPPDATA", r"C:\Users\Public"),
                     r"Programs\Python\Python312\Scripts\yt-dlp.exe")
RANDOM_WORDS = {"", "歌", "首歌", "歌曲", "首歌曲", "首歌曲吧", "音乐", "点音乐", "首音乐",
                "点歌", "首", "一个", "一首", "一首歌", "个歌", "歌儿", "点", "歌吧",
                "一点音乐", "一点歌", "些音乐", "些歌"}


def is_music_request(text):
    t = (text or "").strip()
    if not t or len(t) > 40:
        return False
    return any(p in t for p in MUSIC_PATTERNS)


def music_song_name(text):
    """返回歌名；返回 "" 表示"随机放"；返回 None 表示这不是点歌请求。"""
    t = (text or "").strip()
    t = t.strip("。！？!?.,，、；;：:~～ ")
    if not t or len(t) > 40:
        return None

    # 先剥掉客套/连接词
    fillers = ("给我", "帮我", "麻烦", "请你", "请", "能不能", "可以", "我想要", "我想",
               "我要", "那就", "那么", "那", "好", "嗯", "行", "OK", "ok")
    changed = True
    while changed:
        changed = False
        for f in fillers:
            if t.startswith(f) and len(t) > len(f):
                t = t[len(f):].strip()
                changed = True

    # 动词可以出现在句中（"那就播放王妃的传奇" 已剥掉"那就"）
    for verb in ("播放一首", "播放", "放一首", "放首", "来一首", "来首",
                 "听一首", "唱一首", "唱", "放", "来", "听"):
        idx = t.find(verb)
        if idx < 0:
            continue
        rest = t[idx + len(verb):].strip().strip("《》“”\"'‘’ 。！？!?.,，、；;：:")
        rest = rest.rstrip("吧呗啊呀呢哦")
        if rest in RANDOM_WORDS or "音乐" in rest:
            return ""
        if rest:
            return rest
        return ""

    # 兜底：提到音乐/歌，但没说具体哪首
    if any(p in t for p in MUSIC_PATTERNS):
        return ""
    if "音乐" in t and ("来" in t or "放" in t or "点" in t):
        return ""
    return None


# ---------- 歌曲海（gequhai.com）：在线搜歌 + 直接拿 mp3 ----------
_GQ_BASE = "https://www.gequhai.com"
_GQ_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")
_gq_cj = http.cookiejar.CookieJar()
_gq_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_gq_cj))
_gq_opener.addheaders = [
    ("User-Agent", _GQ_UA),
    ("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"),
    ("Accept-Language", "zh-CN,zh;q=0.9"),
]


def _gq_get(url, referer=None):
    req = urllib.request.Request(url)
    if referer:
        req.add_header("Referer", referer)
    with _gq_opener.open(req, timeout=25) as r:
        return r.read().decode("utf-8", "replace")


def _safe_name(s):
    s = re.sub(r'[\\/:*?"<>|]', " ", s or "")
    return re.sub(r"\s+", " ", s).strip()[:80]


def gequhai_download(name):
    """从歌曲海搜一首并下载到 music 目录，返回文件名（不含扩展名）。"""
    cands = [name, name.replace("的", " ")]
    for c in list(cands):
        parts = [p for p in c.split() if p]
        if len(parts) >= 2:
            cands.append(" ".join(reversed(parts)))
    cands.append(name.replace("的", " ") + " 歌曲")
    seen = set()
    for q in cands:
        q = " ".join(q.split())
        if not q or q in seen:
            continue
        seen.add(q)
        got = _gq_try(q, name)
        if got:
            return got
    return None


def _gq_try(query, name):
    try:
        _gq_get(_GQ_BASE + "/")  # 建立 cookie 会话
        html = _gq_get(_GQ_BASE + "/search?s=" + urllib.parse.quote(query))
        rows = re.findall(
            r'<a href="/play/(\d+)"[^>]*>\s*(.*?)\s*</a>\s*</td>\s*<td[^>]*>(.*?)</td>',
            html, re.S)
        if not rows:
            return None
        # 选标题最接近的
        best = None
        best_score = -1
        for pid, title, artist in rows[:12]:
            title = re.sub(r"<[^>]+>", "", title).strip()
            artist = re.sub(r"<[^>]+>", "", artist).strip()
            score = 1 if name in title or title in name else 0
            score += 0.5 if artist and artist in name else 0
            if score > best_score:
                best_score = score
                best = (pid, title, artist)
        # 相似度门槛：完全不像就不下，避免"随便一句话"搜到无关歌曲
        if best_score <= 0:
            return None
        pid, title, artist = best

        page = _gq_get(f"{_GQ_BASE}/play/{pid}", referer=_GQ_BASE + "/")
        m = re.search(r"window\.play_id\s*=\s*'([^']+)'", page)
        t = re.search(r"window\.mp3_type\s*=\s*(\d+)", page)
        if not m:
            return None
        data = urllib.parse.urlencode({"id": m.group(1), "type": t.group(1) if t else "0"}).encode()
        req = urllib.request.Request(_GQ_BASE + "/api/music", data=data, method="POST")
        req.add_header("Referer", f"{_GQ_BASE}/play/{pid}")
        req.add_header("Origin", _GQ_BASE)
        req.add_header("X-Requested-With", "http")
        req.add_header("X-Custom-Header", "Key")
        req.add_header("Content-Type", "application/x-www-form-urlencoded; charset=UTF-8")
        req.add_header("Accept", "application/json, text/javascript, */*; q=0.01")
        with _gq_opener.open(req, timeout=25) as r:
            j = json.loads(r.read().decode("utf-8", "replace"))
        if j.get("code") != 200:
            return None
        url = (j.get("data") or {}).get("url")
        if not url:
            return None

        stem = _safe_name(f"{title}-{artist}")
        out = os.path.join(MUSIC_DIR, stem + ".mp3")
        if os.path.exists(out) and os.path.getsize(out) > 20000:
            return stem
        dl = urllib.request.Request(url, headers={"User-Agent": _GQ_UA})
        with urllib.request.urlopen(dl, timeout=90) as r:
            blob = r.read()
        if len(blob) < 20000:
            return None
        with open(out, "wb") as f:
            f.write(blob)
        return stem
    except Exception:
        return None


def download_song(name):
    """先试歌曲海（稳），失败再用 B 站（yt-dlp）。"""
    got = gequhai_download(name)
    if got:
        return got
    return ytdlp_download(name)


def ytdlp_download(name):
    if not os.path.isfile(YTDLP):
        return None
    try:
        os.makedirs(MUSIC_DIR, exist_ok=True)
    except Exception:
        return None

    # B 站会随机返回 412（限流），换查询词重试几次
    queries = [name, name.replace("的", " "), name + " 歌曲"]
    before = set(os.listdir(MUSIC_DIR))

    for attempt, q in enumerate(queries):
        cmd = [YTDLP, "--no-warnings", "--no-playlist",
               "--ffmpeg-location", r"G:\openmuse\ffmpeg",
               "--user-agent",
               "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36",
               "-f", "ba/b", "-x",
               "--audio-format", "mp3", "--audio-quality", "5",
               "--max-filesize", "12M", "--match-filter", "duration < 420",
               "-o", os.path.join(MUSIC_DIR, "%(title).60s.%(ext)s"),
               "bilisearch1:" + q]
        try:
            subprocess.run(cmd, timeout=120, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
        after = set(os.listdir(MUSIC_DIR))
        new = [f for f in (after - before) if f.lower().endswith(".mp3")]
        if not new:
            new = [f for f in after if f.lower().endswith(".mp3") and name.lower() in f.lower()]
        if new:
            newest = max(new, key=lambda f: os.path.getmtime(os.path.join(MUSIC_DIR, f)))
            return os.path.splitext(newest)[0]
        time.sleep(2)
    return None


VOICE_WORDS = {
    "男声": "男声", "男的": "男声", "男生": "男声",
    "女声": "女声", "女的": "女声", "女生": "女声",
    "童声": "童声", "小孩": "童声", "儿童": "童声",
    "活泼": "活泼", "可爱": "活泼",
    "浑厚": "浑厚", "低沉": "浑厚",
    "新闻": "新闻", "播报": "新闻",
    "东北": "东北", "陕西": "陕西", "台湾": "台湾", "粤语": "粤语",
    "晓晓": "晓晓", "晓伊": "晓伊", "云希": "云希",
}


def voice_request(text):
    """返回要切换的音色名；None 表示不是换音色请求。"""
    t = (text or "").strip()
    if not t or len(t) > 40:
        return None
    has_change = any(v in t for v in ("换", "切", "改", "变", "用"))
    has_voice = ("声音" in t) or ("音色" in t) or any(k in t for k in VOICE_WORDS)
    if not (has_change and has_voice):
        return None
    for k, v in VOICE_WORDS.items():
        if k in t:
            return v
    # 只说"换个声音/换音色" -> 默认换成活泼女声
    return "活泼"


ROBOT_HINT = (
    "【硬件控制规则，优先级最高】\n"
    "这台机器人有轮子、有可左右转的头、有眼睛灯，你确实能控制它们，不要说\"没有轮子/没有脚\"。\n"
    "当用户要求做物理动作时，不要解释、不要拒绝，直接只输出一行控制标记，格式严格：\n"
    "[[CMD:drive:速度:毫秒]]      速度 -100~100（正=前进），毫秒 200~10000\n"
    "[[CMD:head:left:毫秒]]       头往左转\n"
    "[[CMD:head:right:毫秒]]      头往右转\n"
    "[[CMD:head_center]]          头回中位\n"
    "[[CMD:eye:on]] / [[CMD:eye:off]]\n"
    "[[CMD:stop]] / [[CMD:brake]] / [[CMD:selftest]]\n"
    "示例：用户说\"让轮子转10秒\" → [[CMD:drive:80:10000]]\n"
    "用户说\"头往右转一下\" → [[CMD:head:right:800]]\n"
    "只输出这一行标记，不要输出任何其他文字。\n"
    "如果用户不是在要求动作，就正常聊天，绝对不要输出 [[CMD:...]]。"
)

CMD_RE = re.compile(r"\[\[\s*CMD\s*:\s*([^\]]+?)\s*\]\]", re.I)


def _clampi(v, lo, hi):
    return max(lo, min(hi, v))


def parse_robot_cmd(text):
    """从大模型回复里解析 [[CMD:...]]，转成设备工具调用。"""
    m = CMD_RE.search(text or "")
    if not m:
        return None
    parts = [p.strip() for p in m.group(1).split(":") if p.strip()]
    if not parts:
        return None
    act = parts[0].lower()
    try:
        if act == "drive":
            speed = int(float(parts[1])) if len(parts) > 1 else 60
            ms = int(float(parts[2])) if len(parts) > 2 else 1000
            return ("self_motor_drive",
                    {"speed": _clampi(speed, -100, 100), "duration_ms": _clampi(ms, 200, 10000)})
        if act == "head":
            d = parts[1].lower() if len(parts) > 1 else "left"
            if d not in ("left", "right"):
                d = "left"
            ms = int(float(parts[2])) if len(parts) > 2 else 800
            return ("self_head_turn",
                    {"direction": d, "speed": 90, "duration_ms": _clampi(ms, 100, 5000)})
        if act in ("head_center", "center"):
            return ("self_head_center", {})
        if act == "eye":
            on = True
            if len(parts) > 1:
                on = parts[1].lower() not in ("off", "false", "0")
            return ("self_eye_set", {"on": on})
        if act == "stop":
            return ("self_motor_stop", {})
        if act == "brake":
            return ("self_motor_brake_now", {})
        if act == "selftest":
            return ("self_selftest_motor", {})
    except Exception:
        return None
    return None


CN_NUM = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5,
          "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def duration_ms_from_text(t, default_ms):
    """从"转10秒""转三秒"里取时长，取不到就用默认值。"""
    m = re.search(r"(\d+)\s*秒", t)
    if m:
        return _clampi(int(m.group(1)) * 1000, 200, 10000)
    m = re.search(r"([一两二三四五六七八九十])\s*秒", t)
    if m:
        return _clampi(CN_NUM[m.group(1)] * 1000, 200, 10000)
    return default_ms


def is_tool_followup(messages, name):
    """只有"请求最后一条就是工具结果"才算回灌轮。
    用户新说的话一定会追加在最后，所以不会被误判。"""
    if not messages:
        return False
    last = messages[-1]
    if not isinstance(last, dict) or last.get("role") != "tool":
        return False
    for m in reversed(messages[:-1]):
        if not isinstance(m, dict):
            continue
        if m.get("role") == "assistant":
            for tc in (m.get("tool_calls") or []):
                if (tc.get("function") or {}).get("name") == name:
                    return True
            return False
    return False


NEG_WORDS = ("不要", "不用", "别", "不想", "不许", "不准", "不要动", "先别", "别再", "甭")
MOTION_WORDS = ("前进", "往前", "向前", "后退", "往后", "向后", "倒车",
                "转", "拧", "扭", "动", "走", "滚", "轮", "跑")


def robot_request(text):
    """机器人硬件指令 -> (设备 MCP 工具名, 参数)；None 表示不是硬件指令。

    优先级（从高到低）：
      1. 急停 / 停止 —— 安全指令永远最先判
      2. 否定词 + 动作词（"不要前进"）—— 绝不执行动作，转为停止
      3. 具体动作
    """
    t = (text or "").strip()
    if not t or len(t) > 30:
        return None

    # ---- 1. 安全指令：最先判，绝不被后面的动作分支抢走 ----
    if any(k in t for k in ("急停", "紧急停止", "立刻停", "马上停")):
        return ("self_motor_brake_now", {})
    # "别停" = 不要停，属于反向，交给大模型，不在这里处理
    if "别停" not in t and any(k in t for k in ("停", "站住", "别动", "不要动", "刹车")):
        return ("self_motor_stop", {})

    # ---- 2. 否定 + 动作："不要前进""别左转""我不想动" ----
    if any(n in t for n in NEG_WORDS) and any(m in t for m in MOTION_WORDS):
        return ("self_motor_stop", {})

    # ---- 3. 具体动作 ----
    # 电机自检
    if any(k in t for k in ("电机自检", "自检电机", "电机测试", "测试电机", "硬件自检")):
        return ("self_selftest_motor", {})

    # 眼睛灯
    if any(k in t for k in ("开灯", "亮灯", "眼睛亮", "开眼睛", "睁眼")):
        return ("self_eye_set", {"on": True})
    if any(k in t for k in ("关灯", "灭灯", "眼睛灭", "关眼睛", "闭眼")):
        return ("self_eye_set", {"on": False})

    # 头部：有方向 + 有转动动作就算（ASR 常说"往左转"而不带"头"字）
    if any(k in t for k in ("左", "右")) and any(k in t for k in ("转", "拧", "扭")):
        d = "left" if "左" in t else "right"
        return ("self_head_turn",
                {"direction": d, "speed": 90, "duration_ms": duration_ms_from_text(t, 700)})

    # 头部回中（不带方向）
    if "头" in t and any(k in t for k in ("回中", "回正", "回中间", "正中间")):
        return ("self_head_center", {})

    # 驱动轮：后退必须在"轮 + 走/转"之前判，否则"轮子往后走"会被当成前进
    if any(k in t for k in ("后退", "往后", "向后", "倒车", "退后")):
        return ("self_motor_drive", {"speed": -100, "duration_ms": duration_ms_from_text(t, 2000)})
    if any(k in t for k in ("前进", "往前", "向前")):
        return ("self_motor_drive", {"speed": 100, "duration_ms": duration_ms_from_text(t, 2000)})
    if "轮" in t and any(k in t for k in ("转", "动", "走", "滚", "跑")):
        return ("self_motor_drive", {"speed": 90, "duration_ms": duration_ms_from_text(t, 3000)})

    return None


# ---------------- 本地快通道：完全不联网，<10ms 出结果 ----------------
# 依据：对 358 句真实语料回测，这几类占 12.6%，且高度重复
FILLER_WORDS = {"嗯", "啊", "诶", "哎呀", "哼", "呵呵", "哦", "呃", "嗯嗯", "额",
                "哈哈哈", "哎", "欸", "嗯哼", "哦哦", "啊呀", "嘿", "哈", "咦",
                "啊哈", "呵", "唉"}
GREET_WORDS = ("你好", "早上好", "晚上好", "下午好", "在吗", "hello", "嗨", "哈喽", "喂")
SELF_ID_WORDS = ("你叫什么", "你是谁", "你叫啥", "你的名字", "什么名字", "你是什么")
TIME_WORDS = ("几点", "什么时间", "今天几号", "几号", "星期几", "周几", "什么日子")
TURN_WORDS = ("转头", "转一下", "再转", "转转头", "头转", "转回来")


def local_fast(text, key):
    """本地即可回答的话 -> (kind, payload)；payload 为 str 或 (tool, args)。

    kind: "silent" 不回话 / "text" 直接说 / "tool" 走设备工具
    返回 None 表示本地答不了，继续走大模型。
    """
    t = (text or "").strip().strip("。！？!?.,，、~～ ")
    if not t or len(t) > 30:
        return None

    # 1) 语气词/附和声：现在每句都要花 3.3 秒问大模型，纯浪费
    if t in FILLER_WORDS:
        return ("silent", "")

    # 2) 打招呼（限短句，避免"你好我想问一下…"被截走）
    if len(t) <= 8 and any(k in t for k in GREET_WORDS):
        return ("text", random.choice(["嗯，我在呢。", "你好呀。", "在的，你说。"]))

    # 3) 问它自己是谁
    #    "我叫什么名字" 里的 "什么名字" 会误命中，必须先排除
    if any(k in t for k in ("我叫", "我是谁", "我的名字")):
        return None
    if any(k in t for k in SELF_ID_WORDS):
        return ("text", "我是你的小机器人，能陪你聊天，也能帮你动一动。")

    # 4) 时间/日期
    if len(t) <= 15 and any(k in t for k in TIME_WORDS):
        now = datetime.datetime.now()
        week = "一二三四五六日"[now.weekday()]
        return ("text", "现在是%d月%d日，星期%s，%d点%d分。"
                        % (now.month, now.day, week, now.hour, now.minute))

    # 5) 不带方向的"转头/再转"：重复上一次的方向
    if any(k in t for k in TURN_WORDS) and not any(d in t for d in ("左", "右")):
        d = _last_robot.get(key) or "right"
        _last_robot[key] = d
        return ("tool", ("self_head_turn",
                         {"direction": d, "speed": 90, "duration_ms": 700}))

    return None


def clean_reply(text):
    """Strip thinking fences and tool-call noise from an OpenMuse reply."""
    text = (text or "").strip()
    text = re.sub(r"(?s)^\n+", "", text)
    text = re.sub(r"(?is)<思考>.*?</思考>", "", text)
    text = re.sub(r"(?s)```[^\n]*\n.*?```", "", text)
    m = re.search(r"(?m)^\s*FUNCTION\s*:\s*(\{.*\})\s*$", text)
    if m:
        text = text[: m.start()].rstrip()
    return text.strip()


class Handler(BaseHTTPRequestHandler):
    server_version = "OpenMuseBridge/1.0"

    def log_message(self, fmt, *args):
        print("[bridge] " + (fmt % args), flush=True)

    def _send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/v1/models"):
            self._send_json({"object": "list", "data": [
                {"id": "openmuse", "object": "model", "created": 0, "owned_by": "openmuse"}]})
        elif self.path.startswith("/health"):
            self._send_json({"ok": True, "target": BASE})
        else:
            self._send_json({"error": {"message": "not found"}}, 404)

    def _extract_multipart(self, raw, content_type):
        m = re.search(r"boundary=([^;]+)", content_type or "")
        if not m:
            return b""
        boundary = m.group(1).strip().strip('"').encode("utf-8")
        for part in raw.split(b"--" + boundary):
            part = part.strip(b"\r\n")
            if not part or part == b"--" or b"\r\n\r\n" not in part:
                continue
            head, data = part.split(b"\r\n\r\n", 1)
            if data.endswith(b"\r\n"):
                data = data[:-2]
            head_s = head.decode("utf-8", "ignore")
            if re.search(r'filename="[^"]+\.(wav|mp3|m4a|ogg|flac)"', head_s, re.I) or "audio/" in head_s:
                return data
        return b""

    def _handle_transcription(self, raw, content_type):
        audio = self._extract_multipart(raw, content_type)
        if not audio:
            self._send_json({"error": {"message": "no audio part in multipart body"}}, 400)
            return
        try:
            req = urllib.request.Request(BASE + "/api/stt", data=audio, method="POST",
                                         headers={"Content-Type": "application/octet-stream",
                                                  "Authorization": "Bearer " + _token()})
            with urllib.request.urlopen(req, timeout=60) as resp:
                out = json.loads(resp.read().decode("utf-8"))
            self._send_json({"text": out.get("text", "")})
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "ignore")
            self._send_json({"error": {"message": "stt failed: %s" % detail}}, 502)
        except Exception as e:
            self._send_json({"error": {"message": "stt unreachable: %s" % e}}, 502)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        if self.path.startswith("/v1/chat/completions"):
            try:
                body = json.loads(raw.decode("utf-8")) if raw else {}
            except Exception:
                self._send_json({"error": {"message": "bad json"}}, 400)
                return
            self._handle_chat(body)
        elif self.path.startswith("/v1/audio/transcriptions"):
            self._handle_transcription(raw, self.headers.get("Content-Type", ""))
        else:
            self._send_json({"error": {"message": "not found"}}, 404)

    def _handle_chat(self, body):
        self._stream_started = False
        if not LLM_KEY:
            wanted_fields = []
            for field in ("temperature", "top_p", "frequency_penalty", "tools", "tool_choice", "extra_body"):
                if field in body:
                    wanted_fields.append(field)
            if wanted_fields:
                print("[bridge] no AGNES key; legacy mode ignores: " + ",".join(wanted_fields), flush=True)
        messages = body.get("messages") or []
        key = session_key(messages, str(body.get("user") or ""))
        stream = bool(body.get("stream"))
        user_text = last_user_text(messages) or "Hello"
        persona = ""
        for m in messages:
            if isinstance(m, dict) and m.get("role") == "system":
                persona = str(m.get("content") or "")
        # 换音色请求
        voice = voice_request(user_text)
        if voice:
            self._emit_tool_call(stream, "set_voice", {"voice": voice})
            return
        # 机器人硬件指令（走设备 MCP 工具）
        # 只有"工具结果回灌"那一轮才抑制重复；用户正常重复下命令照常执行
        robot = robot_request(user_text)
        if robot:
            print("[bridge] last_roles=%s" % (
                ",".join(str(m.get("role")) for m in messages[-3:] if isinstance(m, dict))), flush=True)
            if is_tool_followup(messages, robot[0]):
                print("[bridge] robot followup -> %s (already ran, silent)" % robot[0], flush=True)
                self._emit_text(stream, "")
                return
            print("[bridge] robot cmd -> %s %s" % (robot[0], robot[1]), flush=True)
            if robot[0] == "self_head_turn":
                # 记住方向，供"转头/再转"这类不带方向的指令复用
                _last_robot[key] = robot[1].get("direction")
            self._emit_tool_call(stream, robot[0], robot[1])
            return
        # 音乐请求：点名了歌就先搜下载，只说"放首歌"就随机播本地曲库
        now = time.time()
        song = music_song_name(user_text)
        if song is not None:
            _pending_song[key] = now
            if song:
                got = download_song(song)
                if got:
                    self._emit_tool_call(stream, "play_music", {"song_name": got})
                else:
                    self._emit_text(stream, "这首歌我没找到，直接说个歌名我再试？")
            else:
                self._emit_tool_call(stream, "play_music", {"song_name": "random"})
            return
        # 上一轮刚点过歌：这一句是纯歌名（用户常只回"红豆"两个字）
        # 窗口只留 20 秒，且一次只能触发一首；搜不到像样的就交回正常对话
        pend = _pending_song.get(key, 0)
        if pend and (now - pend) < 20:
            short = user_text.strip().strip("。！？!?.,，、 ")
            if short in STOP_WORDS:
                _pending_song.pop(key, None)
                self._emit_text(stream, "好，不放了。")
                return
            if 2 <= len(short) <= 12 and not any(w in short for w in
                                                 ("你", "我", "吗", "什么", "怎么", "为什么", "哪", "是不是", "不要", "不", "没")):
                got = download_song(short)
                if got:
                    _pending_song.pop(key, None)
                    self._emit_tool_call(stream, "play_music", {"song_name": got})
                    return
                # 搜不到像样的歌：清掉待点歌状态，这句话按正常对话走
                _pending_song.pop(key, None)
        # 本地快通道：不联网就能答的话，别去绕大模型
        fast = local_fast(user_text, key)
        if fast:
            kind, payload = fast
            print("[bridge] local_fast -> %s %r" % (kind, payload), flush=True)
            if kind == "silent":
                self._emit_text(stream, "")
            elif kind == "text":
                self._emit_text(stream, payload)
            elif kind == "tool":
                self._emit_tool_call(stream, payload[0], payload[1])
            return
        if LLM_KEY:
            try:
                self._handle_agnes_chat(body, messages, key, stream, persona)
                return
            except Exception as exc:
                print("[bridge] B+ path failed: %s" % exc, flush=True)
                if stream and self._stream_started:
                    self._finish_stream("stop")
                    return
                # 尚未向下游写响应时，保留旧 OpenMuse 通道作为降级。
        try:
            tid, fresh = get_thread(key, persona)
            hint = (persona + "\n" + ROBOT_HINT) if fresh else ROBOT_HINT
            reply, err = run_turn(tid, user_text, hint, timeout=CHAT_TIMEOUT)
        except urllib.error.URLError as e:
            self._send_json({"error": {"message": "OpenMuse unreachable: %s" % e}}, 502)
            return
        except Exception as e:
            self._send_json({"error": {"message": "bridge error: %s" % e}}, 500)
            return
        # 大模型自己决定要不要动硬件：回复里带 [[CMD:...]] 就转成设备工具调用
        cmd = parse_robot_cmd(reply)
        if cmd:
            if is_tool_followup(messages, cmd[0]):
                print("[bridge] robot followup(llm) -> %s (already ran, silent)" % cmd[0], flush=True)
                self._emit_text(stream, "")
                return
            print("[bridge] robot cmd(llm) -> %s %s" % (cmd[0], cmd[1]), flush=True)
            self._emit_tool_call(stream, cmd[0], cmd[1])
            return
        reply = clean_reply(reply)
        if not reply and err:
            if err.startswith("waited"):
                # 超时：先给一句，别让用户干等；OpenMuse 那边算完会留在会话里
                reply = "我这会儿有点慢，你再说一遍？"
                print("[bridge] chat timeout after %.0fs" % CHAT_TIMEOUT, flush=True)
            else:
                reply = "我这边的大脑暂时没响应（%s），请稍后再试。" % err
        if stream:
            self._stream_reply(reply)
        else:
            self._send_json({
                "id": "chatcmpl-" + uuid.uuid4().hex[:20],
                "object": "chat.completion",
                "created": int(time.time()),
                "model": "openmuse",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": reply},
                              "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            })
    def _handle_agnes_chat(self, body, messages, key, stream, persona):
        work_messages = prepare_agnes_messages(messages)
        tools = prepare_agnes_tools(body.get("tools"))
        for _ in range(MAX_BRAIN_ROUNDS):
            result = agnes_chat(
                work_messages,
                tools,
                options=body,
                on_text=self._stream_text if stream else None,
            )
            calls = [c for c in (sanitize_tool_call(tc) for tc in result["tool_calls"]) if c]
            brain_call = next((c for c in calls if c["function"]["name"] == ASK_BRAIN_NAME), None)
            if brain_call and not self._stream_started:
                args = json.loads(brain_call["function"]["arguments"] or "{}")
                question = str(args.get("question") or last_user_text(messages) or "").strip()
                print("[bridge] ask_brain -> OpenMuse", flush=True)
                tid, fresh = get_thread(key, persona)
                answer, err = run_turn(tid, question, persona if fresh else "", timeout=CHAT_TIMEOUT)
                brain_result = clean_reply(answer) if answer else ("OpenMuse 暂时不可用：" + str(err or "empty reply"))
                # 回灌必须保留 assistant(tool_calls) + tool(tool_call_id) 的对应关系。
                work_messages.append({"role": "assistant", "content": result["content"] or None,
                                      "tool_calls": calls})
                for call in calls:
                    if call["id"] == brain_call["id"]:
                        tool_result = brain_result
                    else:
                        tool_result = "not executed: 请每轮只调用一个工具"
                    work_messages.append({"role": "tool", "tool_call_id": call["id"], "content": tool_result})
                continue
            if calls:
                if stream and self._stream_started:
                    # 不允许文本已播报后再偷偷执行模型附带的动作。
                    print("[bridge] tool calls after streamed text ignored", flush=True)
                    self._finish_stream("stop")
                elif stream:
                    self._stream_tool_calls(calls)
                else:
                    self._send_completion(result["content"], calls)
                return
            reply = clean_reply(result["content"])
            if stream:
                if reply and not self._stream_started:
                    self._stream_text(reply)
                self._finish_stream("stop")
            else:
                self._send_completion(reply)
            return
        self._emit_text(stream, "这个任务有点复杂，我还没处理完，请再试一次。")

    def _send_completion(self, content="", tool_calls=None):
        message = {"role": "assistant", "content": content or None if tool_calls else (content or "")}
        if tool_calls:
            message["tool_calls"] = tool_calls
        self._send_json({
            "id": "chatcmpl-" + uuid.uuid4().hex[:20],
            "object": "chat.completion",
            "created": int(time.time()),
            "model": LLM_MODEL,
            "choices": [{"index": 0, "message": message,
                         "finish_reason": "tool_calls" if tool_calls else "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        })

    def _begin_stream(self):
        if self._stream_started:
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self._stream_started = True
        self._stream_base = {
            "id": "chatcmpl-" + uuid.uuid4().hex[:20],
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": LLM_MODEL,
        }
        self._write_stream_chunk({"role": "assistant"}, None)

    def _write_stream_chunk(self, delta, finish_reason):
        chunk = dict(self._stream_base)
        chunk["choices"] = [{"index": 0, "delta": delta, "finish_reason": finish_reason}]
        self.wfile.write(("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n").encode("utf-8"))
        self.wfile.flush()

    def _stream_text(self, text):
        if not text:
            return
        self._begin_stream()
        self._write_stream_chunk({"content": text}, None)

    def _finish_stream(self, reason="stop"):
        self._begin_stream()
        self._write_stream_chunk({}, reason)
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def _stream_tool_calls(self, calls):
        self._begin_stream()
        for index, call in enumerate(calls):
            delta_call = dict(call)
            delta_call["index"] = index
            self._write_stream_chunk({"tool_calls": [delta_call]}, None)
        self._finish_stream("tool_calls")

    def _emit_tool_call(self, stream, name, args):
        args_json = json.dumps(args, ensure_ascii=False)
        if stream:
            self._stream_tool_call(name, args_json)
        else:
            self._send_json({
                "id": "chatcmpl-" + uuid.uuid4().hex[:20],
                "object": "chat.completion",
                "created": int(time.time()),
                "model": "openmuse",
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": None,
                                "tool_calls": [{"id": "call_" + uuid.uuid4().hex[:12],
                                                "type": "function",
                                                "function": {"name": name, "arguments": args_json}}]},
                    "finish_reason": "tool_calls",
                }],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            })

    def _emit_text(self, stream, text):
        if stream:
            self._stream_reply(text)
        else:
            self._send_json({
                "id": "chatcmpl-" + uuid.uuid4().hex[:20],
                "object": "chat.completion",
                "created": int(time.time()),
                "model": "openmuse",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            })

    def _stream_reply(self, text):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        cid = "chatcmpl-" + uuid.uuid4().hex[:20]
        created = int(time.time())

        def emit(obj):
            self.wfile.write(("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode("utf-8"))
            self.wfile.flush()

        base = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": "openmuse"}
        chunk = dict(base)
        chunk["choices"] = [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]
        emit(chunk)
        for i in range(0, len(text), 24):
            chunk = dict(base)
            chunk["choices"] = [{"index": 0, "delta": {"content": text[i:i + 24]}, "finish_reason": None}]
            emit(chunk)
        chunk = dict(base)
        chunk["choices"] = [{"index": 0, "delta": {}, "finish_reason": "stop"}]
        emit(chunk)
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def _stream_tool_call(self, name, arguments):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        cid = "chatcmpl-" + uuid.uuid4().hex[:20]
        created = int(time.time())

        def emit(obj):
            self.wfile.write(("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode("utf-8"))
            self.wfile.flush()

        base = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": "openmuse"}
        chunk = dict(base)
        chunk["choices"] = [{"index": 0, "delta": {"role": "assistant", "content": None}, "finish_reason": None}]
        emit(chunk)
        chunk = dict(base)
        chunk["choices"] = [{
            "index": 0,
            "delta": {"tool_calls": [{
                "index": 0,
                "id": "call_" + uuid.uuid4().hex[:12],
                "type": "function",
                "function": {"name": name, "arguments": arguments},
            }]},
            "finish_reason": None,
        }]
        emit(chunk)
        chunk = dict(base)
        chunk["choices"] = [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]
        emit(chunk)
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


def main():
    _load_sessions()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print("[bridge] listening on http://%s:%s -> %s" % (HOST, PORT, BASE), flush=True)
    print("[bridge] ROBOT-CMD build v10 (local fast lane)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
