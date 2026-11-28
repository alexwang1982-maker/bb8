"""BB-8 R1 voice safety regression tests (no hardware, no production service).

Run from the controlled work copy:
    python -B -m unittest tests.test_bb8_voice_safety_r1 -v

The WebSocket test starts an isolated in-process websockets server on an
ephemeral localhost port. It exercises the production ConnectionHandler route,
but uses a fake Opus decoder and fake ASR/VAD. It is not a human acoustic test.
"""
__test__ = False

import asyncio
import base64
import json
import logging
import sys
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Opus native library is unavailable in this environment. These tests never
# encode/decode real Opus; the fake decoder returns a fixed PCM frame.
if "opuslib_next" not in sys.modules:
    _opus = types.ModuleType("opuslib_next")

    class _FakeDecoder:
        def __init__(self, *args, **kwargs):
            pass

        def decode(self, packet, frame_size):
            return b"\x00" * 1920

    class _FakeEncoder:
        def __init__(self, *args, **kwargs):
            pass

    _opus.Decoder = _FakeDecoder
    _opus.Encoder = _FakeEncoder
    _opus.constants = types.SimpleNamespace(APPLICATION_AUDIO=0, SIGNAL_VOICE=0)
    sys.modules["opuslib_next"] = _opus

import websockets

from core.connection import ConnectionHandler
from core.providers.asr.base import ASRProviderBase, SOURCE_GENERATION_UNSET
from core.providers.asr.aliyun_stream import ASRProvider as AliyunStreamProvider
from core.providers.asr.aliyunbl_stream import ASRProvider as AliyunBlStreamProvider
from core.providers.asr.doubao_stream import ASRProvider as DoubaoStreamProvider
from core.providers.asr.xunfei_stream import ASRProvider as XunfeiStreamProvider
from core.providers.asr.dto.dto import InterfaceType
from core.utils.audio_source_gate import AudioSourceGate, unpack_gated_frame
from core.handle.abortHandle import handleAbortMessage
from core.handle.textHandler.listenMessageHandler import ListenTextMessageHandler
import core.providers.asr.base as asr_base
import core.handle.sendAudioHandle as send_audio_handle


class _FakeASR(ASRProviderBase):
    async def speech_to_text(self, *args, **kwargs):
        return ("", None)


class _Logger:
    def bind(self, **kwargs):
        return self

    def debug(self, *args):
        pass

    def info(self, *args):
        pass

    def warning(self, *args):
        pass

    def error(self, *args):
        pass


def make_conn():
    conn = object.__new__(ConnectionHandler)
    conn.config = {
        "voiceprint": {"barge_in": False, "barge_in_owner_ids": []},
        "tts_playback_timeout_seconds": 60,
    }
    conn.common_config = conn.config
    conn.logger = _Logger()
    conn.session_id = "r1-test"
    conn.client_is_speaking = False
    conn.client_listen_mode = "auto"
    conn.client_aec = False
    conn.client_abort = False
    conn.conn_from_mqtt_gateway = False
    conn.stop_event = threading.Event()
    conn.loop = None
    conn.bind_completed_event = asyncio.Event()
    conn.bind_completed_event.set()
    conn.need_bind = False
    conn.sentence_id = None
    conn.tts = None
    conn.report_queue = __import__("queue").Queue()
    conn.voiceprint_provider = None
    conn.tts_playback_active = False
    conn.tts_playback_token = None
    conn.tts_playback_started_at = 0.0
    conn._tts_watchdog_task = None
    conn.audio_gate = None  # set by init_audio_gate below
    init_audio_gate(conn)
    return conn


def init_audio_gate(conn):
    from core.utils.audio_source_gate import AudioSourceGate

    conn.audio_gate = AudioSourceGate()
    conn.asr_audio_queue = conn.audio_gate.normal_queue
    conn.barge_audio_queue = conn.audio_gate.barge_queue
    conn.current_audio_generation = None
    conn.asr_audio = []
    conn.client_audio_buffer = bytearray()
    conn.client_have_voice = False
    conn.client_voice_stop = False
    conn.client_voice_window = __import__("collections").deque(maxlen=5)
    conn.last_is_voice = False
    conn.vad_last_voice_time = 0.0
    conn._barge_buf = b""
    conn._barge_last_hit = 0


