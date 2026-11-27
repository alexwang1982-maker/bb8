"""BB-8 R7 本机安全测试入口回归测试（Fake MCP / Fake 连接）。

不连接真实设备、不打开串口、不启动生产服务、不执行电机动作。
__test__ = False：避免 conftest.py 的全局 asyncio.sleep/httpx mock 干扰时序断言。
Usage（必须从项目根目录以模块方式运行，避免 tests/core 包遮蔽项目 core 包）：
    python -B -m unittest tests.test_bb8_safe_test -v
"""
__test__ = False

import asyncio
import contextlib
import io
import json
import os
import secrets
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

# 直接以 unittest 运行时需自行加入项目根目录（绕过 conftest.py）。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 本机未安装 opuslib_next；本测试不会触达任何音频函数，
# 仅用空壳模块满足 core.utils.util 的顶层 import。
if "opuslib_next" not in sys.modules:
    sys.modules["opuslib_next"] = types.ModuleType("opuslib_next")

from aiohttp.test_utils import TestClient, TestServer

import core.bb8_safe_test as mod
from core.bb8_safe_test import (
    ALLOWED_DEVICE_ID,
    HEAD_TEST_PATH,
    HEAD_TURN_ARGS,
    HEAD_TURN_SUCCESS_TEXT,
    HEAD_TURN_TOOL,
    LISTEN_HOST,
    LISTEN_PORT,
    MCP_TIMEOUT_S,
    STATUS_PATH,
    STATUS_TOOL,
    TOKEN_HEADER,
    LocalToken,
    SafeTestRunner,
    register_active_connection,
)


def status_json(**overrides):
    value = {
        "left_limit": 0,
        "right_limit": 0,
        "center_limit": 1,
        "head_press": 0,
        "impact": 0,
        "head_motor": 0,
        "drive_motor": 0,
        "estop": 0,
        "impact_latched": 0,
        "drive_fault": 0,
        "head_fault": 0,
        "motion_inhibit_ms": 0,
        "follow_mode": 0,
        "safety_gen": 7,
    }
    value.update(overrides)
    return json.dumps(value, ensure_ascii=False)


CLEAN_BEFORE = status_json()
CLEAN_AFTER = status_json(center_limit=0)


class FakeMCPClient:
    def __init__(self):
        self.ready = True

    async def is_ready(self):
        return self.ready

    def has_tool(self, name):
        return name in (STATUS_TOOL, HEAD_TURN_TOOL)


class FakeWebSocket:
    def __init__(self):
        self.closed = False
        self.state = None


class FakeConn:
    def __init__(self):
        self.device_id = ALLOWED_DEVICE_ID
        self.websocket = FakeWebSocket()
        self.mcp_client = FakeMCPClient()


def fake_call_factory(status_texts, head_text):
    calls = []

    async def fake_call(conn, mcp_client, tool_name, args, timeout, include_raw):
        calls.append((tool_name, args, timeout, include_raw))
        if tool_name == STATUS_TOOL:
            text = status_texts.pop(0)
        elif tool_name == HEAD_TURN_TOOL:
            text = head_text
        else:
            raise AssertionError(f"unexpected tool: {tool_name}")
        raw = {"content": [{"type": "text", "text": text}]}
        return (text, raw) if include_raw else text

    return fake_call, calls


