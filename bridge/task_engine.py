# -*- coding: utf-8 -*-
"""BB-8 Bridge Task Engine — minimal persistent task orchestration (stdlib only).

P0/P1 hardening:
- every state transition is a conditional SQLite update inside one transaction;
- execution generation (fencing token) prevents stale executors from committing;
- task definition + submitted event are atomic; finish + event + outbox are atomic;
- recovery kills orphan task process trees before deciding retry/unknown/cancelled;
- outbox delivery is at-least-once with stable delivery_id/idempotency key and bounded backoff.

Task state lives ONLY here. Hermes/OpenMuse are executors and never write task state.
"""
from __future__ import annotations

import json, os, re, sqlite3, subprocess, threading, time, uuid, urllib.request
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse, parse_qs

DB_PATH = Path(os.environ.get("OPENMUSE_TASK_DB", str(Path(__file__).with_name("tasks.db"))))
MAX_WORKERS = max(1, int(os.environ.get("OPENMUSE_TASK_MAX_WORKERS", "2")))
HERMES_CMD = os.environ.get("OPENMUSE_TASK_HERMES_CMD", r"G:\Hermes\home\bin\hermes.cmd")
TASK_TIMEOUT = float(os.environ.get("OPENMUSE_TASK_TIMEOUT", "120"))
MAX_ATTEMPTS = max(1, int(os.environ.get("OPENMUSE_TASK_MAX_ATTEMPTS", "3")))
RECEIVER = os.environ.get("OPENMUSE_TASK_RESULT_RECEIVER", "").strip()
POLL = 0.5
LEASE_SECONDS = 60.0
HEARTBEAT_SECONDS = 15.0
DELIVER_INTERVAL = 2.0
CANCEL_CONFIRM_TIMEOUT = 8.0
OUTBOX_MAX_ATTEMPTS = max(1, int(os.environ.get("OPENMUSE_TASK_OUTBOX_MAX_ATTEMPTS", "5")))
OUTBOX_BACKOFF_BASE = max(0.05, float(os.environ.get("OPENMUSE_TASK_OUTBOX_BACKOFF_BASE", "5")))
OUTBOX_BACKOFF_CAP = 300.0
# Bounded executor output capture: keep only the tail, never buffer unbounded stdout/stderr.
OUTPUT_LIMIT = max(1024, int(os.environ.get("OPENMUSE_TASK_OUTPUT_LIMIT", "65536")))
RESULT_STDOUT_TAIL = 8000
RESULT_STDERR_TAIL = 2000
TEST_PAUSE_BEFORE_OUTBOX = os.environ.get("OPENMUSE_TASK_TEST_PAUSE_BEFORE_OUTBOX", "") == "1"
# Test-only fault injection: make the next N kill-confirmation attempts fail while the
# real process keeps running, to exercise the "cannot confirm exit" safety path.
TEST_KILL_FAIL_ONCE = max(0, int(os.environ.get("OPENMUSE_TASK_TEST_KILL_FAIL_ONCE", "0")))

_lock = threading.RLock()
_pool: ThreadPoolExecutor | None = None
_started = False
_last_recovered = 0
_running: dict[str, subprocess.Popen] = {}
_stop = threading.Event()
_kill_fail_remaining = TEST_KILL_FAIL_ONCE

QUEUED, RUNNING, PAUSED, RETRY_WAIT, UNKNOWN, CANCELLED, SUCCEEDED, FAILED = (
    "queued", "running", "paused", "retry_wait", "unknown", "cancelled", "succeeded", "failed")
TERMINAL = {CANCELLED, SUCCEEDED, FAILED, UNKNOWN}
RETRY_POLICIES = {"safe", "unknown", "never"}


# ----------------------------------------------------------------- schema / db
def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=10.0, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


@contextmanager
def _tx():
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_schema() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _connect() as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript("""
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            idempotency_key TEXT NOT NULL UNIQUE,
            kind TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            status TEXT NOT NULL,
            attempt INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL,
            priority INTEGER NOT NULL DEFAULT 0,
            owner_pid INTEGER,
            child_pid INTEGER,
            generation INTEGER NOT NULL DEFAULT 0,
            retry_policy TEXT NOT NULL DEFAULT 'unknown',
            lease_until REAL,
            cancel_requested INTEGER NOT NULL DEFAULT 0,
            next_run_at REAL NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            started_at REAL,
            finished_at REAL,
            result_json TEXT,
            error TEXT,
            step_id TEXT
        );
        CREATE TABLE IF NOT EXISTS task_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            type TEXT NOT NULL,
            ts REAL NOT NULL,
            detail_json TEXT
        );
        CREATE TABLE IF NOT EXISTS result_outbox (
            delivery_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            device_id TEXT,
            payload_json TEXT NOT NULL,
            delivered INTEGER NOT NULL DEFAULT 0,
            attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt_at REAL NOT NULL DEFAULT 0,
            last_error TEXT,
            dead_letter INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            delivered_at REAL
        );
        CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, next_run_at, priority);
        CREATE INDEX IF NOT EXISTS idx_tasks_step ON tasks(step_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_events_task ON task_events(task_id, id);
        """)
        # Forward-compatible migration for DBs created by stage 1.
        task_cols = {r[1] for r in c.execute("PRAGMA table_info(tasks)")}
        for name, ddl in {
            "child_pid": "INTEGER", "generation": "INTEGER NOT NULL DEFAULT 0",
            "retry_policy": "TEXT NOT NULL DEFAULT 'unknown'",
            # 2026-11-27 round4: step identity lives in the existing tasks table.
            "step_id": "TEXT",
        }.items():
            if name not in task_cols:
                c.execute(f"ALTER TABLE tasks ADD COLUMN {name} {ddl}")
        out_cols = {r[1] for r in c.execute("PRAGMA table_info(result_outbox)")}
        for name, ddl in {
            "delivery_id": "TEXT", "next_attempt_at": "REAL NOT NULL DEFAULT 0", "last_error": "TEXT",
            "dead_letter": "INTEGER NOT NULL DEFAULT 0",
        }.items():
            if name not in out_cols:
                c.execute(f"ALTER TABLE result_outbox ADD COLUMN {name} {ddl}")
        c.execute("UPDATE result_outbox SET delivery_id=task_id WHERE delivery_id IS NULL")
        _migrate_outbox_pk(c)
        c.execute("CREATE INDEX IF NOT EXISTS idx_outbox_pending ON result_outbox(delivered, next_attempt_at, attempts)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_outbox_task ON result_outbox(task_id, created_at)")


def _migrate_outbox_pk(c: sqlite3.Connection) -> None:
    """Rebuild legacy result_outbox (task_id PRIMARY KEY) so one task can keep one row
    per independent result. Without this, a manual retry would overwrite the previous
    result row and reuse its idempotency key, so the receiver would drop the new result.
    """
    info = c.execute("PRAGMA table_info(result_outbox)").fetchall()
    pk_cols = [r[1] for r in info if r[5]]
    if pk_cols == ["delivery_id"]:
        return
    c.execute("BEGIN IMMEDIATE")
    try:
        c.execute("DROP INDEX IF EXISTS idx_outbox_pending")
        c.execute("DROP INDEX IF EXISTS idx_outbox_delivery_id")
        c.execute("DROP INDEX IF EXISTS idx_outbox_task")
        c.execute("ALTER TABLE result_outbox RENAME TO result_outbox_legacy")
        c.execute("""CREATE TABLE result_outbox (
            delivery_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            device_id TEXT,
            payload_json TEXT NOT NULL,
            delivered INTEGER NOT NULL DEFAULT 0,
            attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt_at REAL NOT NULL DEFAULT 0,
            last_error TEXT,
            dead_letter INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            delivered_at REAL
        )""")
        c.execute("""INSERT OR IGNORE INTO result_outbox(
                         delivery_id,task_id,device_id,payload_json,delivered,attempts,
                         next_attempt_at,last_error,dead_letter,created_at,delivered_at)
                     SELECT COALESCE(delivery_id,task_id),task_id,device_id,payload_json,delivered,attempts,
                            COALESCE(next_attempt_at,0),last_error,COALESCE(dead_letter,0),created_at,delivered_at
                     FROM result_outbox_legacy""")
        c.execute("DROP TABLE result_outbox_legacy")
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise


def _event_tx(c: sqlite3.Connection, task_id: str, typ: str, detail: dict | None = None) -> None:
    c.execute("INSERT INTO task_events(task_id,type,ts,detail_json) VALUES(?,?,?,?)",
              (task_id, typ, time.time(), json.dumps(detail or {}, ensure_ascii=False)))


def _event(task_id: str, typ: str, detail: dict | None = None) -> None:
    with _tx() as c:
        _event_tx(c, task_id, typ, detail)


def _row_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    for k in ("payload_json", "result_json", "error"):
        if d.get(k) and k != "error":
            try:
                d[k] = json.loads(d[k])
            except Exception:
                pass
    return d


# ----------------------------------------------------------------- lifecycle
class ProcessScan:
    __slots__ = ("ok", "pids", "error")

    def __init__(self, ok: bool, pids: list[int] | None = None, error: str = ""):
        self.ok = ok
        self.pids = sorted(set(pids or []))
        self.error = error