class TestAudioSourceGate(unittest.TestCase):
    def test_playback_frames_go_only_to_barge_queue(self):
        conn = make_conn()
        conn.begin_tts_playback("test_tts_start")
        route, item = conn.enqueue_microphone_audio(b"echo-frame", "websocket")

        self.assertEqual(route, "barge")
        self.assertEqual(conn.asr_audio_queue.qsize(), 0)
        self.assertEqual(conn.barge_audio_queue.qsize(), 1)
        generation, frame, source = unpack_gated_frame(item)
        self.assertEqual(frame, b"echo-frame")
        self.assertEqual(source, "websocket")
        self.assertTrue(conn.audio_gate.is_current(generation))
        self.assertTrue(conn.is_tts_playback_active())

    def test_normal_audio_flows_when_not_playing(self):
        conn = make_conn()
        route, _ = conn.enqueue_microphone_audio(b"user-frame", "websocket")
        self.assertEqual(route, "normal")
        self.assertEqual(conn.barge_audio_queue.qsize(), 0)
        self.assertEqual(conn.asr_audio_queue.qsize(), 1)

    def test_reset_drains_queues_and_invalidates_old_frames(self):
        conn = make_conn()
        _, old_item = conn.enqueue_microphone_audio(b"old-frame")
        old_generation, _, _ = unpack_gated_frame(old_item)
        conn.reset_audio_states()

        self.assertEqual(conn.asr_audio_queue.qsize(), 0)
        self.assertEqual(conn.barge_audio_queue.qsize(), 0)
        self.assertEqual(conn.asr_audio, [])
        self.assertEqual(conn._barge_buf, b"")
        self.assertFalse(conn.client_voice_stop)
        self.assertFalse(conn.audio_gate.is_current(old_generation))

    def test_receive_audio_handle_is_not_called_for_barge_frames(self):
        conn = make_conn()
        conn.begin_tts_playback("test_tts_start")
        _, item = conn.enqueue_microphone_audio(b"echo-frame")
        asr = _FakeASR()

        async def run():
            conn.loop = asyncio.get_running_loop()
            with patch.object(asr_base, "handleAudioMessage", new=AsyncMock()) as normal, patch.object(
                asr_base, "handleBargeAudioMessage", new=AsyncMock()
            ) as barge:
                # Production calls this from the ASR worker thread, not the loop.
                await asyncio.to_thread(asr._dispatch_audio_item, conn, item, True)
                normal.assert_not_called()
                barge.assert_awaited_once()

        asyncio.run(run())

    def test_stale_frames_are_not_dispatched(self):
        conn = make_conn()
        _, item = conn.enqueue_microphone_audio(b"old-frame")
        generation, _, _ = unpack_gated_frame(item)
        conn.reset_audio_states()
        asr = _FakeASR()

        async def run():
            conn.loop = asyncio.get_running_loop()
            with patch.object(asr_base, "handleAudioMessage", new=AsyncMock()) as normal:
                await asyncio.to_thread(asr._dispatch_audio_item, conn, item, False)
                normal.assert_not_called()

        asyncio.run(run())
        self.assertFalse(conn.audio_gate.is_current(generation))
        self.assertGreaterEqual(conn.audio_gate.counters["stale_dropped"], 1)


class TestPlaybackStateCleanup(unittest.TestCase):
    def test_stop_clears_speaking_even_when_send_fails(self):
        conn = make_conn()
        conn.begin_tts_playback("test_tts_start")
        conn.websocket = types.SimpleNamespace(send=AsyncMock(side_effect=OSError("closed")))

        async def run():
            with patch.object(send_audio_handle, "_wait_for_audio_completion", new=AsyncMock()):
                with self.assertRaises(OSError):
                    await send_audio_handle.send_tts_message(conn, "stop")

        asyncio.run(run())
        self.assertFalse(conn.client_is_speaking)
        self.assertFalse(conn.tts_playback_active)
        self.assertIsNone(conn.tts_playback_token)
        self.assertTrue(conn.audio_gate.normal_open)

    def test_stale_sentence_stop_does_not_clear_current_playback(self):
        conn = make_conn()
        current_token = conn.begin_tts_playback("first")
        conn.sentence_id = "sentence-0"
        conn.websocket = types.SimpleNamespace(send=AsyncMock())

        async def advance_sentence(_conn):
            # A newer chat started while this stop was waiting for playback drain.
            conn.sentence_id = "sentence-1"
            conn.tts_playback_token = "new-token"
            conn.tts_playback_active = True

        async def run():
            with patch.object(
                send_audio_handle,
                "_wait_for_audio_completion",
                new=AsyncMock(side_effect=advance_sentence),
            ):
                await send_audio_handle.send_tts_message(conn, "stop")

        asyncio.run(run())
        self.assertEqual(conn.tts_playback_token, "new-token")
        self.assertTrue(conn.is_tts_playback_active())