class Bb8SafeTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        mod._active_connections.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.lock_path = Path(self.tmp.name) / "executed.lock"
        lock_patch = patch.object(mod, "LOCK_PATH", self.lock_path)
        lock_patch.start()
        self.addCleanup(lock_patch.stop)

        self.token_value = "t-" + secrets.token_urlsafe(16)
        self.token = LocalToken(self.token_value)
        self.runner = SafeTestRunner()
        self.conn = FakeConn()
        await register_active_connection(ALLOWED_DEVICE_ID, self.conn)

    async def asyncTearDown(self):
        mod._active_connections.clear()

    # ---------------- 基础固定流程 ----------------

    async def test_happy_path_fixed_sequence_and_raw_values(self):
        fake_call, calls = fake_call_factory(
            [CLEAN_BEFORE, CLEAN_AFTER], HEAD_TURN_SUCCESS_TEXT
        )
        with patch.object(mod, "call_mcp_tool", new=fake_call):
            result = await self.runner.run_head_test()

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["code"], "ok")
        self.assertTrue(result["action_executed"])
        self.assertEqual(
            [(c[0], c[1]) for c in calls],
            [
                (STATUS_TOOL, "{}"),
                (HEAD_TURN_TOOL, dict(HEAD_TURN_ARGS)),
                (STATUS_TOOL, "{}"),
            ],
        )
        self.assertTrue(all(c[2] == MCP_TIMEOUT_S and c[3] is True for c in calls))
        self.assertEqual(result["before"]["parsed"]["center_limit"], 1)
        self.assertEqual(result["after"]["parsed"]["center_limit"], 0)
        self.assertEqual(
            result["head_turn"]["raw"]["content"][0]["text"],
            HEAD_TURN_SUCCESS_TEXT,
        )
        self.assertTrue(self.lock_path.exists())

    async def test_precondition_failure_never_dispatches_motor(self):
        bad_status = status_json(left_limit=1)
        fake_call, calls = fake_call_factory([bad_status], HEAD_TURN_SUCCESS_TEXT)
        with patch.object(mod, "call_mcp_tool", new=fake_call):
            result = await self.runner.run_head_test()

        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "precondition_failed")
        self.assertFalse(result["action_executed"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], STATUS_TOOL)
        self.assertFalse(self.lock_path.exists())

    async def test_missing_status_field_stops_before_motor(self):
        incomplete = json.loads(CLEAN_BEFORE)
        incomplete.pop("safety_gen")
        fake_call, calls = fake_call_factory(
            [json.dumps(incomplete)], HEAD_TURN_SUCCESS_TEXT
        )
        with patch.object(mod, "call_mcp_tool", new=fake_call):
            result = await self.runner.run_head_test()

        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "status_fields_missing")
        self.assertFalse(result["action_executed"])
        self.assertEqual(len(calls), 1)
        self.assertFalse(self.lock_path.exists())

    async def test_unknown_connection_stops_without_mcp_call(self):
        mod._active_connections.clear()
        fake_call, calls = fake_call_factory([CLEAN_BEFORE, CLEAN_AFTER], HEAD_TURN_SUCCESS_TEXT)
        with patch.object(mod, "call_mcp_tool", new=fake_call):
            result = await self.runner.run_head_test()

        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "connection_missing")
        self.assertFalse(result["action_executed"])
        self.assertEqual(calls, [])

    async def test_ambiguous_head_result_stops_without_second_motor_or_post_status(self):
        ambiguous = "failed: hit the LEFT limit after 12ms, motion short-braked"
        fake_call, calls = fake_call_factory([CLEAN_BEFORE], ambiguous)
        with patch.object(mod, "call_mcp_tool", new=fake_call):
            result = await self.runner.run_head_test()

        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "head_turn_ambiguous")
        self.assertTrue(result["action_executed"])
        self.assertEqual([c[0] for c in calls], [STATUS_TOOL, HEAD_TURN_TOOL])
        self.assertTrue(self.lock_path.exists())

    async def test_postcondition_fault_is_reported_without_retry(self):
        faulty_after = status_json(center_limit=0, head_fault=1)
        fake_call, calls = fake_call_factory(
            [CLEAN_BEFORE, faulty_after], HEAD_TURN_SUCCESS_TEXT
        )
        with patch.object(mod, "call_mcp_tool", new=fake_call):
            result = await self.runner.run_head_test()

        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "postcondition_failed")
        self.assertTrue(result["action_executed"])
        self.assertEqual([c[0] for c in calls], [STATUS_TOOL, HEAD_TURN_TOOL, STATUS_TOOL])
        self.assertTrue(self.lock_path.exists())

    # ---------------- 持久化单次执行锁 ----------------

    async def test_persistent_lock_blocks_new_runner_after_restart(self):
        fake_call, calls = fake_call_factory(
            [CLEAN_BEFORE, CLEAN_AFTER], HEAD_TURN_SUCCESS_TEXT
        )
        with patch.object(mod, "call_mcp_tool", new=fake_call):
            first = await self.runner.run_head_test()
            second = await SafeTestRunner().run_head_test()

        self.assertTrue(first["ok"])
        self.assertFalse(second["ok"])
        self.assertEqual(second["code"], "already_executed")
        self.assertEqual(len(calls), 3)
        self.assertTrue(self.lock_path.exists())

    async def test_persistent_lock_blocks_other_process(self):
        fake_call, _ = fake_call_factory(
            [CLEAN_BEFORE, CLEAN_AFTER], HEAD_TURN_SUCCESS_TEXT
        )
        with patch.object(mod, "call_mcp_tool", new=fake_call):
            await self.runner.run_head_test()

        root = str(Path(__file__).resolve().parents[1])
        code = (
            "import sys, pathlib, types\n"
            f"sys.path.insert(0, r'{root}')\n"
            "sys.modules.setdefault('opuslib_next', types.ModuleType('opuslib_next'))\n"
            "import core.bb8_safe_test as m\n"
            f"m.LOCK_PATH = pathlib.Path(r'{self.lock_path}')\n"
            "try:\n"
            "    m.acquire_execution_lock()\n"
            "    print('ACQUIRED')\n"
            "except m.SafeTestError as e:\n"
            "    print(e.code)\n"
        )
        proc = subprocess.run(
            [sys.executable, "-B", "-c", code],
            capture_output=True,
            text=True,
            cwd=root,
            timeout=60,
        )
        self.assertEqual(proc.stdout.strip(), "already_executed", proc.stderr)

    async def test_second_trigger_is_rejected_after_one_action(self):
        fake_call, calls = fake_call_factory(
            [CLEAN_BEFORE, CLEAN_AFTER, CLEAN_BEFORE, CLEAN_AFTER],
            HEAD_TURN_SUCCESS_TEXT,
        )
        with patch.object(mod, "call_mcp_tool", new=fake_call):
            first = await self.runner.run_head_test()
            second = await self.runner.run_head_test()

        self.assertTrue(first["ok"])
        self.assertFalse(second["ok"])
        self.assertEqual(second["code"], "already_executed")
        self.assertEqual(len(calls), 3)

    async def test_concurrent_trigger_is_rejected_as_busy(self):
        gate = asyncio.Event()
        head_started = asyncio.Event()
        calls = []

        async def fake_call(conn, mcp_client, tool_name, args, timeout, include_raw):
            calls.append((tool_name, args))
            if tool_name == HEAD_TURN_TOOL:
                head_started.set()
                await gate.wait()
                text = HEAD_TURN_SUCCESS_TEXT
            else:
                text = CLEAN_BEFORE if calls.count((STATUS_TOOL, args)) == 1 else CLEAN_AFTER
            raw = {"content": [{"type": "text", "text": text}]}
            return (text, raw) if include_raw else text

        with patch.object(mod, "call_mcp_tool", new=fake_call):
            first_task = asyncio.create_task(self.runner.run_head_test())
            await head_started.wait()
            second = await self.runner.run_head_test()
            gate.set()
            first = await first_task

        self.assertTrue(first["ok"], first)
        self.assertFalse(second["ok"])
        self.assertEqual(second["code"], "busy")
        self.assertEqual(sum(1 for c in calls if c[0] == HEAD_TURN_TOOL), 1)

    async def test_crash_before_dispatch_is_fail_closed(self):
        original = mod._create_lock_file
        calls = []

        def crash_after_lock():
            original()
            raise RuntimeError("simulated crash after lock write, before dispatch")

        async def fake_call(conn, mcp_client, tool_name, args, timeout, include_raw):
            calls.append(tool_name)
            return CLEAN_BEFORE, {}

        with patch.object(mod, "_create_lock_file", new=crash_after_lock), patch.object(
            mod, "call_mcp_tool", new=fake_call
        ):
            result = await self.runner.run_head_test()

        self.assertFalse(result["ok"])
        self.assertTrue(self.lock_path.exists())
        self.assertNotIn(HEAD_TURN_TOOL, calls)

    async def test_crash_after_dispatch_is_fail_closed(self):
        calls = []

        async def fake_call(conn, mcp_client, tool_name, args, timeout, include_raw):
            calls.append(tool_name)
            if tool_name == HEAD_TURN_TOOL:
                raise TimeoutError("simulated crash after dispatch")
            return CLEAN_BEFORE, {}

        with patch.object(mod, "call_mcp_tool", new=fake_call):
            result = await self.runner.run_head_test()

        self.assertFalse(result["ok"])
        self.assertTrue(result["action_executed"])
        self.assertTrue(self.lock_path.exists())
        self.assertEqual(calls, [STATUS_TOOL, HEAD_TURN_TOOL])

    async def test_lock_write_failure_prevents_dispatch(self):
        calls = []

        def fail_write():
            raise OSError("simulated disk failure")

        async def fake_call(conn, mcp_client, tool_name, args, timeout, include_raw):
            calls.append(tool_name)
            return CLEAN_BEFORE, {}

        with patch.object(mod, "_create_lock_file", new=fail_write), patch.object(
            mod, "call_mcp_tool", new=fake_call
        ):
            result = await self.runner.run_head_test()

        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "lock_write_failed")
        self.assertFalse(result["action_executed"])
        self.assertEqual(calls, [STATUS_TOOL])
        self.assertFalse(self.lock_path.exists())

    # ---------------- 连接生命周期 ----------------

    async def test_mcp_not_ready_removes_connection_without_mcp_call(self):
        self.conn.mcp_client.ready = False
        fake_call, calls = fake_call_factory([CLEAN_BEFORE], HEAD_TURN_SUCCESS_TEXT)
        with patch.object(mod, "call_mcp_tool", new=fake_call):
            result = await self.runner.run_head_test()

        self.assertFalse(result["ok"])
        self.assertFalse(result["action_executed"])
        self.assertEqual(calls, [])
        self.assertEqual(mod._active_connections, {})

    async def test_identity_mismatch_removes_connection_without_mcp_call(self):
        self.conn.device_id = "aa:bb:cc:dd:ee:ff"
        fake_call, calls = fake_call_factory([CLEAN_BEFORE], HEAD_TURN_SUCCESS_TEXT)
        with patch.object(mod, "call_mcp_tool", new=fake_call):
            result = await self.runner.status()

        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "connection_missing")
        self.assertEqual(calls, [])
        self.assertEqual(mod._active_connections, {})

    async def test_closed_connection_removes_connection_without_mcp_call(self):
        self.conn.websocket.closed = True
        fake_call, calls = fake_call_factory([CLEAN_BEFORE], HEAD_TURN_SUCCESS_TEXT)
        with patch.object(mod, "call_mcp_tool", new=fake_call):
            result = await self.runner.status()

        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "connection_missing")
        self.assertEqual(calls, [])
        self.assertEqual(mod._active_connections, {})

    # ---------------- 令牌鉴权与 HTTP 表面 ----------------

    async def test_valid_token_passes_and_invalid_token_rejected(self):
        app = mod.create_app(SafeTestRunner(), token=self.token)
        server = TestServer(app)
        client = TestClient(server)
        await client.start_server()
        try:
            no_header = await client.post(HEAD_TEST_PATH)
            self.assertEqual(no_header.status, 401)
            self.assertEqual((await no_header.json())["code"], "unauthorized")

            wrong = await client.post(
                HEAD_TEST_PATH, headers={TOKEN_HEADER: self.token_value + "x"}
            )
            self.assertEqual(wrong.status, 401)

            fake_call, calls = fake_call_factory(
                [CLEAN_BEFORE, CLEAN_AFTER], HEAD_TURN_SUCCESS_TEXT
            )
            with patch.object(mod, "call_mcp_tool", new=fake_call):
                ok = await client.post(
                    HEAD_TEST_PATH, headers={TOKEN_HEADER: self.token_value}
                )
                body = await ok.json()

            self.assertEqual(ok.status, 200)
            self.assertTrue(body["ok"])
            self.assertEqual(len(calls), 3)
        finally:
            await client.close()

    async def test_token_absent_from_logs_url_and_responses(self):
        app = mod.create_app(SafeTestRunner(), token=self.token)
        server = TestServer(app)
        client = TestClient(server)
        out, err = io.StringIO(), io.StringIO()
        await client.start_server()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                bad = await client.post(
                    HEAD_TEST_PATH, headers={TOKEN_HEADER: "wrong-token"}
                )
                bad_body = await bad.text()
                good = await client.get(STATUS_PATH)
                good_body = await good.text()
        finally:
            await client.close()

        self.assertNotIn(self.token_value, bad_body)
        self.assertNotIn(self.token_value, good_body)
        self.assertNotIn(self.token_value, out.getvalue())
        self.assertNotIn(self.token_value, err.getvalue())
        self.assertNotIn(self.token_value, str(app.router.routes()))

    async def test_get_requires_no_token_and_never_dispatches_action(self):
        app = mod.create_app(SafeTestRunner(), token=self.token)
        server = TestServer(app)
        client = TestClient(server)
        await client.start_server()
        try:
            fake_call, calls = fake_call_factory([CLEAN_BEFORE], HEAD_TURN_SUCCESS_TEXT)
            with patch.object(mod, "call_mcp_tool", new=fake_call):
                resp = await client.get(STATUS_PATH)
                body = await resp.json()

            self.assertEqual(resp.status, 200)
            self.assertTrue(body["ok"])
            self.assertEqual([c[0] for c in calls], [STATUS_TOOL])
            self.assertFalse(self.lock_path.exists())
        finally:
            await client.close()

    async def test_listener_is_loopback_only_on_fixed_port(self):
        self.assertEqual(LISTEN_HOST, "127.0.0.1")
        self.assertEqual(LISTEN_PORT, 18083)
        self.assertNotIn("0.0.0.0", (LISTEN_HOST,))

    async def test_remote_request_is_rejected_by_guard(self):
        class FakeRequest:
            remote = "192.168.1.7"

        response = mod._loopback_guard(FakeRequest())
        self.assertIsNotNone(response)
        self.assertEqual(response.status, 403)

    async def test_http_app_exposes_only_status_get_and_fixed_head_post(self):
        fake_call, calls = fake_call_factory(
            [CLEAN_BEFORE, CLEAN_BEFORE, CLEAN_AFTER], HEAD_TURN_SUCCESS_TEXT
        )
        runner = SafeTestRunner()
        app = mod.create_app(runner, token=self.token)
        self.assertEqual(len(list(app.router.routes())), 2)

        server = TestServer(app)
        client = TestClient(server)
        await client.start_server()
        try:
            with patch.object(mod, "call_mcp_tool", new=fake_call):
                status_resp = await client.get(STATUS_PATH)
                status_body = await status_resp.json()
                reject_resp = await client.post(
                    HEAD_TEST_PATH,
                    data="direction=right",
                    headers={TOKEN_HEADER: self.token_value},
                )
                reject_body = await reject_resp.json()
                post_resp = await client.post(
                    HEAD_TEST_PATH, headers={TOKEN_HEADER: self.token_value}
                )
                post_body = await post_resp.json()

            self.assertEqual(status_resp.status, 200)
            self.assertTrue(status_body["ok"])
            self.assertEqual(reject_resp.status, 400)
            self.assertEqual(reject_body["code"], "unexpected_body")
            self.assertEqual(post_resp.status, 200)
            self.assertTrue(post_body["ok"])
            self.assertEqual(
                [c[0] for c in calls],
                [STATUS_TOOL, STATUS_TOOL, HEAD_TURN_TOOL, STATUS_TOOL],
            )
        finally:
            await client.close()