_WIN_KERNEL32 = None
_WIN_NTDLL = None


def _win_kernel32():
    global _WIN_KERNEL32
    if _WIN_KERNEL32 is not None:
        return _WIN_KERNEL32
    import ctypes
    from ctypes import wintypes
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    # HANDLE OpenProcess(DWORD, BOOL, DWORD)
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.OpenProcess.restype = wintypes.HANDLE
    # BOOL GetExitCodeProcess(HANDLE, LPDWORD)
    k.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    k.GetExitCodeProcess.restype = wintypes.BOOL
    # BOOL CloseHandle(HANDLE)
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    k.CloseHandle.restype = wintypes.BOOL
    # BOOL GetProcessTimes(HANDLE, LPFILETIME, LPFILETIME, LPFILETIME, LPFILETIME)
    k.GetProcessTimes.argtypes = [wintypes.HANDLE,
                                  ctypes.POINTER(wintypes.FILETIME),
                                  ctypes.POINTER(wintypes.FILETIME),
                                  ctypes.POINTER(wintypes.FILETIME),
                                  ctypes.POINTER(wintypes.FILETIME)]
    k.GetProcessTimes.restype = wintypes.BOOL
    # BOOL ReadProcessMemory(HANDLE, LPCVOID, LPVOID, SIZE_T, SIZE_T*)
    k.ReadProcessMemory.argtypes = [wintypes.HANDLE, wintypes.LPCVOID, wintypes.LPVOID,
                                    ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
    k.ReadProcessMemory.restype = wintypes.BOOL
    _WIN_KERNEL32 = k
    return k


def _win_ntdll():
    global _WIN_NTDLL
    if _WIN_NTDLL is not None:
        return _WIN_NTDLL
    import ctypes
    from ctypes import wintypes
    ntdll = ctypes.WinDLL("ntdll", use_last_error=True)

    class UNICODE_STRING(ctypes.Structure):
        _fields_ = [("Length", wintypes.USHORT),
                    ("MaximumLength", wintypes.USHORT),
                    ("Buffer", wintypes.LPWSTR)]

    class OBJECT_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("Length", wintypes.ULONG),
                    ("RootDirectory", wintypes.HANDLE),
                    ("ObjectName", ctypes.POINTER(UNICODE_STRING)),
                    ("Attributes", wintypes.ULONG),
                    ("SecurityDescriptor", wintypes.LPVOID),
                    ("SecurityQualityOfService", wintypes.LPVOID)]

    class CLIENT_ID(ctypes.Structure):
        _fields_ = [("UniqueProcess", wintypes.HANDLE),
                    ("UniqueThread", wintypes.HANDLE)]

    class PROCESS_BASIC_INFORMATION(ctypes.Structure):
        _fields_ = [("Reserved1", wintypes.LPVOID),
                    ("PebBaseAddress", wintypes.LPVOID),
                    ("Reserved2", wintypes.LPVOID * 2),
                    ("UniqueProcessId", ctypes.POINTER(ctypes.c_ulong)),
                    ("Reserved3", wintypes.LPVOID)]

    ntdll.NtQueryInformationProcess.argtypes = [
        wintypes.HANDLE, wintypes.ULONG, wintypes.LPVOID, wintypes.ULONG, ctypes.POINTER(wintypes.ULONG)
    ]
    ntdll.NtQueryInformationProcess.restype = ctypes.c_long
    ntdll.NtOpenProcess.argtypes = [
        ctypes.POINTER(wintypes.HANDLE), wintypes.ULONG,
        ctypes.POINTER(OBJECT_ATTRIBUTES), ctypes.POINTER(CLIENT_ID)
    ]
    ntdll.NtOpenProcess.restype = ctypes.c_long
    _WIN_NTDLL = (ntdll, UNICODE_STRING, OBJECT_ATTRIBUTES, CLIENT_ID, PROCESS_BASIC_INFORMATION)
    return _WIN_NTDLL


def _pid_state(pid: int | None) -> str:
    """Return 'alive', 'exited', or 'unknown'.

    Unknown is fail-closed: callers MUST NOT treat unknown as exited.
    Windows uses OpenProcess + GetExitCodeProcess with explicit 64-bit HANDLE types.
    """
    if not pid:
        return "exited"
    try:
        pid = int(pid)
    except Exception:
        return "unknown"
    if pid <= 0:
        return "exited"
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes
            k = _win_kernel32()
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            SYNCHRONIZE = 0x00100000
            STILL_ACTIVE = 259
            ERROR_INVALID_PARAMETER = 87
            ERROR_ACCESS_DENIED = 5
            ctypes.set_last_error(0)
            h = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, False, wintypes.DWORD(pid))
            if not h:
                err = ctypes.get_last_error()
                if err == ERROR_INVALID_PARAMETER:
                    return "exited"
                # Access denied or any other error means we cannot prove exit.
                return "unknown"
            try:
                code = wintypes.DWORD()
                ok = k.GetExitCodeProcess(h, ctypes.byref(code))
                if not ok:
                    return "unknown"
                return "alive" if code.value == STILL_ACTIVE else "exited"
            finally:
                k.CloseHandle(h)
        except Exception:
            return "unknown"
    try:
        os.kill(pid, 0)
        return "alive"
    except ProcessLookupError:
        return "exited"
    except PermissionError:
        return "alive"
    except Exception:
        return "unknown"


def _pid_alive(pid: int | None) -> bool:
    """Compatibility wrapper: True for alive/unknown (fail-closed), False only for exited."""
    return _pid_state(pid) != "exited"


def _scan_test_hook(kind: str) -> ProcessScan | None:
    """Test-only fault injection for process enumeration. Never used in production."""
    mode = os.environ.get("OPENMUSE_TASK_TEST_SCAN_MODE", "")
    if not mode:
        return None
    target = os.environ.get("OPENMUSE_TASK_TEST_SCAN_KIND", kind)
    if target not in ("any", kind):
        return None
    sleep_s = float(os.environ.get("OPENMUSE_TASK_TEST_SCAN_SLEEP", "0") or "0")
    if sleep_s > 0:
        time.sleep(sleep_s)
    if mode == "timeout":
        return ProcessScan(False, error="test_injected_timeout")
    if mode == "error":
        return ProcessScan(False, error="test_injected_error")
    if mode == "denied":
        return ProcessScan(False, error="test_injected_denied")
    return None


def _find_task_processes(task_id: str, generation: int | None = None) -> ProcessScan:
    """Find executor processes for a task id.

    Returns ProcessScan(ok=False, ...) on timeout/error/denied or any read error that
    could hide a matching process. Callers MUST NOT treat ok=False as "no processes".
    ok=True with empty pids means scan succeeded and no matching processes were found.
    """
    hook = _scan_test_hook("find")
    if hook is not None:
        return hook
    pids: list[int] = []
    me = os.getpid()
    needle = task_id
    try:
        if os.name == "nt":
            ps = (r"$me=$PID; Get-CimInstance Win32_Process | "
                  r"Where-Object { $_.CommandLine -like '*%s*' } | "
                  r"ForEach-Object { $me.ToString() + '|' + $_.ProcessId.ToString() + '|' + $_.CommandLine }"
                  ) % needle
            r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True,
                               encoding="utf-8", errors="replace", timeout=10)
            if r.returncode != 0:
                err = (r.stderr or r.stdout or "").strip()[:300]
                return ProcessScan(False, error="powershell_rc=%s:%s" % (r.returncode, err))
            for line in (r.stdout or "").splitlines():
                if not line.strip():
                    continue
                parts = line.split("|", 2)
                if len(parts) < 3:
                    return ProcessScan(False, error="scan_malformed_line")
                enum_s, pid_s, _cmd = parts
                if not pid_s.strip().isdigit():
                    return ProcessScan(False, error="scan_bad_pid")
                pid = int(pid_s.strip())
                enum_pid = int(enum_s.strip()) if enum_s.strip().isdigit() else -1
                if pid == me or pid == enum_pid:
                    continue
                # Ignore our own scan processes: their command line contains the
                # task id as a wildcard pattern, not as a generation fence.
                if "Get-CimInstance Win32_Process" in _cmd and needle in _cmd:
                    continue
                pids.append(pid)
        else:
            for p in Path("/proc").glob("[0-9]*/cmdline"):
                try:
                    pid = int(p.parent.name)
                    if pid == me:
                        continue
                    cmd = p.read_bytes().decode("utf-8", "ignore")
                    if needle in cmd:
                        pids.append(pid)
                except Exception as e:
                    # unreadable proc entry could hide a matching process
                    return ProcessScan(False, error="proc_read_error:%r" % (e,))
        return ProcessScan(True, pids=pids)
    except subprocess.TimeoutExpired:
        return ProcessScan(False, error="scan_timeout")
    except Exception as e:
        return ProcessScan(False, error="scan_error:%r" % (e,))


def _kill_pid_tree(pid: int) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        else:
            os.killpg(pid, 15)
    except Exception:
        pass


def _terminate_group(proc: subprocess.Popen) -> None:
    _kill_pid_tree(proc.pid)