class TestAudioEntryAndAec(unittest.TestCase):
    def test_websocket_entry_gates_during_playback(self):
        conn = make_conn()
        conn.begin_tts_playback("test_tts_start")
        conn.vad = object()
        conn.asr = object()

        async def run():
            await conn._route_message(b"mock-opus")
            self.assertEqual(conn.asr_audio_queue.qsize(), 0)
            self.assertEqual(conn.barge_audio_queue.qsize(), 1)

        asyncio.run(run())

    def test_mqtt_aec_false_does_not_apply_aec(self):
        conn = make_conn()
        conn.conn_from_mqtt_gateway = True
        conn.vad = object()
        conn.asr = object()
        conn.client_aec = False
        conn._apply_aec = lambda timestamp, frame: self.fail("_apply_aec must not run")
        message = bytearray(16) + b"mock-opus"
        message[8:12] = (1234).to_bytes(4, "big")

        async def run():
            self.assertTrue(await conn._process_mqtt_audio_message(bytes(message)))

        asyncio.run(run())
        self.assertEqual(conn.asr_audio_queue.qsize(), 1)

    def test_mqtt_aec_true_with_valid_timestamp_applies_aec(self):
        conn = make_conn()
        conn.conn_from_mqtt_gateway = True
        conn.vad = object()
        conn.asr = object()
        conn.client_aec = True
        calls = []
        conn._apply_aec = lambda timestamp, frame: calls.append((timestamp, frame)) or b"aec-pcm"
        message = bytearray(16) + b"mock-opus"
        message[8:12] = (1234).to_bytes(4, "big")

        async def run():
            self.assertTrue(await conn._process_mqtt_audio_message(bytes(message)))

        asyncio.run(run())
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], 1234)
        self.assertEqual(conn.asr_audio_queue.qsize(), 1)

    def test_mqtt_audio_is_also_barge_gated_during_playback(self):
        conn = make_conn()
        conn.conn_from_mqtt_gateway = True
        conn.vad = object()
        conn.asr = object()
        conn.begin_tts_playback("test_tts_start")
        message = bytearray(16) + b"mock-opus"
        message[8:12] = (1234).to_bytes(4, "big")

        async def run():
            await conn._process_mqtt_audio_message(bytes(message))
            self.assertEqual(conn.asr_audio_queue.qsize(), 0)
            self.assertEqual(conn.barge_audio_queue.qsize(), 1)

        asyncio.run(run())


