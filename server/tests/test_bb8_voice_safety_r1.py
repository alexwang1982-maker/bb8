"""BB-8 R1 voice safety regression tests (no hardware, no production service).

Run from the controlled work copy:
    python -B -m unittest tests.test_bb8_voice_safety_r1 -v

The WebSocket test starts an isolated in-process websockets server on an
ephemeral localhost port. It exercises the production ConnectionHandler route,
but uses a fake Opus decoder and fake ASR/VAD. It is not a human acoustic test.
"""
__test__ = False

import asyncio
import json
import logging
import sys
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

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
from core.providers.asr.base import ASRProviderBase
from core.utils.audio_source_gate import unpack_gated_frame
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


class TestVisionRouting(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(ROOT.parent / "bridge"))
        import bridge

        cls.bridge = bridge

    def test_three_capabilities_stay_separate(self):
        b = self.bridge
        self.assertEqual(b.vision_request("看看"), "看看")
        self.assertIsNotNone(b.camera_mgmt_request("摄像头切到人脸识别"))
        self.assertIsNotNone(b.camera_latest_request("摄像头识别到什么"))
        self.assertIsNone(b.camera_mgmt_request("看看"))
        self.assertIsNone(b.camera_latest_request("看看"))

    def test_vision_continuation_and_dedup(self):
        b = self.bridge
        b._vision_recent.clear()
        messages = [
            {"role": "user", "content": "看看"},
            {"role": "system", "content": b.build_vision_context("a cat")},
        ]
        self.assertIsNone(b.vision_request_allowed("看看", messages, "session-a"))

        fresh = [{"role": "user", "content": "看看"}]
        self.assertEqual(b.vision_request_allowed("看看", fresh, "session-b"), "看看")
        self.assertIsNone(b.vision_request_allowed("看看", fresh, "session-b"))


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


if __name__ == "__main__":
    unittest.main()
