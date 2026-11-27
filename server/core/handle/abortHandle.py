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
    conn.clear_queues()
    # 打断客户端说话状态
    message = {"type": "tts", "state": "stop", "session_id": conn.session_id}
    if reason:
        # 声纹打断：让固件在 stop 时播一声"啵"，和唤醒音区分
        message["reason"] = reason
    await conn.websocket.send(json.dumps(message))
    conn.clearSpeakStatus()
    conn.logger.bind(tag=TAG).info("Abort message received-end")