class TestListenStopGenerationLifecycle(unittest.IsolatedAsyncioTestCase):
    """listen stop 必须保留源 generation；新一轮 reset/abort 后旧结果失效。"""

    async def _gated_conn(self):
        conn = make_conn()
        conn.asr = _FakeASR()
        conn.asr.interface_type = InterfaceType.NON_STREAM
        _, item = conn.enqueue_microphone_audio(b"frame")
        generation, _, _ = unpack_gated_frame(item)
        conn.current_audio_generation = generation
        conn.asr_audio = [b"frame"]
        return conn, generation

    async def test_listen_stop_passes_source_generation_before_reset(self):
        conn, generation = await self._gated_conn()

        with patch.object(conn.asr, "handle_voice_stop", new=AsyncMock()) as hv:
            await ListenTextMessageHandler().handle(conn, {"state": "stop"})
            hv.assert_awaited_once()
            self.assertEqual(hv.await_args.kwargs["source_generation"], generation)

        # listen stop 是收尾，不是新一轮：generation 仍然有效。
        self.assertTrue(conn.audio_gate.is_current(generation))
        self.assertEqual(conn.asr_audio, [])

    async def test_new_round_during_asr_drops_brain_dispatch(self):
        conn, generation = await self._gated_conn()

        async def slow_asr(pcm_data, session_id):
            # 识别等待期间发生新一轮 reset/abort。
            conn.audio_gate.rotate("new_round_abort")
            return ("旧轮识别文本", None)

        with patch.object(conn.asr, "speech_to_text_wrapper", new=slow_asr), patch.object(
            conn.asr, "stop_ws_connection", new=lambda: None
        ), patch.object(asr_base, "enqueue_asr_report", new=lambda *a, **k: None), patch.object(
            asr_base, "startToChat", new=AsyncMock()
        ) as brain:
            await conn.asr.handle_voice_stop(conn, [b"frame"], source_generation=generation)
            brain.assert_not_awaited()

    async def test_old_voiceprint_enrollment_dropped_after_new_round(self):
        conn, generation = await self._gated_conn()
        enroll = AsyncMock(return_value=True)

        async def identify(wav, session_id):
            conn.audio_gate.rotate("new_round_abort")
            return "owner"

        conn.voiceprint_provider = types.SimpleNamespace(
            identify_speaker=AsyncMock(side_effect=identify),
            enroll_speaker=enroll,
            speaker_ids=["owner"],
        )

        async def asr_text(pcm_data, session_id):
            return ("记住我的声音", None)

        with patch.object(conn.asr, "speech_to_text_wrapper", new=asr_text), patch.object(
            conn.asr, "stop_ws_connection", new=lambda: None
        ), patch.object(asr_base, "startToChat", new=AsyncMock()) as brain:
            await conn.asr.handle_voice_stop(conn, [b"frame"], source_generation=generation)
            enroll.assert_not_awaited()
            brain.assert_not_awaited()

    async def test_none_generation_is_not_authorization(self):
        gate = AudioSourceGate()
        self.assertFalse(gate.is_current(None))

        conn, _ = await self._gated_conn()
        # 连接侧也没有 generation 时，解析结果必须是 None，而不是回退成授权。
        conn.current_audio_generation = None
        with patch.object(conn.asr, "speech_to_text_wrapper", new=AsyncMock()) as stt, patch.object(
            asr_base, "startToChat", new=AsyncMock()
        ) as brain:
            await conn.asr.handle_voice_stop(conn, [b"frame"], source_generation=None)
            stt.assert_not_awaited()
            brain.assert_not_awaited()

    async def test_explicit_none_rejected_even_when_connection_generation_valid(self):
        """旧回调明确传 None：即使连接侧 generation 有效也不得借用。"""
        conn, generation = await self._gated_conn()
        self.assertTrue(conn.audio_gate.is_current(generation))

        identify = AsyncMock(return_value="owner")
        enroll = AsyncMock(return_value=True)
        conn.voiceprint_provider = types.SimpleNamespace(
            identify_speaker=identify, enroll_speaker=enroll, speaker_ids=["owner"]
        )

        with patch.object(conn.asr, "speech_to_text_wrapper", new=AsyncMock()) as stt, patch.object(
            conn.asr, "stop_ws_connection", new=MagicMock()
        ) as stop_ws, patch.object(
            asr_base, "enqueue_asr_report", new=MagicMock()
        ) as report, patch.object(asr_base, "startToChat", new=AsyncMock()) as brain:
            await conn.asr.handle_voice_stop(conn, [b"frame"], source_generation=None)
            stt.assert_not_awaited()
            stop_ws.assert_not_called()
            report.assert_not_called()
            identify.assert_not_awaited()
            enroll.assert_not_awaited()
            brain.assert_not_awaited()

    async def test_stale_stream_captured_generation_rejected_after_new_round(self):
        """STREAM 会话捕获旧 generation，新轮次已有有效 generation 仍须拒绝。"""
        conn, old_generation = await self._gated_conn()
        # 模拟新一轮 reset/abort：generation 必须递增。
        conn.audio_gate.rotate("new_round")
        _, new_item = conn.enqueue_microphone_audio(b"new-round-frame")
        new_generation, _, _ = unpack_gated_frame(new_item)
        conn.current_audio_generation = new_generation
        self.assertTrue(conn.audio_gate.is_current(new_generation))
        self.assertNotEqual(old_generation, new_generation)

        identify = AsyncMock(return_value="owner")
        enroll = AsyncMock(return_value=True)
        conn.voiceprint_provider = types.SimpleNamespace(
            identify_speaker=identify, enroll_speaker=enroll, speaker_ids=["owner"]
        )

        with patch.object(conn.asr, "speech_to_text_wrapper", new=AsyncMock()) as stt, patch.object(
            conn.asr, "stop_ws_connection", new=MagicMock()
        ) as stop_ws, patch.object(
            asr_base, "enqueue_asr_report", new=MagicMock()
        ) as report, patch.object(asr_base, "startToChat", new=AsyncMock()) as brain:
            await conn.asr.handle_voice_stop(
                conn, [b"frame"], source_generation=old_generation
            )
            stt.assert_not_awaited()
            stop_ws.assert_not_called()
            report.assert_not_called()
            identify.assert_not_awaited()
            enroll.assert_not_awaited()
            brain.assert_not_awaited()