def _pid_cmdline(pid: int) -> str | None:
    """Return command line string, or None if unreadable/unknown."""
    try:
        if os.name == "nt":
            ps = "Get-CimInstance Win32_Process -Filter 'ProcessId=%d' -ErrorAction SilentlyContinue | Select-Object -ExpandProperty CommandLine" % pid
            r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True,
                               encoding="utf-8", errors="replace", timeout=10)
            if r.returncode != 0:
                return None
            out = r.stdout or ""
            if out.strip() == "":
                return None if _pid_alive(pid) else ""
            return out
        else:
            proc = Path("/proc/%d/cmdline" % pid)
            if not proc.exists():
                return "" if not _pid_alive(pid) else None
            return proc.read_bytes().decode("utf-8", "ignore")
    except subprocess.TimeoutExpired:
        return None
    except Exception:
        return None


def _pid_cmdline_has_fence(pid: int, fence: str) -> bool | None:
    """Return True/False if we can determine, None if cannot inspect."""
    cmd = _pid_cmdline(pid)
    if cmd is None:
        return None
    return fence in cmd

def _kill_task_tree(task_id: str, generation: int | None = None,
                    proc: subprocess.Popen | None = None,
                    child_pid: int | None = None) -> tuple[bool, str]:
    """Terminate task process tree and confirm it is gone.

    Generation-bound mode (generation is not None):
      - NEVER blindly kill a recorded child_pid. It may be PID-reused by another
        generation or unrelated process. We only issue kill after identity verifies
        (task_id + generation fence). If identity is unknown/mismatch, fail closed.
      - Scan hits are used for confirmation/attribution; only attributable same-gen
        processes may be killed. Other-gen same-task processes are ignored.
      - Any scan/identity uncertainty returns (False, detail).

    Ungenerationed mode: may target task_id-matching processes, but still fail closed
    on scan errors and unknown attribution.
    """
    global _kill_fail_remaining
    with _lock:
        if _kill_fail_remaining > 0:
            _kill_fail_remaining -= 1
            return False, "test_injected_kill_confirm_failure"

    fence = _task_fence(task_id, generation) if generation is not None else None

    def _consider_pid(pid: int, scan_seen: bool) -> tuple[str, str]:
        """Return (action, reason). action in kill/ignore/fail."""
        st = _pid_state(pid)
        if st == "exited":
            return "ignore", "exited"
        if generation is not None:
            # caller-owned proc is trusted for its pid object lifetime; still verify not exited
            if proc is not None and pid == proc.pid and proc.poll() is None:
                return "kill", "caller_proc"
            match, reason, ident = _pid_matches_task(pid, task_id, generation)
            if match is True:
                return "kill", reason
            if match is False:
                # other generation / unrelated / exited-after-match: do not kill
                if reason in ("other_generation", "task_id_missing"):
                    return "ignore", reason
                if reason in ("exited", "exited_after_match"):
                    return "ignore", reason
                # generation fence missing for a live task-id-matching pid is ambiguous
                return "fail", reason
            # unknown
            return "fail", reason
        else:
            match, reason, ident = _pid_matches_task(pid, task_id, None)
            if match is True:
                return "kill", reason
            if match is False and reason in ("exited", "exited_after_match", "task_id_missing"):
                return "ignore", reason
            return "fail", reason

    scan = _find_task_processes(task_id, generation=generation)
    if not scan.ok:
        return False, scan.error

    # Initial kill pass - only verified targets.
    initial_targets: list[int] = []
    seen: set[int] = set()
    if generation is not None:
        candidates = []
        if child_pid:
            candidates.append(int(child_pid))
        if proc is not None and proc.poll() is None:
            candidates.append(int(proc.pid))
        # scan pids are confirmation/attribution candidates; do not blindly kill
        for pid in candidates:
            if pid in seen:
                continue
            seen.add(pid)
            action, reason = _consider_pid(pid, scan_seen=pid in scan.pids)
            if action == "kill":
                initial_targets.append(pid)
            elif action == "fail":
                # cannot safely verify recorded pid identity - fail closed, no kill
                return False, "identity_unverified:%s:%s" % (pid, reason)
        # also kill scan hits that are positively attributable to same generation
        for pid in scan.pids:
            if pid in seen:
                continue
            seen.add(pid)
            action, reason = _consider_pid(pid, scan_seen=True)
            if action == "kill":
                initial_targets.append(pid)
            elif action == "fail":
                return False, "identity_unverified:%s:%s" % (pid, reason)
    else:
        for pid in list(scan.pids) + ([proc.pid] if proc is not None and proc.poll() is None else []):
            if pid in seen:
                continue
            seen.add(pid)
            action, reason = _consider_pid(pid, scan_seen=pid in scan.pids)
            if action == "kill":
                initial_targets.append(pid)
            elif action == "fail":
                return False, "identity_unverified:%s:%s" % (pid, reason)

    for pid in initial_targets:
        # Final identity recheck immediately before kill to reduce PID reuse window.
        if generation is not None and not (proc is not None and pid == proc.pid and proc.poll() is None):
            match, reason, _ = _pid_matches_task(pid, task_id, generation)
            if match is True:
                _kill_pid_tree(pid)
            elif match is False:
                continue
            else:
                return False, "identity_unverified_before_kill:%s:%s" % (pid, reason)
        else:
            _kill_pid_tree(pid)

    deadline = time.time() + CANCEL_CONFIRM_TIMEOUT
    last_err = ""
    while time.time() < deadline:
        scan = _find_task_processes(task_id, generation=generation)
        if not scan.ok:
            last_err = scan.error
            time.sleep(0.2)
            continue

        alive_unresolved: list[int] = []
        unresolved_reasons: list[str] = []

        def _mark(pid: int, reason: str) -> None:
            alive_unresolved.append(int(pid))
            unresolved_reasons.append("%s:%s" % (pid, reason))

        if generation is not None:
            # recorded child pid must be exited or confirmed other-gen/unrelated.
            if child_pid:
                action, reason = _consider_pid(int(child_pid), scan_seen=int(child_pid) in scan.pids)
                if action == "kill":
                    _kill_pid_tree(int(child_pid))
                    _mark(int(child_pid), "still_match_%s" % reason)
                elif action == "fail":
                    _mark(int(child_pid), reason)
            if proc is not None and proc.poll() is None:
                _mark(proc.pid, "caller_proc_alive")
            for pid in scan.pids:
                if child_pid and int(pid) == int(child_pid):
                    continue
                action, reason = _consider_pid(int(pid), scan_seen=True)
                if action == "kill":
                    _kill_pid_tree(int(pid))
                    _mark(int(pid), "still_match_%s" % reason)
                elif action == "fail":
                    _mark(int(pid), reason)
                # ignore => confirmed other gen / unrelated / exited
        else:
            for pid in scan.pids:
                action, reason = _consider_pid(int(pid), scan_seen=True)
                if action == "kill":
                    _kill_pid_tree(int(pid))
                    _mark(int(pid), "still_match_%s" % reason)
                elif action == "fail":
                    _mark(int(pid), reason)
            if proc is not None and proc.poll() is None:
                _mark(proc.pid, "caller_proc_alive")

        # dedupe alive unresolved
        dedup = []
        seen_alive: set[int] = set()
        for pid in alive_unresolved:
            if pid in seen_alive:
                continue
            seen_alive.add(pid)
            if _pid_state(pid) != "exited":
                dedup.append(pid)
        alive_unresolved = dedup

        if not alive_unresolved:
            return True, "confirmed"
        last_err = "unresolved:" + ",".join(unresolved_reasons[:10])
        time.sleep(0.2)

    if last_err:
        return False, last_err
    return False, "process_still_alive_after_kill"


def _reap_orphans() -> int:
    """Retry killing process trees left on UNKNOWN tasks after an unconfirmed kill.

    Never changes task back to runnable state; only clears child_pid once the
    generation-bound process scan confirms exit.
    """
    with _tx() as c:
        rows = c.execute("SELECT id,child_pid,generation FROM tasks WHERE status=? AND child_pid IS NOT NULL",
                         (UNKNOWN,)).fetchall()
    reaped = 0
    for r in rows:
        gen = int(r["generation"])
        with _tx() as c:
            cur = c.execute("SELECT status,generation,child_pid FROM tasks WHERE id=?",
                            (r["id"],)).fetchone()
            if not cur or cur["status"] != UNKNOWN or int(cur["generation"]) != gen:
                continue
        gone, detail = _kill_task_tree(r["id"], generation=gen, child_pid=cur["child_pid"])
        if not gone:
            _event(r["id"], "orphan_reap_unconfirmed", {"generation": gen, "detail": detail})
            continue
        with _tx() as c:
            cur = c.execute("UPDATE tasks SET child_pid=NULL,updated_at=? "
                            "WHERE id=? AND child_pid=? AND status=? AND generation=?",
                            (time.time(), r["id"], r["child_pid"], UNKNOWN, gen))
            if cur.rowcount == 1:
                _event_tx(c, r["id"], "orphan_reaped", {"child_pid": r["child_pid"], "generation": gen})
                reaped += 1
    return reaped


