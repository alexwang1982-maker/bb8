import hashlib
import json
import unicodedata
import uuid
import datetime
import httpx
import openai
from openai.types import CompletionUsage
from config.logger import setup_logging
from core.utils.util import check_model_key
from core.providers.llm.base import LLMProviderBase
from urllib.parse import urlparse

TAG = __name__
logger = setup_logging()

# 需要禁用思考模式的平台域名及其对应参数（默认关闭思考模式）
THINKING_DISABLED_DOMAINS = {
    "aliyuncs.com": {"enable_thinking": False},
    "deepseek.com": {"thinking": {"type": "disabled"}},
    "bigmodel.cn": {"thinking": {"type": "disabled"}},
    "moonshot.cn": {"thinking": {"type": "disabled"}},
    "volces.com": {"thinking": {"type": "disabled"}},
    "modelarts-maas.com": {"thinking": {"type": "disabled"}},
}

# 域名规则的模型名豁免表: 强制思考模型不接受该参数注入(如 MaaS glm-5.3 传 thinking 必 400)
THINKING_DISABLED_MODEL_EXCLUSIONS = {
    "modelarts-maas.com": ("glm-5.3",),
}

# V4.3 slice 1: shadow-only envelope carried through SDK extra_body.
SHADOW_ENVELOPE_VERSION = "shadow-1"