class TestAbortRoundIsolation(unittest.IsolatedAsyncioTestCase):
    """旧 Abort 不得清除新播放态，也不得重开新一轮音频入口。"""

    async def test_stale_abort_does_not_clear_new_playback(self):
        conn = make_conn()
        token1 = conn.begin_tts_playback("first")
        conn.sentence_id = "s1"

        async def start_new_round(payload):
            conn.begin_tts_playback("second")
            conn.sentence_id = "s2"

        conn.websocket = types.SimpleNamespace(send=AsyncMock(side_effect=start_new_round))
        await handleAbortMessage(conn, "stale")

        self.assertNotEqual(conn.tts_playback_token, token1)
        self.assertTrue(conn.is_tts_playback_active())
        self.assertFalse(conn.audio_gate.normal_open)

    async def test_abort_without_new_round_still_clears(self):
        conn = make_conn()
        conn.begin_tts_playback("first")
        conn.sentence_id = "s1"
        conn.websocket = types.SimpleNamespace(send=AsyncMock())

        await handleAbortMessage(conn, "normal")

        self.assertFalse(conn.is_tts_playback_active())
        self.assertTrue(conn.audio_gate.normal_open)


class TestGateAtomicity(unittest.TestCase):
    def test_rotate_holds_lock_for_whole_state_change(self):
        gate = AudioSourceGate()
        observed = []
        original = gate._rotate_locked

        def probe(reason, normal_open):
            observed.append(bool(gate._lock._is_owned()))
            return original(reason, normal_open)

        gate._rotate_locked = probe
        gate.rotate("atomic")

        self.assertEqual(observed, [True])
        self.assertEqual(gate.state()["reason"], "atomic")

    def test_concurrent_gate_changes_keep_generation_consistent(self):
        gate = AudioSourceGate()
        barrier = threading.Barrier(6)

        def worker(i):
            barrier.wait()
            for _ in range(300):
                if i % 3 == 0:
                    gate.rotate("r")
                elif i % 3 == 1:
                    gate.suppress("s")
                else:
                    gate.open("o")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        state = gate.state()
        self.assertEqual(state["generation"], 1800)
        self.assertEqual(state["counters"]["generations"], 1800)
        self.assertIn(state["reason"], {"r", "s", "o"})


class TestRealWebSocketRoute(unittest.IsolatedAsyncioTestCase):
    async def test_playback_frames_over_real_websocket_never_enter_asr_queue(self):
        conn = make_conn()
        conn.bind_completed_event.set()
        conn.vad = object()
        conn.asr = object()

        async def handler(ws):
            conn.websocket = ws
            conn.begin_tts_playback("real_ws_test")
            async for message in ws:
                await conn._route_message(message)

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
                await ws.send(b"mock-opus-frame")
                await asyncio.sleep(0.2)
        finally:
            server.close()
            await server.wait_closed()

        self.assertEqual(conn.asr_audio_queue.qsize(), 0)
        self.assertEqual(conn.barge_audio_queue.qsize(), 1)
        state = conn.audio_gate.state()
        self.assertEqual(state["counters"]["normal_enqueued"], 0)
        self.assertEqual(state["counters"]["barge_enqueued"], 1)


class _FakeResultWs:
    """最小 STREAM 结果连接：返回一条最终结果，随后模拟服务端关闭。"""

    def __init__(self, message, owner=None):
        self.message = message
        self.owner = owner
        self.recv_count = 0
        self.closed = False

    async def recv(self):
        self.recv_count += 1
        if self.recv_count > 1 and self.owner is not None:
            # doubao 自动模式在处理结果后不会自行 break；模拟 WS 关闭收口。
            self.owner.asr_ws = None
            raise websockets.ConnectionClosed(None, None)
        return self.message

    async def send(self, data):
        return None

    async def close(self):
        self.closed = True