class TokenFileLifecycleTest(unittest.IsolatedAsyncioTestCase):
    """令牌目录 ACL / 首次创建 / 重复启动 / 原子替换 / 异常清理的隔离测试。

    全部重定向到临时目录；Windows 上调用真实 icacls，不使用权限 mock。
    失败路径绝不绑定 127.0.0.1:18083，绝不启动生产服务。
    """

    async def asyncSetUp(self):
        self.tmpdir = tempfile.TemporaryDirectory(prefix="bb8-token-")
        self.addCleanup(self.tmpdir.cleanup)
        self.data_dir = Path(self.tmpdir.name) / "bb8-r7"
        self.token_path = self.data_dir / "post.token"
        p_data = patch.object(mod, "DATA_DIR", self.data_dir)
        p_path = patch.object(mod, "TOKEN_PATH", self.token_path)
        p_data.start()
        p_path.start()
        self.addCleanup(p_data.stop)
        self.addCleanup(p_path.stop)

    def _temp_files(self):
        return list(self.data_dir.glob(".post.token.*.tmp"))

    @staticmethod
    def _icacls(path):
        proc = subprocess.run(["icacls", str(path)], capture_output=True)
        return (proc.stdout + proc.stderr).decode("mbcs", errors="replace")

    @staticmethod
    def _principal():
        domain = os.environ.get("USERDOMAIN") or os.environ.get("COMPUTERNAME") or ""
        user = os.environ.get("USERNAME") or ""
        return f"{domain}\\{user}" if domain else user

    async def test_real_windows_data_dir_acl_allows_replace_and_cleanup(self):
        if os.name != "nt":
            self.skipTest("Windows ACL 测试仅在 Windows 运行")
        mod.ensure_data_dir_acl(self.data_dir)
        acl = self._icacls(self.data_dir)
        self.assertIn("(OI)(CI)(F)", acl)
        self.assertIn(self._principal(), acl)
        self.assertNotIn("(I)", acl, "目录 ACL 必须关闭继承")
        for forbidden in ("BUILTIN\\", "NT AUTHORITY\\", "Everyone", "Users"):
            self.assertNotIn(forbidden, acl)

        mod.load_or_create_token()
        final_acl = self._icacls(self.token_path)
        self.assertIn("(F)", final_acl)
        self.assertNotIn(":(R)", final_acl, "令牌文件不得只有只读权限")
        self.assertEqual(self._temp_files(), [])

    async def test_first_creation_does_not_harden_temp_to_readonly(self):
        self.assertFalse(
            hasattr(mod, "_harden_token_file"),
            "不得再对临时令牌文件单独收紧为 (R)",
        )
        marker = "F" * 43
        with patch.object(mod.secrets, "token_urlsafe", return_value=marker):
            token = mod.load_or_create_token()

        self.assertEqual(token._value, marker)
        self.assertTrue(self.token_path.exists())
        self.assertEqual(self.token_path.read_text(encoding="utf-8"), marker)
        self.assertEqual(len(token._value), 43)
        self.assertEqual(self._temp_files(), [])

    async def test_repeated_startup_replaces_token_atomically(self):
        first, second = "A" * 43, "B" * 43
        with patch.object(mod.secrets, "token_urlsafe", side_effect=[first, second]):
            t1 = mod.load_or_create_token()
            t2 = mod.load_or_create_token()

        self.assertEqual(t1._value, first)
        self.assertEqual(t2._value, second)
        self.assertEqual(self.token_path.read_text(encoding="utf-8"), second)
        self.assertEqual(self._temp_files(), [])

    async def test_cleanup_failure_reports_clearly(self):
        with patch.object(
            Path, "unlink", side_effect=OSError("simulated unlink failure")
        ), patch.object(mod.os, "replace", side_effect=OSError("simulated replace failure")):
            with self.assertRaises(RuntimeError) as ctx:
                mod.load_or_create_token()

        self.assertIn("临时令牌文件清理失败", str(ctx.exception))
        self.assertIn("禁止启动监听", str(ctx.exception))
        self.assertIsInstance(ctx.exception.__cause__, OSError)

    async def test_replace_failure_removes_temp_token_file(self):
        with patch.object(
            mod.os, "replace", side_effect=OSError("simulated replace failure")
        ):
            with self.assertRaises(OSError):
                mod.load_or_create_token()

        self.assertEqual(self._temp_files(), [])
        self.assertFalse(self.token_path.exists())

    async def test_fsync_failure_removes_temp_token_file(self):
        with patch.object(mod.os, "fsync", side_effect=OSError("simulated fsync failure")):
            with self.assertRaises(OSError):
                mod.load_or_create_token()

        self.assertEqual(self._temp_files(), [])
        self.assertFalse(self.token_path.exists())

    async def test_listener_not_started_when_replace_fails(self):
        with patch.object(
            mod.os, "replace", side_effect=OSError("simulated replace failure")
        ):
            server = mod.LoopbackSafeTestServer()
            with self.assertRaises(OSError):
                await server.start()

        self.assertIsNone(server._site)
        self.assertIsNone(server._app_runner)
        self.assertIsNone(server._purge_task)
        self.assertEqual(self._temp_files(), [])

    async def test_listener_not_started_when_cleanup_fails(self):
        with patch.object(
            Path, "unlink", side_effect=OSError("simulated unlink failure")
        ), patch.object(mod.os, "replace", side_effect=OSError("simulated replace failure")):
            server = mod.LoopbackSafeTestServer()
            with self.assertRaises(RuntimeError) as ctx:
                await server.start()

        self.assertIn("禁止启动监听", str(ctx.exception))
        self.assertIsNone(server._site)
        self.assertIsNone(server._app_runner)

    async def test_open_fileexists_preserves_preexisting_temp_file(self):
        sentinel = "SENTINEL-DO-NOT-DELETE-9f2c"
        tmp = self.data_dir / f".post.token.{os.getpid()}.tmp"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        tmp.write_text(sentinel, encoding="utf-8")

        with self.assertRaises(FileExistsError):
            mod.load_or_create_token()

        self.assertTrue(tmp.exists(), "O_EXCL 失败不得删除已存在的临时文件")
        self.assertEqual(tmp.read_text(encoding="utf-8"), sentinel)
        self.assertFalse(self.token_path.exists())
        self.assertEqual(self._temp_files(), [tmp])

    async def test_open_fileexists_does_not_poison_later_success(self):
        tmp = self.data_dir / f".post.token.{os.getpid()}.tmp"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        tmp.write_text("SENTINEL", encoding="utf-8")

        with self.assertRaises(FileExistsError):
            mod.load_or_create_token()

        tmp.unlink()
        token = mod.load_or_create_token()

        self.assertTrue(self.token_path.exists())
        self.assertEqual(self.token_path.read_text(encoding="utf-8"), token._value)
        self.assertEqual(self._temp_files(), [])


class AppStartupWiringTest(unittest.TestCase):
    """只读检查 app.py 启动接线：必须 await、异常传播、成功日志后置。"""

    @staticmethod
    def _source():
        path = Path(__file__).resolve().parents[1] / "app.py"
        return path.read_text(encoding="utf-8")

    def test_safe_interface_is_awaited_before_main_services(self):
        source = self._source()
        self.assertNotIn("asyncio.create_task(bb8_safe_server.start())", source)
        self.assertNotIn("bb8_safe_task", source)
        self.assertIn("await server.start()", source)
        self.assertIn("await start_bb8_safe_test_server(bb8_safe_server)", source)

        helper_at = source.index("async def start_bb8_safe_test_server")
        main_at = source.index("async def main():")
        helper = source[helper_at:main_at]
        self.assertLess(helper.index("await server.start()"), helper.index("BB-8 本机安全测试接口是"))

        main = source[main_at:]
        self.assertLess(
            main.index("await start_bb8_safe_test_server(bb8_safe_server)"),
            main.index("ws_task = asyncio.create_task(ws_server.start())"),
        )
        self.assertNotIn("BB-8 本机安全测试接口是", main)


if __name__ == "__main__":
    unittest.main(verbosity=2)
