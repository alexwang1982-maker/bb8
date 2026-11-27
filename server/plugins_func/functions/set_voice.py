from plugins_func.register import register_function, ToolType, ActionResponse, Action
from config.logger import setup_logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

TAG = __name__
logger = setup_logging()

# 常用 EdgeTTS 中文音色
VOICES = {
    "晓晓": "zh-CN-XiaoxiaoNeural",
    "女声": "zh-CN-XiaoxiaoNeural",
    "活泼": "zh-CN-XiaoyiNeural",
    "晓伊": "zh-CN-XiaoyiNeural",
    "男声": "zh-CN-YunxiNeural",
    "云希": "zh-CN-YunxiNeural",
    "浑厚": "zh-CN-YunjianNeural",
    "新闻": "zh-CN-YunyangNeural",
    "童声": "zh-CN-YunxiaNeural",
    "东北": "zh-CN-liaoning-XiaobeiNeural",
    "陕西": "zh-CN-shaanxi-XiaoniNeural",
    "台湾": "zh-TW-HsiaoChenNeural",
    "粤语": "zh-HK-HiuGaaiNeural",
}

set_voice_function_desc = {
    "type": "function",
    "function": {
        "name": "set_voice",
        "description": "切换助手说话的音色。可选：晓晓、活泼、男声、浑厚、新闻、童声、东北、陕西、台湾、粤语",
        "parameters": {
            "type": "object",
            "properties": {
                "voice": {"type": "string", "description": "要切换的音色名称"},
            },
            "required": ["voice"],
        },
    },
}


@register_function("set_voice", set_voice_function_desc, ToolType.SYSTEM_CTL)
def set_voice(conn: "ConnectionHandler", voice: str):
    """切换当前连接使用的 TTS 音色"""
    name = (voice or "").strip()
    code = VOICES.get(name)
    if not code and name.startswith("zh-"):
        code = name
    if not code:
        return ActionResponse(
            action=Action.RESPONSE,
            result="未找到音色",
            response="没有这个音色，可选：晓晓、活泼、男声、浑厚、新闻、童声、东北、陕西、台湾、粤语",
        )
    try:
        if conn.tts is None:
            return ActionResponse(action=Action.RESPONSE, result="tts not ready", response="语音还没准备好")
        conn.tts.voice = code
        logger.bind(tag=TAG).info(f"音色已切换: {name} -> {code}")
        return ActionResponse(action=Action.RESPONSE, result="ok", response=f"已换成{name}的声音")
    except Exception as e:
        logger.bind(tag=TAG).error(f"切换音色失败: {e}")
        return ActionResponse(action=Action.RESPONSE, result=str(e), response="切换音色失败了")