class TestStreamGenerationTaskIsolation(unittest.IsolatedAsyncioTestCase):
    """旧 STREAM 结果任务创建后新轮次启动：两任务各自持有自己的 generation。"""

    def _bind_generation_recorder(self, provider):
        """记录任务传给 handle_voice_stop 的 generation，并走真实基类校验。"""
        calls = []

        async def record_and_run(conn, asr_audio_task, source_generation):
            calls.append(source_generation)
            return await ASRProviderBase.handle_voice_stop(
                provider, conn, asr_audio_task, source_generation=source_generation
            )

        provider.handle_voice_stop = record_and_run
        provider.speech_to_text_wrapper = AsyncMock(return_value=("新轮文本", None))
        provider.stop_ws_connection = MagicMock()
        return calls

    async def _run_concurrent_case(
        self, provider_cls, config, message, forward_name, parse_response=False
    ):
        conn = make_conn()
        old_provider = provider_cls(config, False)
        new_provider = provider_cls(config, False)

        _, old_item = conn.enqueue_microphone_audio(b"old-frame" * 16)
        old_generation, _, _ = unpack_gated_frame(old_item)
        conn.current_audio_generation = old_generation
        conn.asr_audio = [b"old-frame"] * 16
        old_provider.asr_ws = _FakeResultWs(message, old_provider)
        old_calls = self._bind_generation_recorder(old_provider)
        if parse_response:
            old_provider.parse_response = MagicMock(return_value=message)

        # 旧任务只创建 coroutine，不调度执行；此刻连接 generation 仍是旧值。
        old_task = getattr(old_provider, forward_name)(conn, old_generation)

        # 新会话启动并取得新 generation。
        conn.audio_gate.rotate("new_round")
        _, new_item = conn.enqueue_microphone_audio(b"new-frame" * 16)
        new_generation, _, _ = unpack_gated_frame(new_item)
        conn.current_audio_generation = new_generation
        conn.asr_audio = [b"new-frame"] * 16
        self.assertNotEqual(old_generation, new_generation)
        new_provider.asr_ws = _FakeResultWs(message, new_provider)
        new_calls = self._bind_generation_recorder(new_provider)
        if parse_response:
            new_provider.parse_response = MagicMock(return_value=message)

        with patch.object(asr_base, "enqueue_asr_report", new=MagicMock()), patch.object(
            asr_base, "startToChat", new=AsyncMock()
        ) as brain:
            # 旧任务此时才执行：它必须仍用旧 generation，且不得派发 Brain。
            await old_task
            self.assertEqual(old_calls, [old_generation])
            brain.assert_not_awaited()
            self.assertTrue(
                conn.audio_gate.is_current(new_generation),
                "旧任务 cleanup 不得 rotate/清空新会话 generation",
            )

            # 新任务用自己的 generation 正常工作。
            await getattr(new_provider, forward_name)(conn, new_generation)
            self.assertEqual(new_calls, [new_generation])
            brain.assert_awaited_once()
            self.assertEqual(brain.await_args.kwargs["source_generation"], new_generation)

    async def test_doubao_stream_task_keeps_own_generation(self):
        message = {
            "payload_msg": {
                "result": {
                    "text": "你好",
                    "utterances": [{"definite": True, "text": "你好"}],
                },
                "audio_info": {"duration": 100},
            }
        }
        await self._run_concurrent_case(
            DoubaoStreamProvider,
            {"appid": "test", "access_token": "test", "enable_multilingual": False},
            message,
            "_forward_asr_results",
            parse_response=True,
        )

    async def test_aliyun_stream_task_keeps_own_generation(self):
        message = json.dumps(
            {
                "header": {"name": "SentenceEnd", "status": 20000000},
                "payload": {"result": "你好"},
            }
        )
        await self._run_concurrent_case(
            AliyunStreamProvider,
            {"token": "test-token"},
            message,
            "_forward_results",
        )

    async def test_aliyunbl_stream_task_keeps_own_generation(self):
        message = json.dumps(
            {
                "header": {"event": "result-generated"},
                "payload": {
                    "output": {
                        "sentence": {
                            "text": "你好",
                            "sentence_end": True,
                            "end_time": 123,
                        }
                    }
                },
            }
        )
        await self._run_concurrent_case(
            AliyunBlStreamProvider, {}, message, "_forward_results"
        )

    async def test_xunfei_stream_task_keeps_own_generation(self):
        encoded = base64.b64encode(
            json.dumps(
                {"ws": [{"cw": [{"w": "你好"}]}]}, ensure_ascii=False
            ).encode("utf-8")
        ).decode("ascii")
        message = json.dumps(
            {
                "header": {"code": 0, "status": 2},
                "payload": {"result": {"text": encoded}},
            }
        )
        config = {"app_id": "test", "api_key": "test", "api_secret": "test"}
        await self._run_concurrent_case(
            XunfeiStreamProvider, config, message, "_forward_results"
        )

        # 覆写入口也必须把 generation 原样透传给基类。
        probe_conn = make_conn()
        _, probe_item = probe_conn.enqueue_microphone_audio(b"probe")
        probe_generation, _, _ = unpack_gated_frame(probe_item)
        probe = XunfeiStreamProvider(config, False)
        probe.asr_ws = None
        probe.is_processing = False
        with patch.object(ASRProviderBase, "handle_voice_stop", new=AsyncMock()) as base_hv:
            await probe.handle_voice_stop(
                probe_conn, [b"frame"], source_generation=probe_generation
            )
            base_hv.assert_awaited_once()
            self.assertEqual(
                base_hv.await_args.kwargs["source_generation"], probe_generation
            )


