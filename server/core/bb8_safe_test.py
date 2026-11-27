"""BB-8 R7 本机确定性安全测试入口。

安全边界说明：
  * device-id 是客户端自报身份，**未经密码学认证**。本模块只把它当作连接标识，
    不是设备身份证明。真机测试必须在可信隔离局域网内进行并有人现场监护。
  * 本地 POST 令牌只保护本机 HTTP 接口，**不证明设备身份**。
  * 单次执行锁以文件形式持久化，进程重启后仍禁止再次执行电机动作。

只允许两个固定操作：读取 self.status，以及执行一次固定参数的头部测试。
不经过 LLM / 聊天 / 语音；只绑定 127.0.0.1:18083。
"""
from __future__ import annotations

import asyncio
import hmac
import json
import os
import secrets
import subprocess
from pathlib import Path
from typing import Any, Optional

from aiohttp import web

from core.providers.tools.device_mcp.mcp_handler import call_mcp_tool

# 固定设备标识。注意：这是客户端自报值，未经密码学认证。
ALLOWED_DEVICE_ID = "14:c1:9f:50:fc:ec"

LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 18083
STATUS_PATH = "/bb8/r7/status"
DEBUG_PATH = "/bb8/r7/debug"
HEAD_TEST_PATH = "/bb8/r7/head-test-left"
TOKEN_HEADER = "X-BB8-R7-Token"

# 项目专用数据目录，不复用 config.yaml / .config.yaml 或任何业务数据。
DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "bb8-r7-safe-test"
LOCK_PATH = DATA_DIR / "executed.lock"
TOKEN_PATH = DATA_DIR / "post.token"
PURGE_INTERVAL_S = 0.25

# 服务端内部使用 MCPClient 清洗后的工具名；name_mapping 会在发往设备时还原为
# self.status / self.head.turn。
STATUS_TOOL = "self_status"
HEAD_TURN_TOOL = "self_head_turn"
HEAD_TURN_ARGS = {"direction": "left", "speed": 30, "duration_ms": 100}
HEAD_TURN_SUCCESS_TEXT = "ok: head turned left"

# R8 验证入口：只暴露固定动作，不接受任意工具名/参数。
BRAKE_NOW_PATH = "/bb8/r8/brake-now"
NAV_STOP_PATH = "/bb8/r8/nav-stop"
RESUME_PATH = "/bb8/r8/resume"
DRIVE_REFUSAL_PATH = "/bb8/r8/drive-refusal"
DRIVE_TEST_PATH = "/bb8/r8/drive-test"
BRAKE_NOW_TOOL = "self_motor_brake_now"
NAV_STOP_TOOL = "self_nav_stop"
RESUME_TOOL = "self_motor_resume"
DRIVE_TOOL = "self_motor_drive"
DRIVE_REFUSAL_ARGS = {"speed": 30, "duration_ms": 500}
DRIVE_TEST_ARGS = {"speed": 30, "duration_ms": 3000}
R8_ACTION_TOOLS = (BRAKE_NOW_TOOL, NAV_STOP_TOOL, RESUME_TOOL, DRIVE_TOOL)

POST_MOVE_WAIT_S = 1.0
MCP_TIMEOUT_S = 10

# R8 StatusJson() 在 bb8_board.cc 中返回的完整字段集合。
# 缺任何一个字段都视为“无法确认”，不执行动作。
REQUIRED_STATUS_FIELDS = (
    "left_limit",
    "right_limit",
    "center_limit",
    "head_press",
    "impact",
    "head_motor",
    "drive_motor",
    "estop",
    "drive_fault",
    "head_fault",
    "collision_stop",
    "visual_stop",
    "brake_now_stop",
    "stop_source",
    "motion_inhibit_ms",
    "follow_mode",
    "safety_gen",
)

BINARY_FIELDS = (
    "left_limit",
    "right_limit",
    "center_limit",
    "head_press",
    "impact",
    "estop",
    "drive_fault",
    "head_fault",
    "collision_stop",
    "visual_stop",
    "brake_now_stop",
    "follow_mode",
)

