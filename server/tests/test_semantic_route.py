"""BB-8 semantic routing regression tests (no network, no hardware).

Run from the repository root with the Python 3.12 interpreter:
    python -B -m unittest discover -s server/tests -p test_semantic_route.py -v
"""
import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
BRIDGE_DIR = ROOT / "bridge"
for p in (str(ROOT), str(BRIDGE_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

import bridge  # noqa: E402
import semantic_router as semantic  # noqa: E402


def decision_dict(decision):
    return decision.to_dict()


class SemanticRouterTests(unittest.TestCase):
    def setUp(self):
        self.detectors = bridge.build_semantic_detectors("semantic-route-test")
        self.visual_ctx = semantic.RouteContext.make(
            recent_user_texts=("我给你看张照片",),
            has_visual_context=True,
        )
        self.plain_ctx = semantic.RouteContext()

    def route(self, text, ctx=None, model_fn=None):
        return semantic.route_expression(
            text, ctx or self.plain_ctx,
            detectors=self.detectors,
            model_route_fn=model_fn,
        )

    # 1. same keyword, different context
    def test_same_keyword_different_context(self):
        vision = self.route("你看看这个是什么", self.visual_ctx)
        self.assertEqual("vision", vision.intent)
        self.assertEqual("vision_observe", vision.capability)
        self.assertEqual("你看看这个是什么", vision.parameters["question"])
        self.assertGreaterEqual(vision.confidence, 0.8)

        chat = self.route("我看看你能做什么", self.visual_ctx)
        self.assertEqual("chat", chat.intent)
        self.assertEqual("chat_llm", chat.capability)
        self.assertNotEqual("vision", chat.intent)
        self.assertIn("capability_inquiry", chat.reason)

        no_ctx = self.route("你看看这个是什么", self.plain_ctx)
        self.assertEqual("clarify", no_ctx.intent)
        self.assertIn("没看到你指的东西", no_ctx.clarification)

    # 2. wrong-routing guards for device vs vision
    def test_explicit_vision_and_device_guards(self):
        cases = [
            ("你看看前面有什么", self.plain_ctx, "vision", "vision_observe"),
            ("打开眼睛灯", self.plain_ctx, "device_control", "self_eye_set"),
            ("摄像头的IP是多少", self.plain_ctx, "device_control", "self_camera_send"),
            ("摄像头识别结果是什么", self.plain_ctx, "device_control", "self_camera_latest"),
            ("看着我", self.plain_ctx, "device_control", "self_behavior_set"),
            ("别跟了", self.plain_ctx, "device_control", "self_behavior_set"),
            ("记住我叫小王", self.plain_ctx, "memory", "memory_add"),
            ("放首歌", self.plain_ctx, "music", "play_music"),
            ("你好", self.plain_ctx, "local_fast", "local_fast"),
        ]
        for text, ctx, intent, capability in cases:
            with self.subTest(text=text):
                d = self.route(text, ctx)
                self.assertEqual(intent, d.intent, decision_dict(d))
                self.assertEqual(capability, d.capability, decision_dict(d))

    # 3. colloquial ASR phrasing
    def test_colloquial_phrasing(self):
        for text in ("你瞅瞅前面是啥", "帮我拍张照看看桌上", "你看见啥了", "念一下屏幕上的字"):
            with self.subTest(text=text):
                d = self.route(text, self.plain_ctx)
                self.assertEqual("vision", d.intent, decision_dict(d))
                self.assertEqual("vision_observe", d.capability)

    # 4. compound and vague requests clarify instead of acting
    def test_compound_request_clarifies(self):
        d = self.route("先关灯，然后看看前面是什么", self.visual_ctx)
        self.assertEqual("clarify", d.intent)
        self.assertEqual("compound_request", d.reason)
        self.assertIn("两件事", d.clarification)

    def test_vague_requests_clarify(self):
        for text in ("你看看", "帮我看一下", "弄一下那个"):
            with self.subTest(text=text):
                d = self.route(text, self.visual_ctx)
                self.assertEqual("clarify", d.intent, decision_dict(d))
                self.assertTrue(d.clarification)

    # 5. task execution stays on existing Brain/forward capabilities
    def test_task_routes(self):
        brain = self.route("帮我建个文本文档", self.plain_ctx)
        self.assertEqual("task", brain.intent)
        self.assertEqual("ask_brain", brain.capability)

        fwd = self.route("让电脑助手改一下代码", self.plain_ctx)
        self.assertEqual("task", fwd.intent)
        self.assertEqual("forward_to_pc", fwd.capability)

    # 6. model-output anomalies cannot invent hardware capability
    def test_parse_model_route_valid_and_invalid(self):
        ok = semantic.parse_model_route(
            '```json\n{"intent":"vision","capability":"vision_observe",'
            '"parameters":{"question":"看看前面"},"confidence":0.91}\n```',
            allowed_capabilities={"vision_observe"},
        )
        self.assertIsNotNone(ok)
        self.assertEqual("vision", ok.intent)
        self.assertEqual("model", ok.source)

        anomalies = [
            '[{"intent":"chat","capability":"chat_llm","confidence":0.9}]',
            '{"intent":"vision","capability":"vision_observe","parameters":[],"confidence":0.9}',
            '{"intent":"vision","capability":"vision_observe","parameters":{},"confidence":9}',
            '{"intent":"device_control","capability":"self_motor_drive","confidence":0.99}',
            '{"intent":"unknown","capability":"chat_llm","confidence":0.99}',
            'not json at all',
        ]
        for raw in anomalies:
            with self.subTest(raw=raw[:40]):
                self.assertIsNone(semantic.parse_model_route(
                    raw, allowed_capabilities={"vision_observe", "chat_llm"}))

    def test_detector_phase_does_not_mutate_last_robot_direction(self):
        key = "semantic-detector-side-effect-test"
        bridge._last_robot[key] = "left"
        try:
            decision = semantic.route_expression(
                "转头", self.plain_ctx, detectors=bridge.build_semantic_detectors(key))
            self.assertEqual("local_fast", decision.capability)
            self.assertEqual("self_head_turn", decision.parameters["payload"][0])
            self.assertEqual("left", bridge._last_robot[key])
        finally:
            bridge._last_robot.pop(key, None)

    def test_model_route_cannot_grant_unauthorized_device_tool(self):
        def bad_model(text, ctx):
            return json.dumps({
                "intent": "device_control",
                "capability": "self_motor_drive",
                "parameters": {"speed": 100},
                "confidence": 0.99,
            }, ensure_ascii=False)

        d = self.route("看看", self.visual_ctx, model_fn=bad_model)
        self.assertEqual("clarify", d.intent)
        self.assertNotEqual("self_motor_drive", d.capability)
        self.assertIn("invalid_model_route", d.reason)

    def test_valid_model_route_can_disambiguate_visual_request(self):
        def good_model(text, ctx):
            return json.dumps({
                "intent": "vision",
                "capability": "vision_observe",
                "parameters": {"question": "看看前面有什么"},
                "confidence": 0.93,
            }, ensure_ascii=False)

        d = self.route("看看", self.visual_ctx, model_fn=good_model)
        self.assertEqual("vision", d.intent)
        self.assertEqual("model", d.source)
        self.assertEqual("看看前面有什么", d.parameters["question"])


class BridgeDispatchGateTests(unittest.TestCase):
    """Exercise the real Handler._handle_chat gate without network calls."""

    def make_handler(self):
        handler = object.__new__(bridge.Handler)
        handler._stream_started = False
        handler.sent = []
        handler._send_json = lambda obj, code=200: handler.sent.append(obj)
        return handler

    def test_capability_inquiry_never_calls_vision(self):
        handler = self.make_handler()
        body = {"messages": [{"role": "user", "content": "我看看你能做什么"}],
                "stream": False}
        with patch.object(bridge, "LLM_KEY", ""), \
                patch.object(bridge, "vision_observe",
                             side_effect=AssertionError("vision must not run")) as observe, \
                patch.object(bridge, "get_thread", return_value=("tid", False)), \
                patch.object(bridge, "run_turn", return_value=("ok", None)) as turn:
            handler._handle_chat(body)
        observe.assert_not_called()
        self.assertEqual("ok", handler.sent[-1]["choices"][0]["message"]["content"])
        self.assertIn("我看看你能做什么", turn.call_args.args[1])

    def test_assistant_mode_still_forwards_vague_expression(self):
        handler = self.make_handler()
        key = "semantic-assistant-mode-test"
        bridge._ASSISTANT_MODE[key] = time.time() + 60
        body = {"messages": [{"role": "user", "content": "弄一下那个"}],
                "stream": False}

        class NoopThread:
            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                pass

        try:
            with patch.dict(os.environ, {"OPENMUSE_FORWARD_TO_OPENCODE": "1"}), \
                    patch.object(bridge, "session_key", return_value=key), \
                    patch.object(bridge.threading, "Thread", NoopThread), \
                    patch.object(bridge, "LLM_KEY", ""):
                handler._handle_chat(body)
        finally:
            bridge._ASSISTANT_MODE.pop(key, None)
        content = handler.sent[-1]["choices"][0]["message"]["content"]
        self.assertIn("好，我让电脑助手处理：弄一下那个", content)

    def test_visual_question_uses_existing_vision_ability(self):
        handler = self.make_handler()
        messages = [
            {"role": "user", "content": "我给你看张照片"},
            {"role": "user", "content": "你看看这个是什么"},
        ]
        body = {"messages": messages, "stream": False}
        with patch.object(bridge, "LLM_KEY", ""), \
                patch.object(bridge, "vision_observe",
                             return_value=(True, "一个杯子")) as observe, \
                patch.object(bridge, "get_thread", return_value=("tid", False)), \
                patch.object(bridge, "run_turn", return_value=("ok", None)) as turn:
            handler._handle_chat(body)
        observe.assert_called_once_with("你看看这个是什么")
        context_block = turn.call_args.kwargs["context_block"]
        self.assertIn("<vision_observation>", context_block)
        self.assertIn("一个杯子", context_block)


if __name__ == "__main__":
    unittest.main()
