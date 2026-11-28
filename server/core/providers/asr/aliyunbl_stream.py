import json
import uuid
import asyncio
import websockets
from typing import List, TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

from config.logger import setup_logging
from core.providers.asr.base import ASRProviderBase
from core.providers.asr.dto.dto import InterfaceType

TAG = __name__
logger = setup_logging()


class ASRProvider(ASRProviderBase):
    def __init__(self, config, delete_audio_file):
        super().__init__()
        self.interface_type = InterfaceType.STREAM
        self.config = config
        self.text = ""
        self.asr_ws = None
        self.forward_task = None
        self.is_processing = False
        self.server_ready = False  # 服务器准备状态
        self.task_id = None  # 当前任务ID

        # 阿里百炼配置
        self.api_key = config.get("api_key")
        self.model = config.get("model", "paraformer-realtime-v2")
        self.sample_rate = config.get("sample_rate", 16000)
        self.format = config.get("format", "pcm")

        # 可选参数
        self.vocabulary_id = config.get("vocabulary_id")
        self.disfluency_removal_enabled = config.get("disfluency_removal_enabled", False)
        self.language_hints = config.get("language_hints")
        self.semantic_punctuation_enabled = config.get("semantic_punctuation_enabled", False)
        max_sentence_silence = config.get("max_sentence_silence")
        self.max_sentence_silence = int(max_sentence_silence) if max_sentence_silence else 200
        self.multi_threshold_mode_enabled = config.get("multi_threshold_mode_enabled", False)
        self.punctuation_prediction_enabled = config.get("punctuation_prediction_enabled", True)
        self.inverse_text_normalization_enabled = config.get("inverse_text_normalization_enabled", True)

        # WebSocket URL
        self.ws_url = "wss://dashscope.aliyuncs.com/api-ws/v1/inference"

        self.output_dir = config.get("output_dir", "./audio_output")
        self.delete_audio_file = delete_audio_file

    async def open_audio_channels(self, conn):
        await super().open_audio_channels(conn)

    async def receive_audio(self, conn, pcm_frame, audio_have_voice):
        # 先调用父类方法处理基础逻辑
        await super().receive_audio(conn, pcm_frame, audio_have_voice)

        # 只在有声音且没有连接时建立连接
        if audio_have_voice and not self.is_processing and not self.asr_ws:
            try:
                await self._start_recognition(conn)
            except Exception as e:
                logger.bind(tag=TAG).error(f"开始识别失败: {str(e)}")
                await self._cleanup(self.asr_ws)
                return

        # 发送音频数据
        if self.asr_ws and self.is_processing and self.server_ready:
            try:
                await self.asr_ws.send(pcm_frame)
            except Exception as e:
                logger.bind(tag=TAG).warning(f"发送音频失败: {str(e)}")
                await self._cleanup(self.asr_ws)

    async def _start_recognition(self, conn: "ConnectionHandler"):
        """开始识别会话"""
        # 本轮 generation 作为独立参数传给结果任务；任务不读连接当前值或共享属性。
        source_generation = getattr(conn, "current_audio_generation", None)
        session_ws = None
        session_task_id = None
        try:
            # 如果为手动模式,设置超时时长为最大值
            if conn.client_listen_mode == "manual":
                self.max_sentence_silence = 6000

            self.is_processing = True
            self.task_id = uuid.uuid4().hex

            # 建立WebSocket连接；会话 WS/task_id 是本轮独有。
            headers = {
                "Authorization": f"Bearer {self.api_key}"
            }

            logger.bind(tag=TAG).debug(f"正在连接阿里百炼ASR服务, task_id: {self.task_id}")

            session_ws = await websockets.connect(
                self.ws_url,
                additional_headers=headers,
                max_size=1000000000,
                ping_interval=None,
                ping_timeout=None,
                close_timeout=5,
            )
            self.asr_ws = session_ws
            session_task_id = self.task_id

            logger.bind(tag=TAG).debug("WebSocket连接建立成功")

            self.server_ready = False
            self.forward_task = asyncio.create_task(
                self._forward_results(conn, source_generation, session_ws, session_task_id)
            )

            # 发送run-task指令
            run_task_msg = self._build_run_task_message(session_task_id)
            await session_ws.send(json.dumps(run_task_msg, ensure_ascii=False))
            logger.bind(tag=TAG).debug("已发送run-task指令，等待服务器准备...")

        except Exception as e:
            logger.bind(tag=TAG).error(f"建立ASR连接失败: {str(e)}")
            if session_ws is not None:
                await session_ws.close()
            if self.asr_ws is session_ws or self.asr_ws is None:
                self.asr_ws = None
                self.is_processing = False
                self.task_id = None
            raise

    def _build_run_task_message(self, task_id=None) -> dict:
        """构建run-task指令；不借用可能已属于新会话的 self.task_id。"""
        task_id = self.task_id if task_id is None else task_id
        message = {
            "header": {
                "action": "run-task",
                "task_id": task_id,
                "streaming": "duplex"
            },
            "payload": {
                "task_group": "audio",
                "task": "asr",
                "function": "recognition",
                "model": self.model,
                "parameters": {
                    "format": self.format,
                    "sample_rate": self.sample_rate,
                    "disfluency_removal_enabled": self.disfluency_removal_enabled,
                    "semantic_punctuation_enabled": self.semantic_punctuation_enabled,
                    "max_sentence_silence": self.max_sentence_silence,
                    "multi_threshold_mode_enabled": self.multi_threshold_mode_enabled,
                    "punctuation_prediction_enabled": self.punctuation_prediction_enabled,
                    "inverse_text_normalization_enabled": self.inverse_text_normalization_enabled,
                },
                "input": {}
            }
        }

        # 只有当模型名称以v2结尾时才添加vocabulary_id参数
        if self.model.lower().endswith("v2"):
            message["payload"]["parameters"]["vocabulary_id"] = self.vocabulary_id

        if self.language_hints:
            message["payload"]["parameters"]["language_hints"] = self.language_hints

        return message

    async def _forward_results(
        self,
        conn: "ConnectionHandler",
        source_generation,
        session_ws=None,
        session_task_id=None,
    ):
        """结果任务只使用创建时传入的 generation / WebSocket / task_id。"""
        if session_ws is None:
            session_ws = self.asr_ws
        if session_task_id is None:
            session_task_id = self.task_id
        try:
            while not conn.stop_event.is_set():
                # 获取当前连接的音频数据
                audio_data = conn.asr_audio
                try:
                    response = await asyncio.wait_for(session_ws.recv(), timeout=1.0)
                    result = json.loads(response)

                    header = result.get("header", {})
                    payload = result.get("payload", {})
                    event = header.get("event", "")

                    # 处理task-started事件
                    if event == "task-started":
                        self.server_ready = True
                        logger.bind(tag=TAG).debug("服务器已准备，开始发送缓存音频...")

                        # 发送缓存音频
                        if conn.asr_audio:
                            for cached_pcm in conn.asr_audio[-10:]:
                                try:
                                    await session_ws.send(cached_pcm)
                                except Exception as e:
                                    logger.bind(tag=TAG).warning(f"发送缓存音频失败: {e}")
                                    break
                        continue

                    # 处理result-generated事件
                    elif event == "result-generated":
                        output = payload.get("output", {})
                        sentence = output.get("sentence", {})

                        text = sentence.get("text", "")
                        sentence_end = sentence.get("sentence_end", False)
                        end_time = sentence.get("end_time")

                        # 判断是否为最终结果(sentence_end为True且end_time不为null)
                        is_final = sentence_end and end_time is not None

                        if is_final:
                            logger.bind(tag=TAG).info(f"识别到文本: {text}")

                            # 手动模式下累积识别结果
                            if conn.client_listen_mode == "manual":
                                if self.text:
                                    self.text += text
                                else:
                                    self.text = text

                                # 手动模式下,只有在收到stop信号后才触发处理
                                if conn.client_voice_stop:
                                    logger.bind(tag=TAG).debug("收到最终识别结果，触发处理")
                                    await self.handle_voice_stop(conn, audio_data, source_generation=source_generation)
                                    break
                            else:
                                # 自动模式下直接覆盖
                                self.text = text
                                await self.handle_voice_stop(conn, audio_data, source_generation=source_generation)
                                break

                    # 处理task-finished事件
                    elif event == "task-finished":
                        logger.bind(tag=TAG).debug("任务已完成")
                        break

                    # 处理task-failed事件
                    elif event == "task-failed":
                        error_code = header.get("error_code", "UNKNOWN")
                        error_message = header.get("error_message", "未知错误")
                        logger.bind(tag=TAG).error(f"任务失败: {error_code} - {error_message}")
                        break

                except asyncio.TimeoutError:
                    continue
                except websockets.ConnectionClosed:
                    logger.bind(tag=TAG).info("ASR服务连接已关闭")
                    self.is_processing = False
                    break
                except Exception as e:
                    logger.bind(tag=TAG).error(f"处理结果失败: {str(e)}")
                    break

        except Exception as e:
            logger.bind(tag=TAG).error(f"结果转发失败: {str(e)}")
        finally:
            # 清理连接资源；generation 只存在于任务入参。旧会话 cleanup 不得
            # rotate/清空新会话的 generation，因此仅在本会话仍是当前轮次时收尾。
            await self._cleanup(session_ws, session_task_id)
            if conn.audio_gate.is_current(source_generation):
                conn.reset_audio_states()

    async def _send_stop_request(self):
        """发送停止请求(只作用于调用时所属会话)"""
        session_ws = self.asr_ws
        session_task_id = self.task_id
        if session_ws:
            try:
                # 先停止音频发送
                self.is_processing = False

                logger.bind(tag=TAG).debug("收到停止请求，发送finish-task指令")
                await self._send_finish_task(session_ws, session_task_id)
            except Exception as e:
                logger.bind(tag=TAG).error(f"发送停止请求失败: {e}")

    async def _send_finish_task(self, ws=None, task_id=None):
        """向指定会话发送 finish-task，不借用新会话的 WS/task_id。"""
        ws = self.asr_ws if ws is None else ws
        task_id = self.task_id if task_id is None else task_id
        if ws and task_id:
            try:
                finish_msg = {
                    "header": {
                        "action": "finish-task",
                        "task_id": task_id,
                        "streaming": "duplex"
                    },
                    "payload": {
                        "input": {}
                    }
                }
                await ws.send(json.dumps(finish_msg, ensure_ascii=False))
                logger.bind(tag=TAG).debug("已发送finish-task指令")
            except Exception as e:
                logger.bind(tag=TAG).error(f"发送finish-task指令失败: {e}")

    async def _cleanup(self, session_ws=None, session_task_id=None):
        """只关闭本会话的 WebSocket；不得触碰后来建立的新会话状态。"""
        if session_ws is None:
            session_ws = self.asr_ws
        if session_task_id is None:
            session_task_id = self.task_id

        if session_ws is not None:
            try:
                await self._send_finish_task(session_ws, session_task_id)
                await asyncio.sleep(0.1)
                await asyncio.wait_for(session_ws.close(), timeout=2.0)
            except Exception as e:
                logger.bind(tag=TAG).error(f"关闭WebSocket连接失败: {e}")

        # await close() 期间新会话可能已占用 Provider；收尾时重新确认归属。
        # 若 asr_ws 已被同步停止路径置空且尚无新会话，仍由本任务收尾。
        if self.asr_ws is session_ws or self.asr_ws is None:
            self.asr_ws = None
            self.is_processing = False
            self.server_ready = False
            self.forward_task = None
            self.task_id = None

        logger.bind(tag=TAG).debug("ASR会话清理完成")

    async def speech_to_text(self, opus_data, session_id, artifacts=None):
        """获取识别结果"""
        result = self.text
        self.text = ""
        return result, None

    async def close(self):
        """关闭资源"""
        await self._cleanup(self.asr_ws)