P_FAULT_FIELDS = ("estop", "drive_fault", "head_fault")
R_STOP_FIELDS = ("collision_stop", "visual_stop", "brake_now_stop")
VALID_STOP_SOURCES = ("NONE", "IMPACT", "VISUAL", "BRAKE_NOW")

# 用户确认的阻断条件；center_limit 故意不在此列（允许 1 -> 0）。
BLOCKING_ZERO_FIELDS = (
    "left_limit",
    "right_limit",
    "estop",
    "impact",
    "head_press",
    "drive_fault",
    "head_fault",
    "collision_stop",
    "visual_stop",
    "brake_now_stop",
    "head_motor",
    "drive_motor",
    "motion_inhibit_ms",
    "follow_mode",
)


class SafeTestError(Exception):
    """可安全返回给调用方的拒绝原因。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _result(
    ok: bool,
    code: str,
    message: str,
    **extra: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": ok,
        "code": code,
        "message": message,
        "device_id": ALLOWED_DEVICE_ID,
    }
    payload.update(extra)
    return payload


def _websocket_closed(websocket: Any) -> bool:
    if websocket is None:
        return True
    if getattr(websocket, "closed", False):
        return True
    state = getattr(websocket, "state", None)
    return getattr(state, "name", None) == "CLOSED"


# ---------------------------------------------------------------------------
# 持久化单次执行锁
# ---------------------------------------------------------------------------


def _lock_exists() -> bool:
    """无法确认锁状态时一律视为已占用（fail-closed）。"""
    try:
        return LOCK_PATH.exists()
    except OSError:
        return True


def _create_lock_file() -> None:
    """以 O_EXCL 原子创建锁文件并 fsync；写入成功后永不删除。"""
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(LOCK_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        payload = json.dumps(
            {"device_id": ALLOWED_DEVICE_ID, "action": "head_turn_left_30_100"}
        )
        os.write(fd, payload.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)


def acquire_execution_lock() -> None:
    """动作派发前必须调用；任何异常都不得自动解锁。"""
    if _lock_exists():
        raise SafeTestError("already_executed", "持久化执行锁已存在，禁止再次执行")
    try:
        _create_lock_file()
    except FileExistsError as exc:
        raise SafeTestError("already_executed", "持久化执行锁已被占用") from exc
    except Exception as exc:
        raise SafeTestError("lock_write_failed", f"持久化执行锁写入失败: {exc}") from exc


# ---------------------------------------------------------------------------
# 本地 POST 令牌
# ---------------------------------------------------------------------------


def _current_user_principal() -> str:
    """返回当前 Windows 用户的 icacls 主体名。"""
    domain = os.environ.get("USERDOMAIN") or os.environ.get("COMPUTERNAME") or ""
    user = os.environ.get("USERNAME") or ""
    if not user:
        raise RuntimeError("无法确定当前 Windows 用户，拒绝创建令牌目录")
    return f"{domain}\\{user}" if domain else user


def ensure_data_dir_acl(path: Path = DATA_DIR) -> None:
    """专用目录只允许当前用户完全控制，文件继承该 ACL。

    Windows 上不能把单个临时令牌文件收紧为 `(R)`：目录没有 FILE_DELETE_CHILD 时
    `os.replace()` / `unlink()` 会报 WinError 5。改为在目录级关闭继承并只授予
    当前用户 `(OI)(CI)(F)`，既保证只有当前用户可读，又允许原子替换与异常清理。
    POSIX 上目录 0700，令牌文件由 `os.open(..., 0o600)` 保证。
    """
    path.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(path, 0o700)
        return
    principal = _current_user_principal()
    proc = subprocess.run(
        ["icacls", str(path), "/inheritance:r", "/grant:r", f"{principal}:(OI)(CI)(F)"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"无法限制令牌目录权限: {proc.stderr.strip()}")


class LocalToken:
    """本地接口令牌；不提供任何形式的输出或日志方法。"""

    def __init__(self, value: str):
        self._value = value

    def verify(self, supplied: Optional[str]) -> bool:
        if not supplied:
            return False
        return hmac.compare_digest(str(supplied), self._value)


def _cleanup_temp_token(path: Path, cause: BaseException) -> None:
    """删除临时令牌文件；删除失败必须明确报错，禁止监听启动。"""
    try:
        path.unlink()
    except FileNotFoundError:
        return  # 已不存在，视为清理成功
    except OSError as exc:
        raise RuntimeError(
            f"临时令牌文件清理失败，禁止启动监听: {path.name}: {exc}"
        ) from cause


def load_or_create_token() -> LocalToken:
    """服务启动时生成随机令牌，原子写入受目录 ACL 保护的文件。

    令牌不写日志、不写 URL、不进入任何错误响应。
    从目录 ACL 设置 / 创建 / 写入 / fsync / os.replace 任一步失败，都清理临时文件；
    清理本身失败会抛出明确错误，监听不会启动（fail-closed）。
    """
    ensure_data_dir_acl(DATA_DIR)
    value = secrets.token_urlsafe(32)
    tmp = DATA_DIR / f".post.token.{os.getpid()}.tmp"
    fd: Optional[int] = None
    owns_tmp = False  # 所有权标记：仅当本次 os.open 成功创建文件后才置位
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        owns_tmp = True  # O_EXCL 成功，此后异常清理才允许删除该文件
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fd = None  # fd 所有权已转移给 fh，避免 finally 重复关闭
            fh.write(value)
            fh.flush()
            os.fsync(fh.fileno())
        # 目录 ACL 已保证只有当前用户可读；临时文件不再单独收紧为 (R)。
        os.replace(tmp, TOKEN_PATH)
        owns_tmp = False  # 所有权已转移给最终文件
        tmp = None  # 成功路径无需清理
        return LocalToken(value)
    except Exception as exc:
        # O_EXCL 因文件已存在而失败时 owns_tmp 仍为 False，必须保留原文件。
        if owns_tmp and tmp is not None:
            _cleanup_temp_token(tmp, exc)
        raise
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# 最小连接注册表
# ---------------------------------------------------------------------------

_registry_lock = asyncio.Lock()
_active_connections: dict[str, Any] = {}


async def _is_connection_usable(conn: Any) -> bool:
    """只做身份与 WebSocket 存活检查；MCP 就绪留到调用时再判定。

    注册发生在握手早期，MCP initialize/tools-list 尚未完成；若在这里就要求
    ready，0.25s 的清理循环会把刚登记的连接误删。
    """
    if getattr(conn, "device_id", None) != ALLOWED_DEVICE_ID:
        return False
    if _websocket_closed(getattr(conn, "websocket", None)):
        return False
    return True


async def register_active_connection(device_id: str, conn: Any) -> None:
    """只登记固定标识的连接；该标识由客户端自报，未经密码学认证。"""
    if device_id != ALLOWED_DEVICE_ID:
        return
    async with _registry_lock:
        _active_connections[device_id] = conn
    print(f"[bb8-safe-test] registered {device_id}", flush=True)


async def unregister_active_connection(device_id: str, conn: Any) -> None:
    """连接关闭时按对象身份移除，避免误删重连后的新连接。"""
    async with _registry_lock:
        if _active_connections.get(device_id) is conn:
            _active_connections.pop(device_id, None)


async def purge_stale_connections() -> None:
    """连接关闭或 MCP 不再 ready 时立即注销。"""
    async with _registry_lock:
        for device_id, conn in list(_active_connections.items()):
            try:
                usable = await _is_connection_usable(conn)
            except Exception:
                usable = False
            if not usable:
                _active_connections.pop(device_id, None)
                print(f"[bb8-safe-test] purged {device_id}", flush=True)


async def get_active_connection(device_id: str) -> Optional[Any]:
    await purge_stale_connections()
    async with _registry_lock:
        return _active_connections.get(device_id)


async def _resolve_connection() -> tuple[Any, Any]:
    """确认活动连接归属、websocket 存活状态与 MCP 工具就绪状态。"""
    conn = await get_active_connection(ALLOWED_DEVICE_ID)
    if conn is None:
        raise SafeTestError("connection_missing", "未找到可用且 MCP ready 的活动连接")

    if getattr(conn, "device_id", None) != ALLOWED_DEVICE_ID:
        raise SafeTestError("connection_identity_mismatch", "活动连接 device-id 不匹配")

    if _websocket_closed(getattr(conn, "websocket", None)):
        raise SafeTestError("connection_closed", "设备 WebSocket 已断开")

    mcp_client = getattr(conn, "mcp_client", None)
    if mcp_client is None:
        raise SafeTestError("mcp_client_missing", "设备未声明 MCP 能力")

    if not await mcp_client.is_ready():
        raise SafeTestError("mcp_not_ready", "设备 MCP 握手或 tools/list 未完成")

    for tool_name in (STATUS_TOOL, HEAD_TURN_TOOL):
        if not mcp_client.has_tool(tool_name):
            raise SafeTestError("tool_missing", f"设备缺少工具: {tool_name}")

    return conn, mcp_client


async def _call_tool(conn: Any, mcp_client: Any, tool_name: str, args: Any) -> dict[str, Any]:
    """复用现有 MCP 调用链，并保留工具原始返回值用于审计。"""
    if _websocket_closed(getattr(conn, "websocket", None)):
        raise SafeTestError("connection_closed", "调用前设备 WebSocket 已断开")
    if not await mcp_client.is_ready():
        raise SafeTestError("mcp_not_ready", "调用前 MCP 未就绪")

    text, raw = await call_mcp_tool(
        conn,
        mcp_client,
        tool_name,
        args,
        timeout=MCP_TIMEOUT_S,
        include_raw=True,
    )

    if _websocket_closed(getattr(conn, "websocket", None)):
        raise SafeTestError("connection_closed", "调用后设备 WebSocket 已断开")
    return {"text": text, "raw": raw}


def _parse_status(record: dict[str, Any]) -> dict[str, int]:
    """严格核对 status 字段；任何缺失或类型异常都停止。"""
    text = record.get("text")
    if not isinstance(text, str) or not text.strip():
        raise SafeTestError("status_empty", "self.status 未返回文本内容")

    try:
        parsed = json.loads(text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise SafeTestError("status_format_invalid", f"self.status 不是合法 JSON: {exc}") from exc

    if not isinstance(parsed, dict):
        raise SafeTestError("status_format_invalid", "self.status 返回的不是 JSON 对象")

    missing = [name for name in REQUIRED_STATUS_FIELDS if name not in parsed]
    if missing:
        raise SafeTestError("status_fields_missing", f"self.status 缺少字段: {missing}")

    for name in REQUIRED_STATUS_FIELDS:
        if name == "stop_source":  # 字符串枚举，单独校验
            continue
        value = parsed[name]
        if isinstance(value, bool) or not isinstance(value, int):
            raise SafeTestError(
                "status_field_type_invalid",
                f"self.status 字段 {name} 不是整数: {value!r}",
            )

    for name in BINARY_FIELDS:
        if parsed[name] not in (0, 1):
            raise SafeTestError(
                "status_field_value_invalid",
                f"self.status 字段 {name} 不是 0/1: {parsed[name]}",
            )

    for name in ("head_motor", "drive_motor"):
        if not -100 <= parsed[name] <= 100:
            raise SafeTestError(
                "status_field_value_invalid",
                f"self.status 字段 {name} 超出电机速度范围: {parsed[name]}",
            )

    if parsed["stop_source"] not in VALID_STOP_SOURCES:
        raise SafeTestError(
            "status_field_value_invalid",
            f"self.status 字段 stop_source 非法: {parsed['stop_source']!r}",
        )

    if parsed["motion_inhibit_ms"] < 0 or parsed["safety_gen"] < 0:
        raise SafeTestError("status_field_value_invalid", "motion_inhibit_ms/safety_gen 为负数")

    record["parsed"] = parsed
    return parsed


def _check_preconditions(status: dict[str, int]) -> None:
    blocked = [name for name in BLOCKING_ZERO_FIELDS if status[name] != 0]
    if blocked:
        raise SafeTestError("precondition_failed", f"阻断字段非 0: {blocked}")


def _check_postconditions(before: dict[str, int], after: dict[str, int]) -> None:
    blocked = [name for name in BLOCKING_ZERO_FIELDS if after[name] != 0]
    if blocked:
        raise SafeTestError("postcondition_failed", f"动作后阻断字段非 0: {blocked}")

    if after["center_limit"] not in (0, 1):
        raise SafeTestError("postcondition_failed", "动作后 center_limit 非法")

    if after["safety_gen"] != before["safety_gen"]:
        raise SafeTestError(
            "postcondition_failed",
            f"安全代次变化: {before['safety_gen']} -> {after['safety_gen']}",
        )


class SafeTestRunner:
    """固定流程、持久化单次执行锁、禁止重试的编排器。"""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    async def status(self) -> dict[str, Any]:
        if self._lock.locked():
            return _result(False, "busy", "已有安全测试流程在执行中")

        async with self._lock:
            try:
                conn, mcp_client = await _resolve_connection()
                record = await _call_tool(conn, mcp_client, STATUS_TOOL, "{}")
                _parse_status(record)
                return _result(True, "ok", "status 读取成功", status=record)
            except SafeTestError as exc:
                return _result(False, exc.code, exc.message)
            except Exception as exc:  # noqa: BLE001 - 对外只暴露安全失败原因
                return _result(False, "internal_error", f"status 读取异常: {exc}")

    async def run_head_test(self) -> dict[str, Any]:
        before: Optional[dict[str, Any]] = None
        head_turn: Optional[dict[str, Any]] = None
        after: Optional[dict[str, Any]] = None
        lock_acquired = False

        if self._lock.locked():
            return _result(False, "busy", "已有安全测试流程在执行中", action_executed=False)
        if _lock_exists():
            return _result(
                False, "already_executed", "持久化执行锁已存在，禁止重复触发", action_executed=False
            )

        async with self._lock:
            if _lock_exists():
                return _result(
                    False, "already_executed", "持久化执行锁已存在，禁止重复触发", action_executed=False
                )

            try:
                conn, mcp_client = await _resolve_connection()

                before = await _call_tool(conn, mcp_client, STATUS_TOOL, "{}")
                before_status = _parse_status(before)
                _check_preconditions(before_status)

                # 动作派发前原子写入持久化锁；写入失败一律不派发动作。
                # 锁写入成功后永不删除：崩溃、超时、结果不确定都保留。
                acquire_execution_lock()
                lock_acquired = True

                head_turn = await _call_tool(conn, mcp_client, HEAD_TURN_TOOL, dict(HEAD_TURN_ARGS))
                head_text = str(head_turn.get("text") or "").strip()
                if head_text != HEAD_TURN_SUCCESS_TEXT:
                    raise SafeTestError(
                        "head_turn_ambiguous",
                        f"头部动作返回非成功结果，立即停止: {head_text}",
                    )

                await asyncio.sleep(POST_MOVE_WAIT_S)

                after = await _call_tool(conn, mcp_client, STATUS_TOOL, "{}")
                after_status = _parse_status(after)
                _check_postconditions(before_status, after_status)

                return _result(
                    True,
                    "ok",
                    "单次头部安全测试完成",
                    action_executed=True,
                    before=before,
                    head_turn=head_turn,
                    after=after,
                )
            except SafeTestError as exc:
                return _result(
                    False,
                    exc.code,
                    exc.message,
                    action_executed=lock_acquired,
                    before=before,
                    head_turn=head_turn,
                    after=after,
                )
            except Exception as exc:  # noqa: BLE001 - 对外只暴露安全失败原因
                return _result(
                    False,
                    "internal_error",
                    f"安全测试异常: {exc}",
                    action_executed=lock_acquired,
                    before=before,
                    head_turn=head_turn,
                    after=after,
                )


def _active_fields(status: dict[str, int], names: tuple[str, ...]) -> list[str]:
    return [name for name in names if status[name] != 0]


class R8ValidationRunner:
    """R8 安全域验证：只派发四个固定动作，全部经 MCP 原始结果审计。"""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    async def run(self, kind: str) -> dict[str, Any]:
        if kind not in ("brake-now", "nav-stop", "resume", "drive-refusal", "drive-test"):
            return _result(False, "unknown_action", "未知 R8 验证动作")
        if self._lock.locked():
            return _result(False, "busy", "已有安全验证流程在执行中")

        async with self._lock:
            before: Optional[dict[str, Any]] = None
            action: Optional[dict[str, Any]] = None
            after: Optional[dict[str, Any]] = None
            try:
                conn, mcp_client = await _resolve_connection()
                for tool_name in R8_ACTION_TOOLS:
                    if not mcp_client.has_tool(tool_name):
                        raise SafeTestError("tool_missing", f"设备缺少工具: {tool_name}")

                before = await _call_tool(conn, mcp_client, STATUS_TOOL, "{}")
                before_status = _parse_status(before)
                p_before = _active_fields(before_status, P_FAULT_FIELDS)
                r_before = _active_fields(before_status, R_STOP_FIELDS)

                if kind in ("brake-now", "nav-stop", "drive-test"):
                    if p_before:
                        raise SafeTestError("precondition_failed", f"域 P 已锁存: {p_before}")
                    if r_before:
                        raise SafeTestError("precondition_failed", f"域 R 已停止: {r_before}")
                    if _check_preconditions(before_status) is not None:
                        raise SafeTestError("precondition_failed", "基线状态不满足阻断条件")
                elif kind == "resume":
                    # 允许在域 P 已锁存时调用：这正是验证“软件恢复不能解 P”的场景。
                    if not r_before:
                        raise SafeTestError("precondition_failed", "没有可恢复的域 R 停止")
                elif kind == "drive-refusal":
                    if p_before:
                        raise SafeTestError("precondition_failed", f"域 P 已锁存: {p_before}")
                    if not r_before:
                        raise SafeTestError("precondition_failed", "域 R 未停止，不能验证拒绝")

                tool_name, args = {
                    "brake-now": (BRAKE_NOW_TOOL, {}),
                    "nav-stop": (NAV_STOP_TOOL, {}),
                    "resume": (RESUME_TOOL, {}),
                    "drive-refusal": (DRIVE_TOOL, dict(DRIVE_REFUSAL_ARGS)),
                    "drive-test": (DRIVE_TOOL, dict(DRIVE_TEST_ARGS)),
                }[kind]
                action = await _call_tool(conn, mcp_client, tool_name, args)
                action_text = str(action.get("text") or "")
                await asyncio.sleep(0.3)

                after = await _call_tool(conn, mcp_client, STATUS_TOOL, "{}")
                after_status = _parse_status(after)
                p_after = _active_fields(after_status, P_FAULT_FIELDS)
                r_after = _active_fields(after_status, R_STOP_FIELDS)

                checks: dict[str, Any] = {
                    "p_unchanged": p_after == p_before,
                    "p_active_after": p_after,
                    "r_active_before": r_before,
                    "r_active_after": r_after,
                    "source_after": after_status["stop_source"],
                    "gen_before": before_status["safety_gen"],
                    "gen_after": after_status["safety_gen"],
                    "action_text": action_text,
                }
                expected: dict[str, Any] = {}

                if kind == "brake-now":
                    expected = {
                        "brake_now_stop_set": after_status["brake_now_stop"] == 1,
                        "source_is_brake_now": after_status["stop_source"] == "BRAKE_NOW",
                        "gen_increased": after_status["safety_gen"] > before_status["safety_gen"],
                        "action_ok": action_text.startswith("ok:"),
                    }
                elif kind == "nav-stop":
                    expected = {
                        "visual_stop_set": after_status["visual_stop"] == 1,
                        "source_is_visual": after_status["stop_source"] == "VISUAL",
                        "gen_increased": after_status["safety_gen"] > before_status["safety_gen"],
                        "action_ok": action_text.startswith("ok:"),
                    }
                elif kind == "resume":
                    expect_refusal = before_status["motion_inhibit_ms"] > 0 or bool(p_before)
                    checks["expect_refusal_during_hold_or_p"] = expect_refusal
                    expected = {
                        "refusal_matches": (
                            action_text.startswith("refused:") == expect_refusal
                        ),
                    }
                    if expect_refusal:
                        # 保持期内或域 P 锁存时拒绝：R 不清除，gen 不变。
                        expected["gen_unchanged_on_refusal"] = (
                            after_status["safety_gen"] == before_status["safety_gen"]
                        )
                        expected["r_unchanged_on_refusal"] = r_after == r_before
                        expected["source_unchanged_on_refusal"] = (
                            after_status["stop_source"] == before_status["stop_source"]
                        )
                    else:
                        expected["r_cleared"] = not r_after
                        expected["source_none"] = after_status["stop_source"] == "NONE"
                        expected["gen_increased"] = (
                            after_status["safety_gen"] > before_status["safety_gen"]
                        )
                elif kind == "drive-test":
                    # 是否在 3 秒窗口内被物理 IMPACT 打断，由调用方按 action_text 判定。
                    expected = {
                        "drive_attempted": True,
                        "p_unchanged": checks["p_unchanged"],
                    }
                elif kind == "drive-refusal":
                    expected = {
                        "refused": action_text.startswith("refused:"),
                        "r_unchanged": r_after == r_before,
                        "gen_unchanged": (
                            after_status["safety_gen"] == before_status["safety_gen"]
                        ),
                        "motors_zero": (
                            after_status["head_motor"] == 0
                            and after_status["drive_motor"] == 0
                        ),
                    }

                checks["expected"] = expected
                passed = checks["p_unchanged"] and all(expected.values())
                return _result(
                    True if passed else False,
                    "ok" if passed else "assertion_failed",
                    f"R8 {kind} 验证{'通过' if passed else '断言未通过'}",
                    action=kind,
                    before=before,
                    tool_call=action,
                    after=after,
                    checks=checks,
                )
            except SafeTestError as exc:
                return _result(
                    False,
                    exc.code,
                    exc.message,
                    action=kind,
                    before=before,
                    tool_call=action,
                    after=after,
                )
            except Exception as exc:  # noqa: BLE001 - 对外只暴露安全失败原因
                return _result(
                    False,
                    "internal_error",
                    f"R8 验证异常: {exc}",
                    action=kind,
                    before=before,
                    tool_call=action,
                    after=after,
                )


RUNNER_KEY = web.AppKey("bb8_safe_test_runner", SafeTestRunner)
R8_RUNNER_KEY = web.AppKey("bb8_r8_validation_runner", R8ValidationRunner)
TOKEN_KEY = web.AppKey("bb8_safe_test_token", LocalToken)


def create_app(
    runner: Optional[SafeTestRunner] = None,
    token: Optional[LocalToken] = None,
) -> web.Application:
    auth_token = token if token is not None else LocalToken(secrets.token_urlsafe(32))
    app = web.Application()
    app[RUNNER_KEY] = runner or SafeTestRunner()
    app[R8_RUNNER_KEY] = R8ValidationRunner()
    app[TOKEN_KEY] = auth_token
    # allow_head=False：只暴露 GET / POST 两个路由，不额外注册 HEAD。
    app.router.add_get(STATUS_PATH, _status_handler, allow_head=False)
    app.router.add_get(DEBUG_PATH, _debug_handler, allow_head=False)
    app.router.add_post(HEAD_TEST_PATH, _head_test_handler)
    app.router.add_post(BRAKE_NOW_PATH, _r8_brake_now_handler)
    app.router.add_post(NAV_STOP_PATH, _r8_nav_stop_handler)
    app.router.add_post(RESUME_PATH, _r8_resume_handler)
    app.router.add_post(DRIVE_REFUSAL_PATH, _r8_drive_refusal_handler)
    app.router.add_post(DRIVE_TEST_PATH, _r8_drive_test_handler)
    return app


def _loopback_guard(request: web.Request) -> Optional[web.Response]:
    if request.remote not in ("127.0.0.1", "::1"):
        return web.json_response(
            _result(False, "remote_rejected", "仅允许本机 loopback 访问"),
            status=403,
        )
    return None


async def _status_handler(request: web.Request) -> web.Response:
    guarded = _loopback_guard(request)
    if guarded is not None:
        return guarded
    runner: SafeTestRunner = request.app[RUNNER_KEY]
    return web.json_response(await runner.status())


async def _head_test_handler(request: web.Request) -> web.Response:
    guarded = _loopback_guard(request)
    if guarded is not None:
        return guarded

    # 令牌只通过专用 Header 传递，不进 URL、不进日志、不进响应。
    token: LocalToken = request.app[TOKEN_KEY]
    if not token.verify(request.headers.get(TOKEN_HEADER)):
        return web.json_response(
            _result(False, "unauthorized", "本地令牌校验失败"),
            status=401,
        )

    raw = await request.text()
    if raw.strip():
        return web.json_response(
            _result(False, "unexpected_body", "该接口不接受任何参数"),
            status=400,
        )

    runner: SafeTestRunner = request.app[RUNNER_KEY]
    return web.json_response(await runner.run_head_test())


async def _debug_handler(request: web.Request) -> web.Response:
    guarded = _loopback_guard(request)
    if guarded is not None:
        return guarded
    async with _registry_lock:
        entries = list(_active_connections.items())
    details = []
    for device_id, conn in entries:
        mcp_client = getattr(conn, "mcp_client", None)
        websocket = getattr(conn, "websocket", None)
        state = getattr(getattr(websocket, "state", None), "name", None)
        try:
            ready = await mcp_client.is_ready() if mcp_client is not None else None
        except Exception:
            ready = "error"
        details.append({
            "device_id": device_id,
            "conn_device_id": getattr(conn, "device_id", None),
            "ws_closed": getattr(websocket, "closed", None),
            "ws_state": state,
            "mcp_present": mcp_client is not None,
            "mcp_ready": ready,
            "has_status": bool(mcp_client and mcp_client.has_tool(STATUS_TOOL)),
            "has_head": bool(mcp_client and mcp_client.has_tool(HEAD_TURN_TOOL)),
            "tool_count": len(getattr(mcp_client, "tools", {}) or {}),
        })
    return web.json_response(_result(True, "ok", "debug", registry=details))


async def _run_r8(request: web.Request, kind: str) -> web.Response:
    guarded = _loopback_guard(request)
    if guarded is not None:
        return guarded
    token: LocalToken = request.app[TOKEN_KEY]
    if not token.verify(request.headers.get(TOKEN_HEADER)):
        return web.json_response(
            _result(False, "unauthorized", "本地令牌校验失败"),
            status=401,
        )
    runner: R8ValidationRunner = request.app[R8_RUNNER_KEY]
    return web.json_response(await runner.run(kind))


async def _r8_brake_now_handler(request: web.Request) -> web.Response:
    return await _run_r8(request, "brake-now")


async def _r8_nav_stop_handler(request: web.Request) -> web.Response:
    return await _run_r8(request, "nav-stop")


async def _r8_resume_handler(request: web.Request) -> web.Response:
    return await _run_r8(request, "resume")


async def _r8_drive_refusal_handler(request: web.Request) -> web.Response:
    return await _run_r8(request, "drive-refusal")


async def _r8_drive_test_handler(request: web.Request) -> web.Response:
    return await _run_r8(request, "drive-test")


class LoopbackSafeTestServer:
    """独立的 127.0.0.1:18083 监听，不复用 8003。"""

    def __init__(self) -> None:
        self._app_runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None
        self._purge_task: Optional[asyncio.Task] = None

    async def _purge_loop(self) -> None:
        while True:
            try:
                await purge_stale_connections()
            except Exception:
                pass
            await asyncio.sleep(PURGE_INTERVAL_S)

    async def start(self) -> None:
        if self._site is not None:
            return
        # 权限收紧失败会抛异常，监听不启动（fail-closed）。
        token = load_or_create_token()
        app = create_app(token=token)
        self._app_runner = web.AppRunner(app)
        await self._app_runner.setup()
        self._site = web.TCPSite(self._app_runner, LISTEN_HOST, LISTEN_PORT)
        await self._site.start()
        self._purge_task = asyncio.create_task(self._purge_loop())
        print(
            f"[bb8-safe-test] listening on http://{LISTEN_HOST}:{LISTEN_PORT}"
            f"{STATUS_PATH} / {HEAD_TEST_PATH} / R8 validation endpoints",
            flush=True,
        )

    async def stop(self) -> None:
        if self._purge_task is not None:
            self._purge_task.cancel()
            try:
                await self._purge_task
            except asyncio.CancelledError:
                pass
            self._purge_task = None
        if self._app_runner is not None:
            await self._app_runner.cleanup()
            self._app_runner = None
            self._site = None