def _shadow_canonical(value):
    """Canonical view used only for shadow hashes; never mutates messages."""
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n"))
    if isinstance(value, dict):
        return {str(k): _shadow_canonical(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_shadow_canonical(v) for v in value]
    return value


def shadow_messages_hash(messages):
    canonical = json.dumps(
        _shadow_canonical(messages or []),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _build_shadow_envelope(session_id, messages):
    error = ""
    try:
        digest = shadow_messages_hash(messages)
    except Exception as exc:
        digest = ""
        error = type(exc).__name__
    issued = datetime.datetime.now(datetime.timezone.utc).isoformat(
        timespec="milliseconds").replace("+00:00", "Z")
    return {
        "envelope_version": SHADOW_ENVELOPE_VERSION,
        "request_id": "env_" + uuid.uuid4().hex[:16],
        "messages_hash": digest,
        "user_hash": hashlib.sha256(str(session_id or "").encode("utf-8", "replace")).hexdigest(),
        "issued_at": issued,
        "hash_error": error,
    }


class LLMProvider(LLMProviderBase):
    def __init__(self, config):
        self.model_name = config.get("model_name")
        # 可选实例级思考强度: off/disabled/none/disable | low/medium/high | 空/其他=按域名默认
        self.thinking_mode = str(config.get("thinking") or "").strip().lower()
        self.api_key = config.get("api_key")
        if "base_url" in config:
            self.base_url = config.get("base_url")
        else:
            self.base_url = config.get("url")
        
        timeout_config = config.get("timeout")
        if isinstance(timeout_config, dict):
            # 细粒度超时配置
            custom_timeout = httpx.Timeout(
                pool=timeout_config.get("pool", 2.0),
                connect=timeout_config.get("connect", 3.0),
                write=timeout_config.get("write", 5.0),
                read=timeout_config.get("read", 60.0)
            )
        elif isinstance(timeout_config, (int, float)) and timeout_config > 0:
            # 兼容旧的单一超时配置（整数或浮点数）
            custom_timeout = httpx.Timeout(timeout_config)
        else:
            # 未配置或配置无效，使用默认值
            custom_timeout = httpx.Timeout(300)

        param_defaults = {
            "max_tokens": int,
            "temperature": lambda x: round(float(x), 1),
            "top_p": lambda x: round(float(x), 1),
            "frequency_penalty": lambda x: round(float(x), 1),
        }

        for param, converter in param_defaults.items():
            value = config.get(param)
            try:
                setattr(
                    self,
                    param,
                    converter(value) if value not in (None, "") else None,
                )
            except (ValueError, TypeError):
                setattr(self, param, None)

        logger.debug(
            f"意图识别参数初始化: {self.temperature}, {self.max_tokens}, {self.top_p}, {self.frequency_penalty}"
        )

        model_key_msg = check_model_key("LLM", self.api_key)
        if model_key_msg:
            logger.bind(tag=TAG).error(model_key_msg)
        self.client = openai.OpenAI(api_key=self.api_key, base_url=self.base_url, timeout=custom_timeout)

    @staticmethod
    def normalize_dialogue(dialogue):
        """自动修复 dialogue 中缺失 content 的消息"""
        for msg in dialogue:
            if "role" in msg and "content" not in msg:
                msg["content"] = ""
        return dialogue

    def _apply_thinking_disabled(self, request_params: dict):
        """思考模式控制: 实例配置(thinking)优先, 否则按域名默认表"""
        mode = getattr(self, "thinking_mode", "")
        if mode in ("keep", "passthrough", "default"):
            # 不干预: 既不发 thinking 参数也不查域名表(适用于强制思考/拒绝该参数的模型, 如 MaaS glm-5.3)
            return
        if mode in ("off", "disabled", "none", "disable"):
            request_params.setdefault("extra_body", {}).update({"thinking": {"type": "disabled"}})
            logger.bind(tag=TAG).info("思考强度=off: 已禁用思考模式(实例配置)")
            return
        if mode in ("low", "medium", "high"):
            request_params.setdefault("extra_body", {}).update(
                {"reasoning_effort": mode, "thinking": {"type": "enabled"}})
            logger.bind(tag=TAG).info(f"思考强度={mode}: reasoning_effort 已设置(实例配置)")
            return
        parsed_url = urlparse(self.base_url)
        domain = parsed_url.netloc
        model_name = (self.model_name or "").lower()
        for disabled_domain, params in THINKING_DISABLED_DOMAINS.items():
            if disabled_domain not in domain:
                continue
            exclusions = THINKING_DISABLED_MODEL_EXCLUSIONS.get(disabled_domain, ())
            if any(ex in model_name for ex in exclusions):
                logger.bind(tag=TAG).info(
                    f"域名 {domain} 命中禁思考规则, 但模型 {self.model_name} 强制思考且拒绝该参数, 跳过注入")
                break
            request_params.setdefault("extra_body", {}).update(params)
            logger.bind(tag=TAG).info(f"为域名 {domain} 禁用思考模式，参数: {params}")
            break

    def _attach_shadow_envelope(self, request_params, session_id):
        """Attach a shadow Envelope without replacing existing extra_body params."""
        envelope = _build_shadow_envelope(session_id, request_params.get("messages"))
        extra = request_params.get("extra_body")
        if not isinstance(extra, dict):
            extra = {}
            request_params["extra_body"] = extra
        extra["context_envelope"] = envelope
        logger.bind(tag=TAG).debug(
            "context envelope shadow attached request_id=%s messages_hash=%s",
            envelope["request_id"],
            envelope["messages_hash"] or "-",
        )

    def response(self, session_id, dialogue, **kwargs):
        dialogue = self.normalize_dialogue(dialogue)

        request_params = {
            "model": self.model_name,
            "messages": dialogue,
            "stream": True,
            "user": session_id,
        }

        # 添加可选参数,只有当参数不为None时才添加
        optional_params = {
            "max_tokens": kwargs.get("max_tokens", self.max_tokens),
            "temperature": kwargs.get("temperature", self.temperature),
            "top_p": kwargs.get("top_p", self.top_p),
            "frequency_penalty": kwargs.get("frequency_penalty", self.frequency_penalty),
        }

        for key, value in optional_params.items():
            if value is not None:
                request_params[key] = value

        # 禁用思考模式
        self._apply_thinking_disabled(request_params)
        self._attach_shadow_envelope(request_params, session_id)

        responses = self.client.chat.completions.create(**request_params)

        is_active = True
        try:            
            for chunk in responses:
                try:
                    delta = chunk.choices[0].delta if getattr(chunk, "choices", None) else None
                    content = getattr(delta, "content", "") if delta else ""
                except IndexError:
                    content = ""
                if content:
                    if "<think>" in content:
                        is_active = False
                        content = content.split("<think>")[0]
                    if "</think>" in content:
                        is_active = True
                        content = content.split("</think>")[-1]
                    if is_active:
                        yield content
        finally:
            responses.close()

    def response_with_functions(self, session_id, dialogue, functions=None, **kwargs):
        dialogue = self.normalize_dialogue(dialogue)

        request_params = {
            "model": self.model_name,
            "messages": dialogue,
            "stream": True,
            "tools": functions,
            "user": session_id,
        }

        optional_params = {
            "max_tokens": kwargs.get("max_tokens", self.max_tokens),
            "temperature": kwargs.get("temperature", self.temperature),
            "top_p": kwargs.get("top_p", self.top_p),
            "frequency_penalty": kwargs.get("frequency_penalty", self.frequency_penalty),
        }

        for key, value in optional_params.items():
            if value is not None:
                request_params[key] = value

        # 禁用思考模式
        self._apply_thinking_disabled(request_params)
        self._attach_shadow_envelope(request_params, session_id)

        stream = self.client.chat.completions.create(**request_params)

        try:
            for chunk in stream:
                if getattr(chunk, "choices", None):
                    delta = chunk.choices[0].delta
                    content = getattr(delta, "content", "")
                    tool_calls = getattr(delta, "tool_calls", None)
                    yield content, tool_calls
                elif isinstance(getattr(chunk, "usage", None), CompletionUsage):
                    usage_info = getattr(chunk, "usage", None)
                    logger.bind(tag=TAG).info(
                        f"Token 消耗：输入 {getattr(usage_info, 'prompt_tokens', '未知')}，"
                        f"输出 {getattr(usage_info, 'completion_tokens', '未知')}，"
                        f"共计 {getattr(usage_info, 'total_tokens', '未知')}"
                    )
        finally:
            stream.close()
