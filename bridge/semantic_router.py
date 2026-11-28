# -*- coding: utf-8 -*-
"""BB-8 unified semantic intent router.

Turns a full user expression (plus the minimum conversation context) into one
explicit decision:

    intent, capability, parameters, confidence

The router does not register tools and does not grant permissions. Device
capabilities are only accepted from detectors supplied by bridge (the existing
keyword/explicit command helpers), so a model-produced route can never invent a
hardware tool that the current expression did not authorize.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

# ------------------------------------------------------------------ constants
INTENT_CHAT = "chat"
INTENT_VISION = "vision"
INTENT_TASK = "task"
INTENT_DEVICE = "device_control"
INTENT_MEMORY = "memory"
INTENT_MUSIC = "music"
INTENT_LOCAL_FAST = "local_fast"
INTENT_CLARIFY = "clarify"
INTENT_UNRESOLVABLE = "unresolvable"

CAP_CHAT = "chat_llm"
CAP_VISION = "vision_observe"
CAP_CLARIFY = "clarify"
CAP_ASK_BRAIN = "ask_brain"
CAP_FORWARD = "forward_to_pc"

ALL_INTENTS = frozenset({
    INTENT_CHAT, INTENT_VISION, INTENT_TASK, INTENT_DEVICE,
    INTENT_MEMORY, INTENT_MUSIC, INTENT_LOCAL_FAST,
    INTENT_CLARIFY, INTENT_UNRESOLVABLE,
})

ACTIONABLE_INTENTS = frozenset({
    INTENT_VISION, INTENT_TASK, INTENT_DEVICE, INTENT_MEMORY, INTENT_MUSIC,
})

_MODEL_THRESHOLD = 0.75
_DECISION_THRESHOLD = 0.55

_INTENT_LABELS = {
    INTENT_VISION: "用摄像头看画面",
    INTENT_TASK: "交给大脑/电脑助手办事",
    INTENT_DEVICE: "控制机器人硬件",
    INTENT_MEMORY: "记住内容",
    INTENT_MUSIC: "播放音乐",
    INTENT_LOCAL_FAST: "本地直接回答",
}

# Colloquial ASR variants are intentionally kept next to the standard wording.
_VISUAL_VERBS = (
    "看看", "看一下", "看一眼", "瞧瞧", "瞅瞅", "瞅一眼", "瞄一眼",
    "拍一下", "拍一张", "拍张照", "拍个照", "识别一下", "读一下", "读一读",
    "念一下", "念出来",
)
_EXPLICIT_VISUAL_NOUNS = (
    "前面", "面前", "身后", "画面", "照片", "图片", "屏幕", "摄像头",
    "你穿", "我穿", "身上", "手里", "桌上", "周围", "四周", "这边", "那边",
)
_DEICTIC_NOUNS = ("这个", "那个", "这儿", "那儿", "这里", "那里", "它", "这东西", "那东西", "啥", "什么")
_VISUAL_CONTEXT_WORDS = (
    "照片", "图片", "画面", "摄像头", "给你看", "发你", "拍了", "拍张", "屏幕上",
)
VISUAL_CONTEXT_WORDS = _VISUAL_CONTEXT_WORDS
_CAPABILITY_INQUIRY = (
    "你能做什么", "你能干啥", "你能干嘛", "你会什么", "你会啥", "你都会啥",
    "你有什么功能", "你有什么本事", "你都能干啥", "你能帮我做什么",
)
_META_CHAT = (
    "你看看你", "看看你自己", "看看你说", "看看你干", "你看看这是谁说的",
    "你看看这叫", "别看看", "不用看",
)
_VAGUE_ACTION = (
    "弄一下", "搞一下", "整一下", "处理一下", "搞一下这个", "弄一下那个",
    "帮我想想办法", "你看着办",
)
_FORWARD_WORDS = (
    "电脑助手", "编码助手", "opencode", "open code", "open-claw", "改代码",
    "改固件", "刷机", "改配置", "编译", "部署", "仓库", "git", "提交代码",
)
_TASK_VERBS = (
    "建一个", "建个", "创建", "写一个", "写个", "改一下", "改成", "删掉",
    "删除", "整理", "下载", "运行", "执行", "生成一个", "做一个", "查一下",
    "搜索一下", "发邮件", "打开电脑上的",
)

# "你看看前面是什么" / "帮我看一下照片里是谁" / "读一下屏幕上的字"
_VISUAL_PATTERNS = tuple(re.compile(p) for p in (
    r"(?:你|请|帮我|麻烦)(?:就)?(?:看看|看一下|看一眼|瞧瞧|瞅瞅|瞅一眼|瞄一眼)"
    r"(?:前面|面前|身后|画面|照片|图片|屏幕|摄像头|你穿|我穿|身上|手里|桌上|周围|四周|这里|那里)",
    r"(?:看看|看一下|看一眼|瞧瞧|瞅瞅|拍一下|拍张照|拍个照|识别一下|读一下|念一下)"
    r"(?:前面|面前|身后|画面|照片|图片|屏幕|摄像头|你穿|我穿|身上|手里|桌上|周围|四周|这里|那里)",
    r"你(?:看到|看见|能看见|能看到|看得到)(?:什么|啥|谁|我|不|吗|了|画面|前面)",
    r"(?:前面|面前|画面|照片|图片|屏幕|摄像头)(?:里|上|中|有|是|显示|拍到|识别)",
))
_BARE_VISUAL_PATTERNS = tuple(re.compile(p) for p in (
    r"^(?:你|请|帮我|麻烦)?(?:看看|看一下|看一眼|瞧瞧|瞅瞅|瞅一眼|瞄一眼|拍一下|拍张照|拍个照|识别一下|读一下|念一下)[。！？!?.,，、~\s]*$",
    r"^(?:帮我|请|麻烦)(?:看看|看一下|看一眼|瞧瞧|瞅瞅)[吧呀啊呢嘛哦]*$",
))
_DEICTIC_VISUAL_PATTERNS = tuple(re.compile(p) for p in (
    r"(?:你|请|帮我|麻烦)?(?:看看|看一下|看一眼|瞧瞧|瞅瞅|瞅一眼|瞄一眼)(?:这个|那个|这东西|那东西|它)(?:$|[。！？!?吗啥])",
    r"(?:你|请|帮我|麻烦)?(?:看看|看一下|看一眼)(?:这个|那个)(?:是|有)?(?:什么|啥|谁)",
    r"(?:你|请|帮我|麻烦)?(?:看看|看一下|看一眼)(?:什么|啥|谁)[。！？!?吗呀呢]*$",
))
_COMPOUND_RE = re.compile(r"[，,；;]|(?:然后|接着|再|同时|并且|另外|还有|顺便)")


# ------------------------------------------------------------------- decision
@dataclass(frozen=True)
class RouteContext:
    """Minimum context needed to resolve deictic expressions such as "这个"."""
    recent_user_texts: Tuple[str, ...] = ()
    recent_tool_names: Tuple[str, ...] = ()
    has_visual_context: bool = False
    tool_followup: bool = False

    @classmethod
    def make(cls,
             recent_user_texts: Optional[Iterable[str]] = None,
             recent_tool_names: Optional[Iterable[str]] = None,
             has_visual_context: bool = False,
             tool_followup: bool = False) -> "RouteContext":
        return cls(
            tuple(str(t) for t in (recent_user_texts or ()) if str(t).strip()),
            tuple(str(t) for t in (recent_tool_names or ()) if str(t).strip()),
            bool(has_visual_context),
            bool(tool_followup),
        )


@dataclass(frozen=True)
class RouteDecision:
    intent: str
    capability: str
    parameters: Dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    reason: str = ""
    clarification: str = ""
    source: str = "deterministic"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "intent": self.intent,
            "capability": self.capability,
            "parameters": dict(self.parameters),
            "confidence": round(float(self.confidence), 3),
            "reason": self.reason,
            "clarification": self.clarification,
            "source": self.source,
        }

    @property
    def needs_clarification(self) -> bool:
        return self.intent in (INTENT_CLARIFY, INTENT_UNRESOLVABLE)

    @property
    def actionable(self) -> bool:
        return self.intent in ACTIONABLE_INTENTS


def _decision(intent: str, capability: str, parameters=None, confidence=0.0,
              reason="", clarification="", source="deterministic") -> RouteDecision:
    return RouteDecision(intent, capability, dict(parameters or {}),
                         float(confidence), reason, clarification, source)


def _clarify(reason: str, question: str, confidence: float = 0.62) -> RouteDecision:
    return _decision(INTENT_CLARIFY, CAP_CLARIFY, {"reason": reason},
                     confidence, reason, question)


def _unresolvable(text: str) -> RouteDecision:
    return _decision(INTENT_UNRESOLVABLE, CAP_CLARIFY, {"input": text[:120]},
                     0.3, "empty_or_unresolvable", "没听明白，你再说一遍？")


# ------------------------------------------------------------------ detectors
def _run_detectors(text: str,
                   detectors: Optional[Sequence[Tuple[str, Callable[[str], Any]]]]
                   ) -> Tuple[Optional[Tuple[str, str, Dict[str, Any]]], Dict[str, str]]:
    """Run the existing bridge command helpers once and keep a capability allowlist.

    A detector returns either (tool_name, args) or a capability string. Only
    what the current expression actually produces becomes authorizable.
    """
    hit: Optional[Tuple[str, str, Dict[str, Any]]] = None
    allowed: Dict[str, str] = {}
    for capability, fn in detectors or ():
        try:
            result = fn(text)
        except Exception:
            result = None
        if not result:
            continue
        if isinstance(result, str):
            tool_name, args = result, {}
        elif isinstance(result, (tuple, list)) and len(result) == 2:
            tool_name, args = str(result[0]), dict(result[1] or {})
        else:
            continue
        if not tool_name:
            continue
        allowed[tool_name] = capability
        if hit is None:
            hit = (capability, tool_name, args)
    return hit, allowed


def _has_explicit_visual_noun(t: str) -> bool:
    return any(w in t for w in _EXPLICIT_VISUAL_NOUNS)


def _has_visual_verb(t: str) -> bool:
    return any(v in t for v in _VISUAL_VERBS)


def _is_capability_inquiry(t: str) -> bool:
    return any(w in t for w in _CAPABILITY_INQUIRY)


def _is_meta_chat(t: str) -> bool:
    return any(w in t for w in _META_CHAT)


def _is_vague_action(t: str) -> bool:
    return any(w in t for w in _VAGUE_ACTION)


def _vision_reason(t: str, ctx: RouteContext) -> Optional[str]:
    if _is_meta_chat(t) or _is_capability_inquiry(t):
        return None
    # "眼睛灯" is a hardware command, never a camera request.
    if "眼睛灯" in t:
        return None
    for p in _DEICTIC_VISUAL_PATTERNS:
        if p.search(t):
            if ctx.has_visual_context or _has_explicit_visual_noun(t):
                return "deictic_visual_with_context"
            return "deictic_visual_needs_context"
    if any(p.search(t) for p in _VISUAL_PATTERNS):
        return "explicit_visual_request"
    if any(p.search(t) for p in _BARE_VISUAL_PATTERNS):
        return "bare_visual_request"
    return None


def _task_route(t: str, allowed: Dict[str, str], confidence: float) -> Optional[RouteDecision]:
    if any(w in t.lower() for w in _FORWARD_WORDS):
        return _decision(INTENT_TASK, CAP_FORWARD, {"question": t}, 0.86, "forward_task")
    if any(w in t for w in _TASK_VERBS) and len(t) >= 5:
        return _decision(INTENT_TASK, CAP_ASK_BRAIN, {"question": t},
                         0.78, "complex_task")
    return None


def _route_single(text: str, ctx: RouteContext,
                  detector_hit: Optional[Tuple[str, str, Dict[str, Any]]],
                  allowed: Dict[str, str]) -> RouteDecision:
    t = re.sub(r"\s+", "", str(text or "").strip().strip("。！？!?.,，、；;~～ "))
    if not t:
        return _unresolvable(text or "")

    # Semantic overrides must win over raw keyword hits.
    if _is_capability_inquiry(t):
        return _decision(INTENT_CHAT, CAP_CHAT, {"answer": "capability_inquiry"},
                         0.97, "capability_inquiry_not_vision")
    if _is_meta_chat(t):
        return _decision(INTENT_CHAT, CAP_CHAT, {"answer": "meta_chat"},
                         0.95, "meta_chat_not_vision")

    if detector_hit:
        capability, tool_name, args = detector_hit
        # 探测器标签（follow/robot/music/...）是能力分类；真正用于门控的是
        # 现有工具名。两者都要认，否则 memory/music 会被误降到 device_control。
        intent = {
            "self_behavior_set": INTENT_DEVICE,
            "set_voice": INTENT_DEVICE,
            "memory_add": INTENT_MEMORY,
            "play_music": INTENT_MUSIC,
            "local_fast": INTENT_LOCAL_FAST,
        }.get(tool_name, {
            "memory": INTENT_MEMORY,
            "music": INTENT_MUSIC,
            "local_fast": INTENT_LOCAL_FAST,
        }.get(capability, INTENT_DEVICE))
        params = dict(args)
        params.setdefault("tool", tool_name)
        return _decision(intent, tool_name, params, 0.95,
                         "authorized_detector_capability")

    # Vague requests are clarified before any capability is chosen.
    if _is_vague_action(t):
        return _clarify("vague_action", "你要我具体做什么？说清楚要动的设备或要看的内容，我再动手。")

    vreason = _vision_reason(t, ctx)
    if vreason == "bare_visual_request":
        return _clarify(vreason, "你想让我看什么？可以说“看看前面”“看看照片”或“看看摄像头画面”。", 0.58)
    if vreason == "deictic_visual_needs_context":
        return _clarify(vreason, "我没看到你指的东西。先让我看摄像头/照片，再说“看看这个是什么”。", 0.6)
    if vreason == "explicit_visual_request" or vreason == "deictic_visual_with_context":
        return _decision(INTENT_VISION, CAP_VISION, {"question": str(text).strip()},
                         0.93 if ctx.has_visual_context else 0.88, vreason)

    task = _task_route(t, allowed, 0.78)
    if task:
        return task

    # Ordinary conversation never reaches vision/device fast lanes.
    return _decision(INTENT_CHAT, CAP_CHAT, {"answer": "llm"},
                     0.9, "ordinary_chat")


def _compound_segments(text: str) -> List[str]:
    parts = [p.strip(" 。！？!?.,，、；;~～ ") for p in _COMPOUND_RE.split(str(text or ""))]
    return [p for p in parts if p]


def _compound_decision(text: str, ctx: RouteContext,
                       detectors: Optional[Sequence[Tuple[str, Callable[[str], Any]]]],
                       allowed: Dict[str, str]) -> Optional[RouteDecision]:
    parts = _compound_segments(text)
    if len(parts) < 2:
        return None
    decisions = []
    for part in parts:
        hit, _ = _run_detectors(part, detectors)
        decisions.append(_route_single(part, ctx, hit, allowed))
    actionable = [d for d in decisions if d.actionable]
    if len(actionable) < 2:
        return None
    labels = [_INTENT_LABELS.get(d.intent, d.capability) for d in actionable]
    question = "你说了两件事：" + "、".join(labels) + "。你想我先做哪个？"
    return _clarify("compound_request", question, 0.7)


def _priority_follow_off(text: str,
                         detectors: Optional[Sequence[Tuple[str, Callable[[str], Any]]]]
                         ) -> Optional[Dict[str, Any]]:
    """显式关闭跟随优先于复合澄清与助手模式转发。

    follow_request 有长度上限，复合表达整句可能匹配不到；对整句和每个分句
    都跑一遍现有探测器，只有产出 self_behavior_set(on=False) 才提升为安全快通道。
    """
    candidates = [str(text or "")] + _compound_segments(text)
    seen = set()
    for part in candidates:
        key = part.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        for _, fn in detectors or ():
            try:
                result = fn(part)
            except Exception:
                continue
            if not isinstance(result, (tuple, list)) or len(result) != 2:
                continue
            tool_name, args = str(result[0]), dict(result[1] or {})
            if tool_name == "self_behavior_set" and args.get("on") is False:
                params = dict(args)
                params.setdefault("tool", tool_name)
                return params
    return None


# ------------------------------------------------------------- model route IO
def parse_model_route(raw: Any,
                      allowed_capabilities: Optional[Iterable[str]] = None
                      ) -> Optional[RouteDecision]:
    """Parse a model-produced route JSON defensively.

    Accepts plain JSON or a fenced/embedded JSON object. Rejects arrays,
    unknown intents/capabilities, non-object parameters and out-of-range
    confidence. Returns None on every anomaly so callers can keep the
    deterministic decision.
    """
    if isinstance(raw, RouteDecision):
        return raw
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except Exception:
            return None
    if isinstance(raw, dict):
        obj = raw
    elif isinstance(raw, str):
        s = raw.strip()
        if not s:
            return None
        s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s, flags=re.I)
        if s.startswith("[") or s.endswith("]"):
            return None  # model must emit exactly one object, not an array
        candidates = [s]
        start, end = s.find("{"), s.rfind("}")
        if start >= 0 and end > start:
            candidates.append(s[start:end + 1])
        obj = None
        for candidate in candidates:
            try:
                parsed = json.loads(candidate)
            except Exception:
                continue
            if isinstance(parsed, dict):
                obj = parsed
                break
        if obj is None:
            return None
    else:
        return None

    intent = str(obj.get("intent") or obj.get("intent_type") or "").strip()
    aliases = {
        "device": INTENT_DEVICE, "device_control": INTENT_DEVICE,
        "vision": INTENT_VISION, "visual": INTENT_VISION,
        "chat": INTENT_CHAT, "normal_chat": INTENT_CHAT,
        "task": INTENT_TASK, "task_execution": INTENT_TASK,
        "memory": INTENT_MEMORY, "music": INTENT_MUSIC,
        "clarify": INTENT_CLARIFY, "unresolvable": INTENT_UNRESOLVABLE,
    }
    intent = aliases.get(intent.lower(), intent.lower())
    if intent not in ALL_INTENTS:
        return None

    capability = str(obj.get("capability") or obj.get("target") or "").strip()
    if not capability:
        return None
    allowed = set(allowed_capabilities or ())
    if allowed and capability not in allowed:
        return None

    params_raw = obj.get("parameters", obj.get("params", {}))
    if not isinstance(params_raw, dict):
        return None
    params = dict(params_raw)

    try:
        confidence = float(obj.get("confidence", 0.0))
    except (TypeError, ValueError):
        return None
    if not 0.0 <= confidence <= 1.0:
        return None

    if intent == INTENT_VISION and capability != CAP_VISION:
        return None
    if intent == INTENT_CHAT and capability != CAP_CHAT:
        return None
    if intent in (INTENT_CLARIFY, INTENT_UNRESOLVABLE) and capability != CAP_CLARIFY:
        return None
    if intent == INTENT_TASK and capability not in (CAP_ASK_BRAIN, CAP_FORWARD):
        return None
    if intent in (INTENT_DEVICE, INTENT_MEMORY, INTENT_MUSIC, INTENT_LOCAL_FAST):
        # Device/tool capabilities must have come from bridge detectors.
        if not allowed or capability not in allowed:
            return None

    clarification = str(obj.get("clarification") or "").strip()
    reason = str(obj.get("reason") or "model_route").strip()
    return _decision(intent, capability, params, confidence, reason,
                     clarification, source="model")


# ------------------------------------------------------------------ public API
def route_expression(text: str,
                     context: Optional[RouteContext] = None,
                     detectors: Optional[Sequence[Tuple[str, Callable[[str], Any]]]] = None,
                     model_route_fn: Optional[Callable[[str, RouteContext], Any]] = None,
                     allow_model: bool = True) -> RouteDecision:
    """Resolve one full user expression into a single executable decision."""
    ctx = context or RouteContext()
    raw = str(text or "")
    normalized = re.sub(r"\s+", "", raw.strip())
    if not normalized:
        return _unresolvable(raw)

    priority_follow_off = _priority_follow_off(raw, detectors)
    if priority_follow_off is not None:
        return _decision(INTENT_DEVICE, "self_behavior_set", priority_follow_off,
                         0.99, "priority_follow_off")

    hit, allowed = _run_detectors(raw, detectors)
    compound = _compound_decision(raw, ctx, detectors, allowed)
    if compound:
        decision = compound
    else:
        decision = _route_single(raw, ctx, hit, allowed)

    # Only ambiguous visual deictics may consult the existing LLM. Deterministic
    # safety/device/chat routes never need an extra model round trip.
    can_ask_model = (
        allow_model and model_route_fn is not None
        and decision.intent == INTENT_CLARIFY
        and decision.reason in ("bare_visual_request", "deictic_visual_needs_context")
        and (ctx.has_visual_context or len(normalized) >= 4)
    )
    if can_ask_model:
        model_allowed = set(allowed) | {CAP_VISION, CAP_CHAT, CAP_CLARIFY, CAP_ASK_BRAIN, CAP_FORWARD}
        try:
            model_decision = parse_model_route(model_route_fn(raw, ctx), model_allowed)
        except Exception:
            model_decision = None
        if model_decision and model_decision.confidence >= _MODEL_THRESHOLD:
            return model_decision
        print_reason = (model_decision.reason if model_decision else "invalid_model_route")
        return _decision(decision.intent, decision.capability, decision.parameters,
                         decision.confidence, decision.reason + "|" + print_reason,
                         decision.clarification, source="model_fallback")

    return decision


def build_route_hint(decision: RouteDecision) -> str:
    """Compact system hint for the existing Brain/LLM path; adds no capability."""
    def _safe(v: Any) -> str:
        return str(v).replace("<", "&lt;").replace(">", "&gt;")[:300]

    return (
        "\n<semantic_route>\n"
        "intent=" + _safe(decision.intent) + "\n"
        "capability=" + _safe(decision.capability) + "\n"
        "confidence=" + ("%.2f" % float(decision.confidence)) + "\n"
        "本轮语义路由只用于理解意图；不得调用未在本轮注册/授权的新工具。\n"
        "</semantic_route>"
    )