class _BlockingCloseWs:
    """可暂停在 close() 的 STREAM WebSocket，用于真实异步交错回归。"""

    def __init__(self, messages, close_gate=None, recv_gate=None, recv_gate_after=0):
        self._messages = list(messages)
        self.close_gate = close_gate
        self.recv_gate = recv_gate
        self.recv_gate_after = recv_gate_after
        self.close_started = asyncio.Event()
        self.closed = False

    async def recv(self):
        if self.recv_gate is not None and len(self._messages) <= self.recv_gate_after:
            await self.recv_gate.wait()
        if self._messages:
            return self._messages.pop(0)
        raise websockets.ConnectionClosed(None, None)

    async def send(self, data):
        return None

    async def close(self):
        self.closed = True
        self.close_started.set()
        if self.close_gate is not None:
            await self.close_gate.wait()


class TestStreamSessionResourceIsolation(unittest.IsolatedAsyncioTestCase):
    """同一 Provider 实例：旧会话 cleanup 不得关闭/清空新会话资源。"""

    @staticmethod
    def _release_for_new_session(provider):
        """模拟旧的同步停止路径：释放 processing，并使 asr_ws 变为空。"""
        provider.stop_ws_connection()
        if provider.asr_ws is not None:
            provider.asr_ws = None
        provider.is_processing = False

    async def _run_case(
        self, provider_cls, config, message, forward_name, parse_response=False
    ):
        conn = make_conn()
        provider = provider_cls(config, False)
        conn.asr = provider

        # 旧轮次：捕获 generation，并把旧任务绑定到它自己的 WebSocket。
        conn.audio_gate.rotate("old_round")
        _, old_item = conn.enqueue_microphone_audio(b"old-frame" * 16)
        old_generation, _, _ = unpack_gated_frame(old_item)
        conn.current_audio_generation = old_generation
        conn.asr_audio = [b"old-frame"] * 16

        old_close_gate = asyncio.Event()
        # 旧任务只需一条最终结果；再 recv 时抛 ConnectionClosed 进入 cleanup。
        old_ws = _BlockingCloseWs([message], close_gate=old_close_gate)
        provider.asr_ws = old_ws
        provider.is_processing = True
        provider.server_ready = False
        provider.forward_task = None
        old_task_id = "old-task"
        if hasattr(provider, "task_id"):
            provider.task_id = old_task_id
        if parse_response:
            provider.parse_response = MagicMock(return_value=message)

        calls = []

        async def handle_voice_stop(c, audio, source_generation=SOURCE_GENERATION_UNSET):
            calls.append(source_generation)
            if source_generation == old_generation:
                # 旧回调不得派发 Brain；同步停止让新会话可以启动。
                self._release_for_new_session(provider)
                return
            return await ASRProviderBase.handle_voice_stop(
                provider, c, audio, source_generation=source_generation
            )

        provider.handle_voice_stop = handle_voice_stop
        provider.speech_to_text_wrapper = AsyncMock(return_value=("新轮文本", None))

        new_recv_gate = asyncio.Event()
        # doubao 第一条用于初始化响应；所有 Provider 的结果 recv 都在此后暂停。
        new_ws = _BlockingCloseWs(
            [message, message] if parse_response else [message],
            recv_gate=new_recv_gate,
            recv_gate_after=1,
        )
        forward = getattr(provider, forward_name)
        forward_args = [conn, old_generation, old_ws]
        if forward_name == "_forward_results" and isinstance(
            provider, AliyunBlStreamProvider
        ):
            forward_args.append(old_task_id)

        with patch.object(
            websockets, "connect", new=AsyncMock(return_value=new_ws)
        ), patch.object(
            asr_base, "enqueue_asr_report", new=MagicMock()
        ), patch.object(
            asr_base, "startToChat", new=AsyncMock()
        ) as brain:
            old_task = asyncio.create_task(forward(*forward_args))
            # 旧任务已进入 finally，并暂停在 WebSocket close()。
            await asyncio.wait_for(old_ws.close_started.wait(), 2)
            self.assertIsNone(provider.asr_ws)
            self.assertFalse(provider.is_processing)

            # 新轮次在同一 Provider 实例上启动。
            conn.audio_gate.rotate("new_round")
            _, new_item = conn.enqueue_microphone_audio(b"new-frame" * 16)
            new_generation, _, _ = unpack_gated_frame(new_item)
            conn.current_audio_generation = new_generation
            conn.asr_audio = [b"new-frame"] * 16
            conn.client_voice_stop = False

            await provider.receive_audio(conn, b"new-frame", True)
            self.assertIs(provider.asr_ws, new_ws)
            self.assertTrue(provider.is_processing)
            self.assertIsNotNone(provider.forward_task)
            new_task = provider.forward_task
            self.assertFalse(new_task.done())

            # 释放旧 close：旧 cleanup 完成后不得触碰新会话资源。
            old_close_gate.set()
            await asyncio.wait_for(old_task, 2)
            self.assertTrue(old_ws.closed)
            self.assertIs(provider.asr_ws, new_ws)
            self.assertTrue(provider.is_processing)
            self.assertIs(provider.forward_task, new_task)
            self.assertFalse(new_ws.closed)
            self.assertEqual(calls, [old_generation])
            brain.assert_not_awaited()

            # 新任务仍能正常完成并派发 Brain。
            new_recv_gate.set()
            await asyncio.wait_for(new_task, 2)
            self.assertEqual(calls, [old_generation, new_generation])
            brain.assert_awaited_once()
            self.assertEqual(brain.await_args.kwargs["source_generation"], new_generation)

    async def test_doubao_old_cleanup_does_not_destroy_new_session(self):
        message = {
            "payload_msg": {
                "result": {
                    "text": "你好",
                    "utterances": [{"definite": True, "text": "你好"}],
                },
                "audio_info": {"duration": 100},
            }
        }
        await self._run_case(
            DoubaoStreamProvider,
            {"appid": "test", "access_token": "test", "enable_multilingual": False},
            message,
            "_forward_asr_results",
            parse_response=True,
        )

    async def test_aliyun_old_cleanup_does_not_destroy_new_session(self):
        message = json.dumps(
            {
                "header": {"name": "SentenceEnd", "status": 20000000},
                "payload": {"result": "你好"},
            }
        )
        await self._run_case(
            AliyunStreamProvider, {"token": "test-token"}, message, "_forward_results"
        )

    async def test_aliyunbl_old_cleanup_does_not_destroy_new_session(self):
        message = json.dumps(
            {
                "header": {"event": "result-generated"},
                "payload": {
                    "output": {
                        "sentence": {
                            "text": "你好",
                            "sentence_end": True,
                            "end_time": 123,
                        }
                    }
                },
            }
        )
        await self._run_case(AliyunBlStreamProvider, {}, message, "_forward_results")

    async def test_xunfei_old_cleanup_does_not_destroy_new_session(self):
        encoded = base64.b64encode(
            json.dumps(
                {"ws": [{"cw": [{"w": "你好"}]}]}, ensure_ascii=False
            ).encode("utf-8")
        ).decode("ascii")
        message = json.dumps(
            {
                "header": {"code": 0, "status": 2},
                "payload": {"result": {"text": encoded}},
            }
        )
        await self._run_case(
            XunfeiStreamProvider,
            {"app_id": "test", "api_key": "test", "api_secret": "test"},
            message,
            "_forward_results",
        )


if __name__ == "__main__":
    unittest.main()