def recover() -> int:
    """Recover stale RUNNING tasks only after killing their orphan process trees.

    Generation-bound: kill and every state transition use the exact (id,generation)
    snapshot observed stale. Scan/kill uncertainty => UNKNOWN, no replay.
    """
    now = time.time()
    n = _reap_orphans()
    with _tx() as c:
        rows = c.execute("SELECT * FROM tasks WHERE status=?", (RUNNING,)).fetchall()
    for r in rows:
        snap_gen = int(r["generation"])
        stale = not _pid_alive(r["owner_pid"]) or (r["lease_until"] or 0) <= now
        if not stale:
            continue
        with _tx() as c:
            cur = c.execute("SELECT * FROM tasks WHERE id=? AND status=? AND generation=?",
                            (r["id"], RUNNING, snap_gen)).fetchone()
            if not cur:
                continue
        gone, detail = _kill_task_tree(r["id"], generation=snap_gen, child_pid=cur["child_pid"])
        with _tx() as c:
            cur = c.execute("SELECT * FROM tasks WHERE id=? AND status=? AND generation=?",
                            (r["id"], RUNNING, snap_gen)).fetchone()
            if not cur:
                continue
            if not gone:
                c.execute("UPDATE tasks SET status=?,owner_pid=NULL,lease_until=NULL,finished_at=?,"
                          "updated_at=?,error=?,cancel_requested=1 WHERE id=? AND status=? AND generation=?",
                          (UNKNOWN, now, now,
                           "orphan process tree not confirmed dead; automatic recovery refused: " + detail,
                           r["id"], RUNNING, snap_gen))
                _event_tx(c, r["id"], "recovery_kill_failed",
                          {"child_pid": cur["child_pid"], "generation": snap_gen, "detail": detail})
                n += 1
                continue
            if cur["cancel_requested"]:
                c.execute("UPDATE tasks SET status=?,owner_pid=NULL,child_pid=NULL,lease_until=NULL,"
                          "finished_at=?,updated_at=? WHERE id=? AND status=? AND generation=?",
                          (CANCELLED, now, now, r["id"], RUNNING, snap_gen))
                _event_tx(c, r["id"], "recovered_cancelled", {"generation": snap_gen})
            elif cur["retry_policy"] == "safe" and int(cur["attempt"]) + 1 < int(cur["max_attempts"]):
                delay = min(60.0, 2.0 ** (int(cur["attempt"]) + 1))
                c.execute("UPDATE tasks SET status=?,attempt=attempt+1,owner_pid=NULL,child_pid=NULL,"
                          "lease_until=NULL,next_run_at=?,updated_at=? WHERE id=? AND status=? AND generation=?",
                          (RETRY_WAIT, now + delay, now, r["id"], RUNNING, snap_gen))
                _event_tx(c, r["id"], "recovered_retry", {"delay": delay, "generation": snap_gen})
            else:
                c.execute("UPDATE tasks SET status=?,owner_pid=NULL,child_pid=NULL,lease_until=NULL,"
                          "finished_at=?,updated_at=?,error=? WHERE id=? AND status=? AND generation=?",
                          (UNKNOWN, now, now, "execution interrupted; side effects unknown",
                           r["id"], RUNNING, snap_gen))
                _event_tx(c, r["id"], "recovered_unknown", {"generation": snap_gen})
            n += 1
    return n


