import time
import json
import asyncio
import io
import wave
import urllib.request
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler
from core.utils.util import audio_to_data
from core.handle.abortHandle import handleAbortMessage
from core.handle.intentHandler import handle_user_intent
from core.utils.output_counter import check_device_output_limit
from core.handle.sendAudioHandle import send_stt_message, SentenceType

TAG = __name__

# ---------------- 声纹打断（barge-in）----------------
# 背景：设备说话时固件默认关麦，所以只有"喊唤醒词"能打断。
# 方案：固件说话时开麦（NoAudioCodecSimplex 是双 I2S，可同时收放），
#       服务器每 0.6s 取一段音频问本地声纹服务：
#         是主人(>阈值) → 发 abort，转为聆听；
#         是设备自己的 TTS(相似度 ~0.19) → 丢弃，不自打断。
# 开关：.config.yaml 顶层 voiceprint.barge_in: true
BARGE_WINDOW_SECS = 0.6
BARGE_BYTES = int(16000 * 2 * BARGE_WINDOW_SECS)


def _wav_bytes(pcm: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(pcm)
    return buf.getvalue()


def _post_voiceprint(url: str, speaker_ids: str, wav: bytes) -> dict:
    boundary = "----vpbarge"
    body = b""
    field = ("speaker_ids", speaker_ids)
    body += (
        "--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
        % (boundary, field[0], field[1])
    ).encode("utf-8")
    body += (
        "--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.wav\"\r\n"
        "Content-Type: audio/wav\r\n\r\n" % boundary
    ).encode("utf-8")
    body += wav + b"\r\n"
    body += ("--%s--\r\n" % boundary).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "multipart/form-data; boundary=" + boundary)
    with urllib.request.urlopen(req, timeout=4) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


async def _barge_in_is_owner(conn: "ConnectionHandler", pcm_frame: bytes) -> bool:
    """累计到 0.6s 再问一次声纹；是主人返回 True。"""
    vp = (conn.config or {}).get("voiceprint", {}) or {}
    url = vp.get("url")
    if not url:
        return False
    buf = getattr(conn, "_barge_buf", b"") + pcm_frame
    if len(buf) < BARGE_BYTES:
        conn._barge_buf = buf
        return False
    chunk, conn._barge_buf = buf[:BARGE_BYTES], b""

    speakers = vp.get("speakers") or []
    ids = ",".join(str(s).split(",")[0].strip() for s in speakers if s) or "wangjian"
    thr = float(vp.get("similarity_threshold", 0.35))

    def _probe():
        return _post_voiceprint(url, ids, _wav_bytes(chunk))

    try:
        data = await asyncio.to_thread(_probe)
    except Exception as exc:
        conn.logger.bind(tag=TAG).debug(f"声纹打断探测失败: {exc}")
        return False
    score = float(data.get("score") or 0.0)
    sid = data.get("speaker_id")
    if score >= thr:
        conn.logger.bind(tag=TAG).info(f"声纹打断命中: {sid} score={score:.3f}")
        return True
    conn.logger.bind(tag=TAG).debug(f"声纹打断丢弃: score={score:.3f}")
    return False


async def handleAudioMessage(conn: "ConnectionHandler", pcm_frame):
    # 不在说话状态就清掉打断探测的缓存
    if not conn.client_is_speaking:
        conn._barge_buf = b""
        conn._barge_last_hit = 0

    # 声纹打断：说话中，只认主人。
    # 用"上次命中时间"节流，不用一次性闭锁——闭锁在连续说话时会一直卡住，
    # 实测背诗那次就是被前两次命中锁死，导致后面完全打不断。
    now = time.time()
    last_hit = getattr(conn, "_barge_last_hit", 0) or 0
    if (
        conn.client_is_speaking
        and conn.client_listen_mode != "manual"
        and not conn.client_aec
        and (conn.config or {}).get("voiceprint", {}).get("barge_in", False)
        and (now - last_hit) > 2.0
    ):
        if await _barge_in_is_owner(conn, pcm_frame):
            conn._barge_last_hit = now
            conn._barge_buf = b""
            # 清掉已经攒进 ASR 的"机器人自己的声音"，否则打断后会拿它当用户输入
            try:
                conn.reset_audio_states()
            except Exception:
                pass
            await handleAbortMessage(conn, "barge_in")

    # 当前片段是否有人说话
    have_voice = conn.vad.is_vad(conn, pcm_frame)
    # 如果设备刚刚被唤醒，短暂忽略VAD检测
    if hasattr(conn, "just_woken_up") and conn.just_woken_up:
        have_voice = False
        # 设置一个短暂延迟后恢复VAD检测
        if not hasattr(conn, "vad_resume_task") or conn.vad_resume_task.done():
            conn.vad_resume_task = asyncio.create_task(resume_vad_detection(conn))
        return
    # 服务端AEC功能需要实时触发打断
    if conn.client_aec and have_voice:
        if conn.client_is_speaking and conn.client_listen_mode != "manual":
            await handleAbortMessage(conn)
    # 设备长时间空闲检测，用于say goodbye
    await no_voice_close_connect(conn, have_voice)
    # 接收音频
    await conn.asr.receive_audio(conn, pcm_frame, have_voice)


async def resume_vad_detection(conn: "ConnectionHandler"):
    # 等待2秒后恢复VAD检测
    await asyncio.sleep(2)
    conn.just_woken_up = False


