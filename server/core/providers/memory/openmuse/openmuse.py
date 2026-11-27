"""
OpenMuse 记忆后端（真源 = G:\\openmuse 的 /api/memory，不另建本地库）

设计要点：
1. 只读注入：query_memory() 把 OpenMuse 的记忆条目原样取回，交给 xiaozhi-server
   原生的 <memory> 注入通道（agent-base-prompt.txt:82）。这样记忆“始终在场”，
   不依赖模型主动调用工具，比 MCP 工具检索更可靠。
2. 过滤 digest：OpenMuse 会定时写入“团队动态”代码笔记，实测占 3293/3450 字符，
   全是噪音，注入后会污染对话，这里按 category 排除。
3. 写入克制：只有在用户明确说“记住 / 以后叫我…”时才回写，避免把
   “前进十秒”之类的指令写进记忆库。
"""

import asyncio
import json
import os
import urllib.request

from ..base import MemoryProviderBase, logger

TAG = __name__

API_BASE = os.environ.get("OPENMUSE_API_BASE", "http://127.0.0.1:8890").rstrip("/")
TOKEN_FILE = os.environ.get(
    "OPENMUSE_TOKEN_FILE", r"G:\openmuse\data\server_token"
)

WRITE_TRIGGERS = ("记住", "以后叫我", "以后喊我", "以后称呼我", "以后就叫我")
WRITE_NEGATIVE = ("不记住", "没记住", "别记住", "不用记住")


def _token() -> str:
    try:
        with open(TOKEN_FILE, encoding="utf-8") as fh:
            return fh.read().strip()
    except Exception:
        return ""


def _req(method: str, path: str, body=None, timeout: float = 5.0):
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(API_BASE + path, data=data, method=method)
    req.add_header("Authorization", "Bearer " + _token())
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", "replace")
    return json.loads(raw) if raw else None


def _text_of(msg):
    """兼容 dict / Message 两种形态。"""
    content = msg.get("content") if isinstance(msg, dict) else getattr(msg, "content", None)
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or ""))
            else:
                parts.append(str(item))
        return " ".join(p for p in parts if p).strip()
    return str(content or "").strip()


def _role_of(msg):
    return msg.get("role") if isinstance(msg, dict) else getattr(msg, "role", None)


class MemoryProvider(MemoryProviderBase):
    def __init__(self, config, summary_memory=None):
        super().__init__(config)
        self.max_items = int((config or {}).get("max_items", 30))
        excluded = (config or {}).get("exclude_categories", ["digest"])
        self.exclude = {str(x).lower() for x in (excluded or [])}

    async def save_memory(self, msgs, session_id=None):
        try:
            last_user = ""
            for msg in msgs or []:
                if _role_of(msg) == "user":
                    last_user = _text_of(msg)
            if not last_user:
                return None
            if any(neg in last_user for neg in WRITE_NEGATIVE):
                return None
            if not any(word in last_user for word in WRITE_TRIGGERS):
                return None
            await asyncio.to_thread(
                _req,
                "POST",
                "/api/memory",
                {"content": last_user[:500], "category": "profile"},
            )
            logger.bind(tag=TAG).info("openmuse 记忆已写入: %s" % last_user[:40])
        except Exception as exc:
            logger.bind(tag=TAG).warning("openmuse save_memory 失败: %s" % exc)
        return None

    async def query_memory(self, query: str) -> str:
        try:
            data = await asyncio.to_thread(_req, "GET", "/api/memory")
            items = data if isinstance(data, list) else (data or {}).get("memories", [])
            lines = []
            for item in items[: self.max_items]:
                if not isinstance(item, dict):
                    continue
                if str(item.get("category") or "").lower() in self.exclude:
                    continue
                content = str(item.get("content") or "").strip()
                if content:
                    lines.append("- " + content[:500])
            if not lines:
                return ""
            logger.bind(tag=TAG).debug("openmuse 注入记忆 %d 条" % len(lines))
            return "\n".join(lines)
        except Exception as exc:
            logger.bind(tag=TAG).warning("openmuse query_memory 失败: %s" % exc)
            return ""
