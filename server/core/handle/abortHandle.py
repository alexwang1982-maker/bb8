import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler
TAG = __name__


async def handleAbortMessage(conn: "ConnectionHandler", reason=""):
    conn.logger.bind(tag=TAG).info("Abort message received")
    # 设置成打断状态，会自动打断llm、tts任务
    conn.close_after_chat = False
    conn.client_abort = True
    # 记录被打断轮次的身份：stop 发送等待期间可能已有新轮次 TTS 启动，
    # 旧 Abort 绝不能清掉新播放态或重新开放新一轮的音频入口。
    aborted_token = getattr(conn, "tts_playback_token", None)
    aborted_sentence = getattr(conn, "sentence_id", None)
    # 先使旧音频generation失效并复位VAD，再清队列；防止消费线程竞态取到旧帧。
    conn.reset_audio_states()
    conn.clear_queues()
    # 打断客户端说话状态
    message = {"type": "tts", "state": "stop", "session_id": conn.session_id}
    if reason:
        # 声纹打断：让固件在 stop 时播一声"啵"，和唤醒音区分
        message["reason"] = reason
    await conn.websocket.send(json.dumps(message))
    if (
        getattr(conn, "tts_playback_token", None) == aborted_token
        and getattr(conn, "sentence_id", None) == aborted_sentence
    ):
        conn.clearSpeakStatus()
    conn.logger.bind(tag=TAG).info("Abort message received-end")