async def startToChat(conn: "ConnectionHandler", text):
    # 检查输入是否是JSON格式（包含说话人信息）
    speaker_name = None
    actual_text = text

    try:
        # 尝试解析JSON格式的输入
        if text.strip().startswith("{") and text.strip().endswith("}"):
            data = json.loads(text)
            if "speaker" in data and "content" in data:
                speaker_name = data["speaker"]
                actual_content = data["content"]
                conn.logger.bind(tag=TAG).info(f"解析到说话人信息: {speaker_name}")

                # 仅在该说话人首次出现时保留 {"speaker":...} JSON，让模型自然称呼一次；
                # 后续轮降为纯文本，避免每轮重复出现名字诱导模型反复称呼
                if speaker_name not in conn.introduced_speakers:
                    conn.introduced_speakers.add(speaker_name)
                    actual_text = text
                else:
                    actual_text = actual_content
            elif "content" in data:
                # FunASR 等返回 {"content","language","emotion"}：只把识别正文交给 LLM，
                # 否则整段 JSON 会进入对话历史
                actual_text = data["content"]
    except (json.JSONDecodeError, KeyError):
        # 如果解析失败，继续使用原始文本
        pass

    # 保存说话人信息到连接对象
    if speaker_name:
        conn.current_speaker = speaker_name
    else:
        conn.current_speaker = None

    # 声纹过滤：可配置是否忽略"未知说话人"（环境音/别人说话）
    if speaker_name == "未知说话人":
        _vp = (conn.config or {}).get("voiceprint", {})
        if _vp.get("ignore_unknown", False):
            conn.logger.bind(tag=TAG).info("声纹=未知说话人，忽略本句（环境音过滤）")
            return

    if conn.need_bind:
        await check_bind_device(conn)
        return

    # 如果当日的输出字数大于限定的字数
    if conn.max_output_size > 0:
        if check_device_output_limit(
            conn.headers.get("device-id"), conn.max_output_size
        ):
            await max_out_size(conn)
            return

    # manual 模式下不打断正在播放的内容
    if conn.client_is_speaking and conn.client_listen_mode != "manual":
        await handleAbortMessage(conn)

    # 首先进行意图分析，使用实际文本内容
    intent_handled = await handle_user_intent(conn, actual_text)

    if intent_handled:
        # 如果意图已被处理，不再进行聊天
        return

    # 意图未被处理，继续常规聊天流程，使用实际文本内容
    await send_stt_message(conn, actual_text)

    # 准备开始新会话
    conn.client_abort = False

    conn.executor.submit(conn.chat, actual_text)


async def no_voice_close_connect(conn: "ConnectionHandler", have_voice):
    if have_voice:
        conn.last_activity_time = time.time() * 1000
        return
    # 只有在已经初始化过时间戳的情况下才进行超时检查
    if conn.last_activity_time > 0.0:
        no_voice_time = time.time() * 1000 - conn.last_activity_time
        close_connection_no_voice_time = int(
            conn.config.get("close_connection_no_voice_time", 120)
        )
        if (
            not conn.close_after_chat
            and no_voice_time > 1000 * close_connection_no_voice_time
        ):
            conn.close_after_chat = True
            conn.client_abort = False
            end_prompt = conn.config.get("end_prompt", {})
            if end_prompt and end_prompt.get("enable", True) is False:
                conn.logger.bind(tag=TAG).info("结束对话，无需发送结束提示语")
                await conn.close()
                return
            prompt = end_prompt.get("prompt")
            if not prompt:
                prompt = "请你以```时间过得真快```未来头，用富有感情、依依不舍的话来结束这场对话吧。！"
            await startToChat(conn, prompt)


async def max_out_size(conn: "ConnectionHandler"):
    # 播放超出最大输出字数的提示
    conn.client_abort = False
    text = "不好意思，我现在有点事情要忙，明天这个时候我们再聊，约好了哦！明天不见不散，拜拜！"
    await send_stt_message(conn, text)
    file_path = "config/assets/max_output_size.wav"
    opus_packets = await audio_to_data(file_path)
    conn.tts.tts_audio_queue.put((SentenceType.LAST, opus_packets, text))
    conn.close_after_chat = True


async def check_bind_device(conn: "ConnectionHandler"):
    if conn.bind_code:
        # 确保bind_code是6位数字
        if len(conn.bind_code) != 6:
            conn.logger.bind(tag=TAG).error(f"无效的绑定码格式: {conn.bind_code}")
            text = "绑定码格式错误，请检查配置。"
            await send_stt_message(conn, text)
            return

        text = f"请登录控制面板，输入{conn.bind_code}，绑定设备。"
        await send_stt_message(conn, text)

        # 播放提示音
        music_path = "config/assets/bind_code.wav"
        opus_packets = await audio_to_data(music_path)
        conn.tts.tts_audio_queue.put((SentenceType.FIRST, opus_packets, text))

        # 逐个播放数字
        for i in range(6):  # 确保只播放6位数字
            try:
                digit = conn.bind_code[i]
                num_path = f"config/assets/bind_code/{digit}.wav"
                num_packets = await audio_to_data(num_path)
                conn.tts.tts_audio_queue.put((SentenceType.MIDDLE, num_packets, None))
            except Exception as e:
                conn.logger.bind(tag=TAG).error(f"播放数字音频失败: {e}")
                continue
        conn.tts.tts_audio_queue.put((SentenceType.LAST, [], None))
    else:
        # 播放未绑定提示
        conn.client_abort = False
        text = f"没有找到该设备的版本信息，请正确配置 OTA地址，然后重新编译固件。"
        await send_stt_message(conn, text)
        music_path = "config/assets/bind_not_found.wav"
        opus_packets = await audio_to_data(music_path)
        conn.tts.tts_audio_queue.put((SentenceType.LAST, opus_packets, text))