def submit(idempotency_key: str, kind: str, payload: dict, device_id: str = "",
           priority: int = 0, max_attempts: int | None = None,
           retry_policy: str = "unknown", step_id: str = "") -> dict:
    key = str(idempotency_key or "").strip()
    if not key:
        raise ValueError("idempotency_key required")
    p = dict(payload or {})
    if not valid_marker(p.get("_expected_marker")):
        raise ValueError("expected_marker required: task cannot be submitted without a usable result marker")
    sid = str(step_id or "").strip()
    if not sid:
        raise ValueError("step_id required: every submitted step must be identifiable")
    now = time.time()
    tid = "tsk_" + uuid.uuid4().hex[:16]
    attempts = max_attempts or MAX_ATTEMPTS
    policy = retry_policy if retry_policy in RETRY_POLICIES else "unknown"
    p["_device_id"] = device_id
    p["_step_id"] = sid
    with _tx() as c:
        existing = c.execute("SELECT * FROM tasks WHERE idempotency_key=?", (key,)).fetchone()
        if existing:
            return {"task": _row_dict(existing), "duplicate": True}
        c.execute("""INSERT INTO tasks(id,idempotency_key,kind,payload_json,status,max_attempts,
                     priority,retry_policy,next_run_at,created_at,updated_at,step_id)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (tid, key, kind, json.dumps(p, ensure_ascii=False), QUEUED,
                   attempts, int(priority), policy, now, now, now, sid or None))
        _event_tx(c, tid, "submitted", {"kind": kind, "device_id": device_id,
                                        "retry_policy": policy, "step_id": sid or None})
        row = c.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    return {"task": _row_dict(row), "duplicate": False}


def status(task_id: str) -> dict | None:
    with _connect() as c:
        return _row_dict(c.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())


def find_step(step_id: str) -> dict | None:
    """Return the newest task recorded for a step_id (reuses the tasks table; no ledger)."""
    sid = str(step_id or "").strip()
    if not sid:
        return None
    with _connect() as c:
        row = c.execute("SELECT * FROM tasks WHERE step_id=? ORDER BY created_at DESC LIMIT 1",
                        (sid,)).fetchone()
    return _row_dict(row) if row else None


def valid_marker(value) -> bool:
    """A marker is usable only when it is a non-blank string after trimming."""
    return bool(str(value or "").strip())


def marker_missing(expected_marker, stdout) -> bool:
    """True when real output does not contain a usable expected marker.

    Both an absent/blank marker and a non-matching marker are failures. A task can
    never be called verified on exit code alone.
    """
    expected = str(expected_marker or "").strip()
    return (not expected) or (expected not in str(stdout or ""))


def find_step_success(step_id: str, marker: str = "") -> dict | None:
    """Find an already verified-successful task for a step, so adjustments never replay it."""
    sid = str(step_id or "").strip()
    if not sid:
        return None
    with _connect() as c:
        rows = c.execute("SELECT id FROM tasks WHERE step_id=? ORDER BY created_at ASC",
                         (sid,)).fetchall()
    for r in rows:
        v = verified_status(r["id"], marker)
        if v["verified"]:
            return v
    return None


def verified_status(task_id: str, marker: str | None = None) -> dict:
    """External-fact judgement for a task.

    A task counts as verified success ONLY when all four hold:
      status=succeeded, current error empty, result_json.exit_code==0,
      and the expected result marker is present in stdout.
    Historical attempt failures stay in task_events (attempt_error_archived /
    retry_scheduled / failed) and are returned as `attempt_history`.
    """
    tid = str(task_id or "").strip()
    t = status(tid)
    if not t:
        return {"task_id": tid, "status": "", "terminal": False, "verified": False,
                "reason": "unknown_task", "error": None, "result": None,
                "attempt_history": []}
    err = t.get("error")
    if isinstance(err, str):
        try:
            err = json.loads(err)
        except Exception:
            err = {"raw": err}
    result = t.get("result_json")
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except Exception:
            result = None
    payload = t.get("payload_json")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            payload = {}
    # None means "no override supplied": use the marker stored with the task.
    # An explicitly supplied blank/wrong marker must fail, never silently fall back.
    expected = str((payload or {}).get("_expected_marker") or "").strip() \
        if marker is None else str(marker).strip()
    st = str(t.get("status") or "")
    terminal = st in (SUCCEEDED, FAILED, UNKNOWN, CANCELLED)
    reasons = []
    if st != SUCCEEDED:
        reasons.append("status=" + st)
    if err:
        reasons.append("error_not_empty")
    if not isinstance(result, dict) or result.get("exit_code") != 0:
        reasons.append("exit_code_not_zero")
    # A task with no usable expected marker is NEVER verified, even when every
    # other field looks successful. This closes the "no marker => pass" hole.
    if not valid_marker(expected):
        reasons.append("expected_marker_missing_or_blank")
    elif marker_missing(expected, (result or {}).get("stdout")):
        reasons.append("marker_missing:" + expected)
    hist = []
    for e in events(tid, limit=100):
        if e.get("type") in ("retry_scheduled", "attempt_error_archived", "failed",
                             "unknown", "cancelled", "attempt_failed"):
            hist.append({"type": e.get("type"), "ts": e.get("ts"), "detail": e.get("detail_json")})
    return {"task_id": tid, "step_id": t.get("step_id") or "", "status": st,
            "terminal": terminal, "verified": not reasons,
            "reason": "ok" if not reasons else ";".join(reasons),
            "error": err, "result": result, "attempt": t.get("attempt"),
            "created_at": t.get("created_at"), "finished_at": t.get("finished_at"),
            "attempt_history": hist}


def list_tasks(limit: int = 50, status_filter: str = "") -> list[dict]:
    with _connect() as c:
        if status_filter:
            rows = c.execute("SELECT * FROM tasks WHERE status=? ORDER BY created_at DESC LIMIT ?",
                             (status_filter, limit)).fetchall()
        else:
            rows = c.execute("SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [_row_dict(r) for r in rows]


def events(task_id: str, limit: int = 100) -> list[dict]:
    with _connect() as c:
        rows = c.execute("SELECT type,ts,detail_json FROM task_events WHERE task_id=? ORDER BY id DESC LIMIT ?",
                         (task_id, limit)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["detail_json"] = json.loads(d["detail_json"] or "{}")
        except Exception:
            pass
        out.append(d)
    return out


def _cancel_test_barrier() -> None:
    """Test-only: widen the read->CAS window so a real generation change can be injected."""
    path = os.environ.get("OPENMUSE_TASK_TEST_CANCEL_BARRIER", "")
    if not path:
        return
    try:
        Path(path + ".waiting").write_text("1", encoding="utf-8")
        deadline = time.time() + 10
        while time.time() < deadline:
            if Path(path + ".go").exists():
                return
            time.sleep(0.02)
    except Exception:
        pass


def cancel(task_id: str) -> dict:
    t = status(task_id)
    if not t:
        return {"ok": False, "error": "unknown task"}
    if t["status"] in TERMINAL:
        return {"ok": False, "error": "terminal state: " + t["status"]}
    if t["status"] == RUNNING:
        gen = int(t["generation"])
        _cancel_test_barrier()
        with _tx() as c:
            cur = c.execute("UPDATE tasks SET cancel_requested=1,updated_at=? "
                            "WHERE id=? AND status=? AND generation=?",
                            (time.time(), task_id, RUNNING, gen))
            if cur.rowcount != 1:
                # generation changed (retry/claim): never cancel a newer execution
                return {"ok": False, "stale": True,
                        "error": "generation changed; cancel not applied"}
            _event_tx(c, task_id, "cancel_requested", {"generation": gen})
        proc = _running.get(task_id)
        gone, detail = _kill_task_tree(task_id, generation=gen, proc=proc, child_pid=t.get("child_pid"))
        if not gone:
            _event(task_id, "cancel_kill_unconfirmed", {"generation": gen, "detail": detail})
            return {"ok": False, "pending": True,
                    "error": "process tree did not exit; cancel not confirmed: " + detail}
        with _tx() as c:
            cur = c.execute("UPDATE tasks SET status=?,owner_pid=NULL,child_pid=NULL,lease_until=NULL,"
                            "finished_at=?,updated_at=? WHERE id=? AND status=? AND generation=?",
                            (CANCELLED, time.time(), time.time(), task_id, RUNNING, gen))
            if cur.rowcount == 1:
                _event_tx(c, task_id, "cancelled", {"generation": gen})
                return {"ok": True, "mode": "process_tree_confirmed"}
            final = c.execute("SELECT status,generation FROM tasks WHERE id=?", (task_id,)).fetchone()
            if final and final["status"] == CANCELLED and int(final["generation"]) == gen:
                return {"ok": True, "mode": "process_tree_confirmed_concurrent"}
            return {"ok": False, "error": "task finished concurrently; terminal state preserved"}
    with _tx() as c:
        cur = c.execute("UPDATE tasks SET status=?,finished_at=?,updated_at=? WHERE id=? AND status IN (?,?,?)",
                        (CANCELLED, time.time(), time.time(), task_id, QUEUED, RETRY_WAIT, PAUSED))
        if cur.rowcount == 1:
            _event_tx(c, task_id, "cancelled", {"from": t["status"]})
            return {"ok": True, "mode": "immediate"}
    return {"ok": False, "error": "state changed concurrently; retry request"}


def pause(task_id: str) -> dict:
    t = status(task_id)
    if not t:
        return {"ok": False, "error": "unknown task"}
    if t["status"] in TERMINAL:
        return {"ok": False, "error": "terminal state: " + t["status"]}
    if t["status"] == RUNNING:
        return {"ok": False, "unsupported": True,
                "error": "pausing a running executor is unsupported; use cancel"}
    with _tx() as c:
        cur = c.execute("UPDATE tasks SET status=?,updated_at=? WHERE id=? AND status IN (?,?)",
                        (PAUSED, time.time(), task_id, QUEUED, RETRY_WAIT))
        if cur.rowcount == 1:
            _event_tx(c, task_id, "paused", {})
            return {"ok": True, "mode": "queued_checkpoint"}
        if t["status"] == PAUSED:
            return {"ok": True, "mode": "already_paused"}
    return {"ok": False, "error": "state changed concurrently; no paused+running state allowed"}


def resume(task_id: str) -> dict:
    with _tx() as c:
        cur = c.execute("UPDATE tasks SET status=?,next_run_at=?,updated_at=? WHERE id=? AND status=?",
                        (QUEUED, time.time(), time.time(), task_id, PAUSED))
        if cur.rowcount == 1:
            _event_tx(c, task_id, "resumed", {})
            return {"ok": True}
    return {"ok": False, "error": "only a currently paused task can be resumed"}


def retry(task_id: str, force: bool = False) -> dict:
    t = status(task_id)
    if not t:
        return {"ok": False, "error": "unknown task"}
    if t["status"] not in (FAILED, UNKNOWN, CANCELLED):
        return {"ok": False, "error": "only failed/unknown/cancelled tasks can be retried"}
    if t.get("retry_policy") == "never" and not force:
        return {"ok": False, "error": "retry_policy=never; explicit force required"}
    gen = int(t["generation"])
    child_pid = t.get("child_pid")
    if child_pid:
        # Generation-bound: old-generation process must be confirmed dead before any retry,
        # including force=True. Scan failure/timeout/denied => refuse.
        gone, detail = _kill_task_tree(task_id, generation=gen, child_pid=child_pid)
        if not gone:
            _event(task_id, "retry_blocked_live_process", {
                "generation": gen, "child_pid": child_pid, "detail": detail, "force": force,
            })
            return {"ok": False, "pending": True,
                    "error": "recorded/scan-detected process not confirmed dead; retry refused: " + detail}
        with _tx() as c:
            c.execute("UPDATE tasks SET child_pid=NULL,updated_at=? WHERE id=? AND generation=? AND status IN (?,?,?)",
                      (time.time(), task_id, gen, FAILED, UNKNOWN, CANCELLED))
    else:
        scan = _find_task_processes(task_id, generation=gen)
        if not scan.ok:
            return {"ok": False, "pending": True,
                    "error": "process scan failed; retry refused: " + scan.error}
        if scan.pids:
            gone, detail = _kill_task_tree(task_id, generation=gen)
            if not gone:
                return {"ok": False, "pending": True,
                        "error": "generation process still alive; retry refused: " + detail}
    with _tx() as c:
        cur = c.execute("UPDATE tasks SET status=?,attempt=0,generation=generation+1,cancel_requested=0,"
                        "next_run_at=?,result_json=NULL,error=NULL,finished_at=NULL,updated_at=?,child_pid=NULL "
                        "WHERE id=? AND status IN (?,?,?) AND generation=?",
                        (QUEUED, time.time(), time.time(), task_id, FAILED, UNKNOWN, CANCELLED, gen))
        if cur.rowcount == 1:
            _event_tx(c, task_id, "manual_retry", {"force": force, "from_generation": gen})
            return {"ok": True}
    return {"ok": False, "error": "state/generation changed concurrently; retry refused"}


def outbox(task_id: str = "", only_pending: bool = False) -> list[dict]:
    with _connect() as c:
        if task_id:
            rows = c.execute("SELECT * FROM result_outbox WHERE task_id=? ORDER BY created_at ASC",
                             (task_id,)).fetchall()
        elif only_pending:
            rows = c.execute("SELECT * FROM result_outbox WHERE delivered=0 AND dead_letter=0 AND attempts<? "
                             "AND next_attempt_at<=? ORDER BY created_at", (OUTBOX_MAX_ATTEMPTS, time.time())).fetchall()
        else:
            rows = c.execute("SELECT * FROM result_outbox ORDER BY created_at DESC LIMIT 50", ()).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["payload_json"] = json.loads(d["payload_json"])
        except Exception:
            pass
        out.append(d)
    return out


# ----------------------------------------------------------------- executor
def _claim() -> dict | None:
    now = time.time()
    with _tx() as c:
        row = c.execute("""SELECT * FROM tasks WHERE status IN (?,?) AND next_run_at<=?
                           AND cancel_requested=0 ORDER BY priority DESC, created_at ASC LIMIT 1""",
                        (QUEUED, RETRY_WAIT, now)).fetchone()
        if not row:
            return None
        gen = int(row["generation"]) + 1
        cur = c.execute("""UPDATE tasks SET status=?,owner_pid=?,lease_until=?,generation=?,
                           started_at=?,updated_at=?,child_pid=NULL WHERE id=? AND status IN (?,?)""",
                        (RUNNING, os.getpid(), now + LEASE_SECONDS, gen, now, now,
                         row["id"], QUEUED, RETRY_WAIT))
        if cur.rowcount != 1:
            return None
        _event_tx(c, row["id"], "claimed", {"pid": os.getpid(), "generation": gen})
        claimed = c.execute("SELECT * FROM tasks WHERE id=?", (row["id"],)).fetchone()
    return _row_dict(claimed)


def _heartbeat(task_id: str, generation: int, stop_evt: threading.Event) -> None:
    while not stop_evt.wait(HEARTBEAT_SECONDS):
        t = status(task_id)
        if not t or t["status"] != RUNNING or int(t["generation"]) != generation:
            return
        if t.get("cancel_requested"):
            proc = _running.get(task_id)
            if proc and proc.poll() is None:
                _kill_task_tree(task_id, generation=generation, proc=proc)
            return
        with _tx() as c:
            c.execute("UPDATE tasks SET lease_until=?,updated_at=? WHERE id=? AND status=? AND generation=?",
                      (time.time() + LEASE_SECONDS, time.time(), task_id, RUNNING, generation))


def _task_fence(task_id: str, generation: int) -> str:
    return "%s:g%d" % (task_id, int(generation))


def _identity_test_hook(pid: int, task_id: str | None = None, generation: int | None = None) -> dict | None:
    mode = os.environ.get("OPENMUSE_TASK_TEST_IDENTITY_MODE", "")
    if not mode:
        return None
    target_pid = os.environ.get("OPENMUSE_TASK_TEST_IDENTITY_PID")
    if target_pid and str(int(target_pid)) != str(int(pid)):
        return None
    if mode == "unknown":
        return {"pid": int(pid), "state": "unknown", "cmdline": None, "create_time_ns": None, "error": "test_injected_identity_unknown"}
    if mode == "mismatch_unrelated":
        return {"pid": int(pid), "state": "alive", "cmdline": "C:\\Windows\\System32\\notepad.exe", "create_time_ns": 1, "error": "test_injected_identity_mismatch"}
    if mode == "mismatch_other_gen":
        tid = os.environ.get("OPENMUSE_TASK_TEST_IDENTITY_TASK_ID") or task_id or "tsk_test"
        other = os.environ.get("OPENMUSE_TASK_TEST_IDENTITY_OTHER_GEN", "99")
        return {"pid": int(pid), "state": "alive", "cmdline": "%s:g%s:fence" % (tid, other), "create_time_ns": 1, "error": "test_injected_identity_other_gen"}
    return None


def _pid_identity(pid: int) -> dict:
    """Return identity evidence for a pid.

    Fields: state (alive/exited/unknown), cmdline (str|None), create_time_ns (int|None),
    error (str). cmdline/create_time_ns are best-effort evidence; unknowns are fail-closed.
    """
    hook = _identity_test_hook(pid)
    if hook is not None:
        return hook
    state = _pid_state(pid)
    info = {"pid": int(pid), "state": state, "cmdline": None, "create_time_ns": None, "error": ""}
    if state == "exited":
        return info
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes
            k = _win_kernel32()
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            PROCESS_VM_READ = 0x0010
            ERROR_ACCESS_DENIED = 5
            ctypes.set_last_error(0)
            h = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_VM_READ, False, wintypes.DWORD(int(pid)))
            if not h:
                err = ctypes.get_last_error()
                info["error"] = "openprocess_err_%s" % err
                info["state"] = "unknown" if err != 87 else "exited"
                return info
            try:
                # creation time
                creation = wintypes.FILETIME()
                exit_t = wintypes.FILETIME()
                kernel_t = wintypes.FILETIME()
                user_t = wintypes.FILETIME()
                if k.GetProcessTimes(h, ctypes.byref(creation), ctypes.byref(exit_t),
                                     ctypes.byref(kernel_t), ctypes.byref(user_t)):
                    ct = ((creation.dwHighDateTime << 32) | creation.dwLowDateTime)
                    info["create_time_ns"] = int(ct) * 100
                # command line via NtQueryInformationProcess + ReadProcessMemory
                try:
                    ntdll, _, _, _, PBI = _win_ntdll()
                    pbi = PBI()
                    ret_len = wintypes.ULONG()
                    status = ntdll.NtQueryInformationProcess(h, 0, ctypes.byref(pbi), ctypes.sizeof(pbi), ctypes.byref(ret_len))
                    if status == 0 and pbi.PebBaseAddress:
                        # Read ProcessParameters pointer from PEB.
                        # Offsets: x64 PEB.ProcessParameters @ 0x20, RTL_USER_PROCESS_PARAMETERS.CommandLine @ 0x70
                        # x86: @0x10 and @0x40. We support x64 (current harness).
                        ptr_size = ctypes.sizeof(ctypes.c_void_p)
                        params_addr = ctypes.c_void_p()
                        read = ctypes.c_size_t()
                        peb = ctypes.cast(pbi.PebBaseAddress, ctypes.c_void_p).value
                        proc_params_off = 0x20 if ptr_size == 8 else 0x10
                        cmdline_off = 0x70 if ptr_size == 8 else 0x40
                        if k.ReadProcessMemory(h, ctypes.c_void_p(peb + proc_params_off), ctypes.byref(params_addr), ptr_size, ctypes.byref(read)) and read.value == ptr_size and params_addr.value:
                            # UNICODE_STRING: USHORT Length, USHORT MaxLength, padding(4 on x64), PWSTR Buffer
                            class USTR(ctypes.Structure):
                                _fields_ = [("Length", wintypes.USHORT), ("MaximumLength", wintypes.USHORT)]
                            u = USTR()
                            # Read Length/MaxLength at cmdline_off
                            if k.ReadProcessMemory(h, ctypes.c_void_p(params_addr.value + cmdline_off), ctypes.byref(u), ctypes.sizeof(u), ctypes.byref(read)):
                                buf_ptr = ctypes.c_void_p()
                                ptr_off = cmdline_off + (8 if ptr_size == 8 else 4)
                                if k.ReadProcessMemory(h, ctypes.c_void_p(params_addr.value + ptr_off), ctypes.byref(buf_ptr), ptr_size, ctypes.byref(read)) and buf_ptr.value and u.Length > 0:
                                    nchars = u.Length // 2
                                    buf = ctypes.create_unicode_buffer(nchars + 1)
                                    if k.ReadProcessMemory(h, ctypes.c_void_p(buf_ptr.value), buf, u.Length, ctypes.byref(read)):
                                        info["cmdline"] = buf.value
                except Exception as e:
                    # cmdline unreadable: keep state but mark unknown cmdline evidence
                    info["error"] = (info["error"] + ";cmdline_err:%r" % (e,))[:300]
                # fallback cmdline via PowerShell only if not obtained
                if info["cmdline"] is None:
                    ps = "Get-CimInstance Win32_Process -Filter 'ProcessId=%d' -ErrorAction SilentlyContinue | Select-Object -ExpandProperty CommandLine" % int(pid)
                    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True,
                                       encoding="utf-8", errors="replace", timeout=10)
                    if r.returncode == 0:
                        out = r.stdout or ""
                        if out.strip() != "":
                            info["cmdline"] = out
                        elif _pid_state(pid) == "exited":
                            info["state"] = "exited"
                            return info
                        else:
                            info["error"] = (info["error"] + ";cmdline_empty").strip(";")[:300]
                    else:
                        info["error"] = (info["error"] + ";ps_rc_%s" % r.returncode).strip(";")[:300]
                # recheck liveness after evidence collection (exit edge)
                st2 = _pid_state(pid)
                if st2 == "exited":
                    info["state"] = "exited"
                elif st2 == "unknown":
                    info["state"] = "unknown"
                return info
            finally:
                k.CloseHandle(h)
        except Exception as e:
            info["state"] = "unknown"
            info["error"] = ("identity_err:%r" % (e,))[:300]
            return info
    else:
        # POSIX: cmdline from /proc, starttime not used as strong identity here
        try:
            proc = Path("/proc/%d/cmdline" % pid)
            if proc.exists():
                info["cmdline"] = proc.read_bytes().decode("utf-8", "ignore")
            elif _pid_state(pid) == "exited":
                info["state"] = "exited"
            else:
                info["error"] = "cmdline_missing"
        except Exception as e:
            info["error"] = ("cmdline_err:%r" % (e,))[:300]
            info["state"] = "unknown"
        st2 = _pid_state(pid)
        if st2 != "alive":
            info["state"] = st2
        return info


def _pid_matches_task(pid: int, task_id: str, generation: int | None) -> tuple[bool | None, str, dict]:
    """Verify pid belongs to target task/generation.

    Returns (True, reason, identity) if confirmed match.
            (False, reason, identity) if confirmed mismatch or confirmed exited.
            (None, reason, identity) if cannot confirm (fail-closed).
    """
    ident = _pid_identity(pid)
    if ident["state"] == "exited":
        return False, "exited", ident
    if ident["state"] == "unknown":
        return None, "identity_unknown", ident
    cmd = ident.get("cmdline")
    if cmd is None:
        return None, "cmdline_unknown", ident
    if task_id not in cmd:
        return False, "task_id_missing", ident
    if generation is not None:
        fence = _task_fence(task_id, generation)
        if fence in cmd:
            # Recheck liveness to reduce PID-reuse edge; if now exited, not a live match.
            st = _pid_state(pid)
            if st == "exited":
                return False, "exited_after_match", ident
            if st == "unknown":
                return None, "post_match_unknown", ident
            return True, "fence_match", ident
        # has task id but different/absent generation fence
        if re.search(re.escape(task_id) + r":g\d+", cmd):
            return False, "other_generation", ident
        return None, "generation_fence_missing", ident
    # unscoped mode: task_id match is sufficient, but still fail-closed on unknowns above
    st = _pid_state(pid)
    if st == "exited":
        return False, "exited_after_match", ident
    if st == "unknown":
        return None, "post_match_unknown", ident
    return True, "task_match", ident


def _build_cmd(task: dict) -> list[str]:
    payload = task["payload_json"] if isinstance(task["payload_json"], dict) else {}
    query = str(payload.get("query") or payload.get("text") or task["kind"])
    fence = _task_fence(task["id"], int(task.get("generation") or 0))
    # Generation-bound session name makes process scan/kill attributable and prevents
    # retries from silently continuing unknown side effects in an old Hermes session.
    cmd = [HERMES_CMD, "chat", "-q", query, "--oneshot", "--run-budget",
           str(payload.get("run_budget") or min(60, int(TASK_TIMEOUT))),
           "--continue", fence, "--create-if-missing"]
    if payload.get("toolsets"):
        cmd += ["-t", str(payload["toolsets"])]
    if payload.get("skills"):
        cmd += ["-s", str(payload["skills"])]
    # 2026-11-27 连续任务测试第2轮修正：
    # 不能加 hermes --safe-mode —— 实测它会把 provider 强制成未配置的 auto，
    # 导致所有真实技能任务在请求发出前就失败（见 run2 证据）。
    # 改为：限定安全技能白名单（bridge 侧）+ 限制工具迭代 + 显式真实 provider。
    if os.environ.get("BB8_TASK_RESTRICTED", "0") == "1":
        cmd += ["--max-turns", "8"]
    _model = os.environ.get("OPENMUSE_TASK_HERMES_MODEL", "").strip()
    if _model:
        cmd += ["-m", _model]
    return cmd
def _drain_pipe(pipe, key: str, store: dict, totals: dict, lock: threading.Lock) -> None:
    """Read a child pipe forever, keeping only the tail up to OUTPUT_LIMIT bytes."""
    while True:
        try:
            chunk = pipe.read1(65536) if hasattr(pipe, "read1") else pipe.read(65536)
        except Exception:
            break
        if not chunk:
            break
        with lock:
            totals[key] += len(chunk)
            buf = store[key]
            buf.extend(chunk)
            over = len(buf) - OUTPUT_LIMIT
            if over > 0:
                del buf[:over]


def _run_task(task: dict) -> None:
    tid, generation = task["id"], int(task["generation"])
    stop_evt = threading.Event()
    hb = threading.Thread(target=_heartbeat, args=(tid, generation, stop_evt), daemon=True)
    hb.start()
    store = {"stdout": bytearray(), "stderr": bytearray()}
    totals = {"stdout": 0, "stderr": 0}
    threads: list[threading.Thread] = []
    rc = None
    cancelled = False
    spawned = False
    tree_confirmed = True
    tree_detail = "not_needed"
    proc = None
    try:
        cmd = _build_cmd(task)
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                creationflags=creationflags,
                                start_new_session=(os.name != "nt"))
        spawned = True
        with _lock:
            _running[tid] = proc
        with _tx() as c:
            c.execute("UPDATE tasks SET child_pid=? WHERE id=? AND status=? AND generation=?",
                      (proc.pid, tid, RUNNING, generation))
        for pipe, key in ((proc.stdout, "stdout"), (proc.stderr, "stderr")):
            if pipe is None:
                continue
            th = threading.Thread(target=_drain_pipe, args=(pipe, key, store, totals, _lock),
                                  daemon=True)
            th.start()
            threads.append(th)
        try:
            rc = proc.wait(timeout=TASK_TIMEOUT)
        except subprocess.TimeoutExpired:
            cur = status(tid)
            tree_confirmed, tree_detail = _kill_task_tree(tid, generation=generation,
                                                          child_pid=(cur or {}).get("child_pid"))
            try:
                proc.wait(timeout=10)
            except Exception:
                pass
            rc = -99
        for th in threads:
            th.join(timeout=3)
        for pipe in (proc.stdout, proc.stderr):
            try:
                pipe.close()
            except Exception:
                pass
        t = status(tid)
        if t and t.get("cancel_requested"):
            cancelled = True
    except Exception as e:
        store["stderr"].extend((" | " + repr(e)).encode("utf-8"))
        totals["stderr"] += len(repr(e))
        rc = -1
    finally:
        with _lock:
            _running.pop(tid, None)
        stop_evt.set()
        hb.join(timeout=2)
    cap = {"stdout": bytes(store["stdout"]).decode("utf-8", "replace"),
           "stderr": bytes(store["stderr"]).decode("utf-8", "replace"),
           "stdout_bytes": totals["stdout"], "stderr_bytes": totals["stderr"]}
    _finish(tid, generation, rc, cap, cancelled, spawned, tree_confirmed, tree_detail)


def _write_outbox_tx(c: sqlite3.Connection, task: sqlite3.Row, payload: dict,
                     generation: int) -> None:
    payload_obj = json.loads(task["payload_json"] or "{}")
    did = str(payload_obj.get("_device_id") or "")
    # One independent idempotency key per execution result, so a manual retry
    # produces a new delivery instead of colliding with the previous result.
    delivery_id = "%s:g%d" % (task["id"], generation)
    c.execute("""INSERT OR REPLACE INTO result_outbox(delivery_id,task_id,device_id,payload_json,delivered,
                 attempts,next_attempt_at,last_error,dead_letter,created_at)
                 VALUES(?,?,?,?,0,0,0,NULL,0,?)""",
              (delivery_id, task["id"], did, json.dumps(payload, ensure_ascii=False), time.time()))


def _finish(task_id: str, generation: int, rc: int | None, cap: dict,
            cancelled: bool, spawned: bool, tree_confirmed: bool, tree_detail: str = "") -> None:
    with _tx() as c:
        t = c.execute("SELECT * FROM tasks WHERE id=? AND status=? AND generation=?",
                      (task_id, RUNNING, generation)).fetchone()
        if not t:
            return  # stale executor: fencing token prevents any commit
        now = time.time()
        meta = {"stdout_bytes": cap["stdout_bytes"], "stderr_bytes": cap["stderr_bytes"],
                "output_truncated": (cap["stdout_bytes"] > RESULT_STDOUT_TAIL
                                     or cap["stderr_bytes"] > RESULT_STDERR_TAIL)}
        if not tree_confirmed:
            err = {"exit_code": rc,
                   "error": "process tree not confirmed dead; result withheld: " + (tree_detail or "unknown"),
                   "stderr": cap["stderr"][-RESULT_STDERR_TAIL:], **meta}
            c.execute("UPDATE tasks SET status=?,attempt=attempt+1,error=?,finished_at=?,updated_at=?,"
                      "owner_pid=NULL,lease_until=NULL WHERE id=? AND status=? AND generation=?",
                      (UNKNOWN, json.dumps(err, ensure_ascii=False), now, now, task_id, RUNNING, generation))
            _write_outbox_tx(c, t, {"error": err, "exit_code": rc, "unknown_side_effects": True}, generation)
            _event_tx(c, task_id, "unknown_tree_unconfirmed", {"exit_code": rc, "detail": tree_detail})
            return
        if cancelled or t["cancel_requested"]:
            gone, detail = _kill_task_tree(task_id, generation=generation, child_pid=t["child_pid"])
            if not gone:
                err = {"exit_code": rc,
                       "error": "cancel requested but process tree not confirmed dead: " + detail,
                       "stderr": cap["stderr"][-RESULT_STDERR_TAIL:], **meta}
                c.execute("UPDATE tasks SET status=?,attempt=attempt+1,error=?,finished_at=?,updated_at=?,"
                          "owner_pid=NULL,lease_until=NULL WHERE id=? AND status=? AND generation=?",
                          (UNKNOWN, json.dumps(err, ensure_ascii=False), now, now, task_id, RUNNING, generation))
                _write_outbox_tx(c, t, {"error": err, "exit_code": rc, "unknown_side_effects": True}, generation)
                _event_tx(c, task_id, "cancelled_but_tree_unconfirmed", {"exit_code": rc, "detail": detail})
                return
            c.execute("UPDATE tasks SET status=?,owner_pid=NULL,child_pid=NULL,lease_until=NULL,"
                      "finished_at=?,updated_at=? WHERE id=? AND status=? AND generation=?",
                      (CANCELLED, now, now, task_id, RUNNING, generation))
            _event_tx(c, task_id, "cancelled_checkpoint", {"exit_code": rc})
            return
        # 2026-11-27 privacy/task closure: normal exit alone is never success.
        # Every task now carries an expected marker, and the real child output must
        # contain it before SUCCEEDED.
        expected_marker = ""
        try:
            _payload = json.loads(t["payload_json"] or "{}")
            expected_marker = str(_payload.get("_expected_marker") or "").strip()
        except Exception:
            pass
        marker_error = ("result_marker_missing:" + (expected_marker or "<blank>")
                        if rc == 0 and marker_missing(expected_marker, cap["stdout"]) else "")
        if rc == 0 and not marker_error:
            result = {"stdout": cap["stdout"][-RESULT_STDOUT_TAIL:],
                      "stderr": cap["stderr"][-RESULT_STDERR_TAIL:], "exit_code": rc, **meta}
            # 2026-11-27 round4: a retry that finally succeeds must clear the CURRENT
            # error field, but the previous attempt's timeout/failure stays in events.
            archived = None
            if t and t["error"]:
                archived = t["error"]
                _event_tx(c, task_id, "attempt_error_archived",
                          {"attempt_before": max(0, int(t["attempt"] or 0) - 1),
                           "error": archived})
            c.execute("UPDATE tasks SET status=?,result_json=?,error=NULL,finished_at=?,updated_at=?,"
                      "owner_pid=NULL,child_pid=NULL,lease_until=NULL WHERE id=?",
                      (SUCCEEDED, json.dumps(result, ensure_ascii=False), now, now, task_id))
            # Test-only crash point: task UPDATE is deliberately not committed before outbox.
            if TEST_PAUSE_BEFORE_OUTBOX:
                Path(str(DB_PATH) + ".pause").write_text(task_id, encoding="utf-8")
                time.sleep(60)
            _write_outbox_tx(c, t, result, generation)
            _event_tx(c, task_id, "succeeded", {"exit_code": rc, "error_archived": bool(archived)})
            return
        err = {"exit_code": rc, "stderr": cap["stderr"][-RESULT_STDERR_TAIL:],
               "stdout": cap["stdout"][-RESULT_STDERR_TAIL:], **meta}
        if marker_error:
            err["error"] = marker_error
        safe = (not spawned) or t["retry_policy"] == "safe"
        attempt = int(t["attempt"]) + 1
        if safe and attempt < int(t["max_attempts"]):
            delay = min(60.0, 2.0 ** attempt)
            c.execute("UPDATE tasks SET status=?,attempt=?,error=?,next_run_at=?,updated_at=?,"
                      "owner_pid=NULL,child_pid=NULL,lease_until=NULL WHERE id=?",
                      (RETRY_WAIT, attempt, json.dumps(err, ensure_ascii=False), now + delay, now, task_id))
            _event_tx(c, task_id, "retry_scheduled",
                      {"attempt": attempt, "delay": delay, "exit_code": rc, "error": err})
        else:
            final = FAILED if safe else UNKNOWN
            c.execute("UPDATE tasks SET status=?,attempt=?,error=?,finished_at=?,updated_at=?,owner_pid=NULL,"
                      "child_pid=NULL,lease_until=NULL WHERE id=?",
                      (final, attempt, json.dumps(err, ensure_ascii=False), now, now, task_id))
            _write_outbox_tx(c, t, {"error": err, "exit_code": rc, "unknown_side_effects": not safe}, generation)
            _event_tx(c, task_id, final,
                      {"attempt": attempt, "exit_code": rc, "safe": safe, "error": err})


def _worker_loop() -> None:
    while not _stop.is_set():
        task = _claim()
        if not task:
            _stop.wait(POLL)
            continue
        _run_task(task)


def _deliver_loop() -> None:
    while not _stop.wait(DELIVER_INTERVAL):
        if not RECEIVER:
            continue
        for row in outbox(only_pending=True):
            err_text = "non-2xx response"
            try:
                data = json.dumps({"delivery_id": row["delivery_id"], "task_id": row["task_id"],
                                   "device_id": row["device_id"], "payload": row["payload_json"]},
                                  ensure_ascii=False).encode("utf-8")
                req = urllib.request.Request(RECEIVER, data=data, method="POST",
                                             headers={"Content-Type": "application/json",
                                                      "Idempotency-Key": str(row["delivery_id"])})
                with urllib.request.urlopen(req, timeout=5) as resp:
                    if 200 <= resp.status < 300:
                        with _tx() as c:
                            c.execute("UPDATE result_outbox SET delivered=1,delivered_at=?,last_error=NULL "
                                      "WHERE delivery_id=? AND delivered=0",
                                      (time.time(), row["delivery_id"]))
                        _event(row["task_id"], "result_delivered", {"delivery_id": row["delivery_id"]})
                        continue
            except Exception as exc:
                err_text = repr(exc)
            attempts = int(row["attempts"]) + 1
            if attempts >= OUTBOX_MAX_ATTEMPTS:
                with _tx() as c:
                    c.execute("UPDATE result_outbox SET attempts=?,dead_letter=1,next_attempt_at=0,"
                              "last_error=? WHERE delivery_id=?",
                              (attempts, err_text[:500], row["delivery_id"]))
                    _event_tx(c, row["task_id"], "outbox_dead",
                              {"attempts": attempts, "last_error": err_text[:500],
                               "delivery_id": row["delivery_id"]})
                print("[task_engine] outbox dead letter task=%s delivery=%s attempts=%d error=%s" %
                      (row["task_id"], row["delivery_id"], attempts, err_text[:200]), flush=True)
                continue
            delay = min(OUTBOX_BACKOFF_CAP, OUTBOX_BACKOFF_BASE * (2.0 ** max(0, attempts - 1)))
            with _tx() as c:
                c.execute("UPDATE result_outbox SET attempts=?,next_attempt_at=?,last_error=? "
                          "WHERE delivery_id=?",
                          (attempts, time.time() + delay, err_text[:500], row["delivery_id"]))
            _event(row["task_id"], "outbox_retry",
                   {"attempt": attempts, "delay": delay, "delivery_id": row["delivery_id"]})
            print("[task_engine] outbox retry task=%s delivery=%s attempt=%d error=%s" %
                  (row["task_id"], row["delivery_id"], attempts, err_text[:200]), flush=True)


def init() -> dict:
    global _pool, _started, _last_recovered
    if _started:
        return {"ok": True, "already": True}
    init_schema()
    _last_recovered = recover()
    _pool = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="bb8-task")
    for _ in range(MAX_WORKERS):
        _pool.submit(_worker_loop)
    threading.Thread(target=_deliver_loop, daemon=True, name="bb8-task-deliver").start()
    _started = True
    return {"ok": True, "db": str(DB_PATH), "max_workers": MAX_WORKERS,
            "recovered": _last_recovered, "receiver": RECEIVER or None}


# ----------------------------------------------------------------- HTTP bridge
def handle_request(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    parsed = urlparse(path)
    q = parse_qs(parsed.query)
    p = parsed.path
    body = body or {}
    try:
        if method == "POST" and p == "/tasks/submit":
            key = str(body.get("idempotency_key") or "").strip()
            if not key:
                return 400, {"error": "idempotency_key required"}
            sid = str(body.get("step_id") or "").strip()
            if not sid:
                return 400, {"error": "step_id required"}
            payload = body.get("payload") or {}
            if not isinstance(payload, dict):
                return 400, {"error": "payload must be an object"}
            marker = str(payload.get("_expected_marker") or "").strip()
            if not valid_marker(marker):
                return 400, {"error": "expected_marker required: blank or missing marker cannot be verified"}
            r = submit(key, str(body.get("kind") or "hermes_cli"), payload,
                       str(body.get("device_id") or ""), int(body.get("priority") or 0),
                       body.get("max_attempts"), str(body.get("retry_policy") or "unknown"),
                       sid)
            return 202, r
        if method == "POST" and p == "/tasks/cancel":
            return 200, cancel(str(body.get("id") or ""))
        if method == "POST" and p == "/tasks/pause":
            return 200, pause(str(body.get("id") or ""))
        if method == "POST" and p == "/tasks/resume":
            return 200, resume(str(body.get("id") or ""))
        if method == "POST" and p == "/tasks/retry":
            return 200, retry(str(body.get("id") or ""), bool(body.get("force")))
        if method == "GET" and p == "/tasks/status":
            t = status(str((q.get("id") or [""])[0]))
            return (200, {"task": t}) if t else (404, {"error": "unknown task"})
        if method == "GET" and p == "/tasks/verify":
            # Absent query marker means "use the marker stored with the task".
            # A present-but-blank marker is explicit and must fail verification.
            marker = str((q.get("marker") or [""])[0]) if "marker" in q else None
            v = verified_status(str((q.get("id") or [""])[0]), marker)
            return 200, {"verification": v}
        if method == "GET" and p == "/tasks/find-step":
            t = find_step(str((q.get("step_id") or [""])[0]))
            return (200, {"task": t}) if t else (404, {"error": "unknown step"})
        if method == "GET" and p == "/tasks/list":
            return 200, {"tasks": list_tasks(int((q.get("limit") or ["50"])[0]),
                                             str((q.get("status") or [""])[0]))}
        if method == "GET" and p == "/tasks/events":
            return 200, {"events": events(str((q.get("id") or [""])[0]),
                                          int((q.get("limit") or ["100"])[0]))}
        if method == "GET" and p == "/tasks/result":
            return 200, {"outbox": outbox(str((q.get("id") or [""])[0]))}
        if method == "GET" and p == "/tasks/health":
            return 200, {"ok": True, "db": str(DB_PATH), "workers": MAX_WORKERS,
                         "running": len(_running), "receiver": RECEIVER or None,
                         "recovered_last_init": _last_recovered}
        return 404, {"error": "not found"}
    except Exception as e:
        return 500, {"error": repr(e)}
