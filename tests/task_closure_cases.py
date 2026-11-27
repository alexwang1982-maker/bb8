# -*- coding: utf-8 -*-
"""BB-8 round4 task-closure acceptance cases.

All judgements use external facts: Task Engine SQLite rows via HTTP, /tasks/verify,
result_outbox, task_events and bridge logs. The assistant's own narrative is never
treated as completion evidence.

Usage:
  python tests/task_closure_cases.py --tag round4_r1
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BRIDGE = "http://127.0.0.1:8910"
ROOT = Path(__file__).resolve().parents[1]
USER = "bb8-task-closure-verify-01"
# Bridge session_key(): when body.user is present it hashes that user only.
SESSION_KEY = hashlib.sha256(USER.encode("utf-8")).hexdigest()[:16]

TOOLS = [
    {"type": "function", "function": {
        "name": "submit_task",
        "description": "提交一个真实非物理技能步骤给 Task Engine。必须写 step_id；多步骤必须写 depends_on 和 expected_marker。",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"}, "skill": {"type": "string"},
            "idempotency_key": {"type": "string"}, "step_id": {"type": "string"},
            "depends_on": {"type": "string"}, "expected_marker": {"type": "string"},
        }, "required": ["query", "step_id", "expected_marker"]}}},
    {"type": "function", "function": {
        "name": "check_task",
        "description": "按真实任务 ID 查询四重成功校验、状态、error、outbox 和历史 attempt。",
        "parameters": {"type": "object", "properties": {
            "task_id": {"type": "string"}, "expected_marker": {"type": "string"},
        }, "required": ["task_id"]}}},
]

PERSONA = (
    "你是桌上的 BB-8。安全规则：只做聊天、视觉观察和只读/非物理技能；"
    "绝不控制电机、不刷固件、不解除安全锁。"
    "任务必须通过 submit_task/check_task 工具处理，工具返回什么就如实说什么。"
    "不要自行判断技能是否可用，也不要说工具不存在；技能可用性由 submit_task 工具判定，你必须先调用它。"
    "查询任务状态必须调用 check_task，不许凭记忆回答。"
    "每次提交都必须给 step_id 和 expected_marker；依赖任务还必须给 depends_on。"
    "已成功步骤不要重复。"
    "只有 status=succeeded、error 为空、exit_code=0、结果标记出现才算完成。"
)


class Env:
    def __init__(self, tag):
        self.tag = tag
        self.evid = ROOT / "evidence" / "real-env-continuous" / tag
        self.evid.mkdir(parents=True, exist_ok=True)

    @property
    def log(self):
        # Bridge writes continuously; always read the latest log before judging.
        p = self.evid / "bridge.out.log"
        return p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""

    def dump(self, name, obj):
        p = self.evid / name
        p.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        return name

    def marker(self, name):
        return "BB8-%s-%s-OK" % (self.tag.upper().replace("_", "-"), name)


def http(method, url, body=None, headers=None, timeout=360):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    h = dict(headers or {})
    if data is not None:
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            return {"status": r.status, "elapsed_s": round(time.time() - t0, 3),
                    "raw": raw, "json": json.loads(raw) if raw else None, "error": None}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        return {"status": e.code, "elapsed_s": round(time.time() - t0, 3),
                "raw": raw, "json": None, "error": "HTTPError " + str(e.code)}
    except Exception as e:
        return {"status": 0, "elapsed_s": round(time.time() - t0, 3),
                "raw": "", "json": None, "error": repr(e)}


def chat(messages, env=None):
    # Task tools must be sent explicitly; bridge only auto-adds ask_brain/forward_to_pc.
    return http("POST", BRIDGE + "/v1/chat/completions",
                {"model": "openmuse", "messages": messages, "stream": False, "user": USER,
                 "tools": TOOLS}, timeout=390)


def api(path, body=None, method=None, timeout=60):
    return http(method or ("POST" if body is not None else "GET"),
                BRIDGE + path, body, timeout=timeout)


def content(resp):
    try:
        return resp["json"]["choices"][0]["message"].get("content") or ""
    except Exception:
        return ""


def usermsg(text):
    return [{"role": "system", "content": PERSONA}, {"role": "user", "content": text}]


def verify(tid, marker=""):
    r = api("/tasks/verify?id=" + urllib.parse.quote(tid) +
            ("&marker=" + urllib.parse.quote(marker) if marker else ""))
    return r.get("json", {}).get("verification", {}) if r.get("json") else {}


def wait_verify(tid, marker="", seconds=330):
    end = time.time() + seconds
    last = {}
    while time.time() < end:
        last = verify(tid, marker)
        if last.get("terminal"):
            return last
        time.sleep(2)
    return last


def task_list(limit=100):
    return ((api("/tasks/list?limit=%d" % limit).get("json") or {}).get("tasks") or [])


def find_step(step_id):
    r = api("/tasks/find-step?step_id=" + urllib.parse.quote(step_id))
    return (r.get("json") or {}).get("task")


def tasks_for_step(step_id):
    return [t for t in task_list(100) if t.get("step_id") == step_id]


def find_task_by_marker(marker, step_id=""):
    rows = task_list(100)
    hits = [t for t in rows if marker in json.dumps(t, ensure_ascii=False, default=str)]
    if step_id:
        hits = [t for t in hits if t.get("step_id") == step_id]
    return hits[0] if hits else None


def outbox(tid):
    return ((api("/tasks/result?id=" + urllib.parse.quote(tid)).get("json") or {}).get("outbox") or [])


def wait_outbox_delivered(tid, marker="", seconds=90):
    """Wait until the result_outbox row is really delivered to the receiver.

    The T6 failure fixture read the outbox too early: delivery is asynchronous,
    so a Brain turn could drain no pending result and inject no context. This
    helper makes the fixture wait on the external fact before starting the turn.
    """
    end = time.time() + seconds
    while time.time() < end:
        rows = outbox(tid) if tid else []
        hit = [o for o in rows if o.get("delivered") == 1 and
               (not marker or marker in str((o.get("payload_json") or {}).get("stdout") or ""))]
        if hit:
            return hit
        time.sleep(1)
    return []


def events(tid):
    return ((api("/tasks/events?id=" + urllib.parse.quote(tid)).get("json") or {}).get("events") or [])


def result(ok, case, evidence, observed, elapsed):
    return {"case": case, "pass": bool(ok), "external_evidence": evidence,
            "observed": observed, "elapsed_s": round(elapsed, 2)}


def stable_query(marker):
    return ("请只用 document-to-action-items 把这句话整理成三条行动项，直接在回复中输出，"
            "不要写任何文件，也不要调用文件工具。第一行必须原样输出标记 %s。" % marker)


def submit_direct(key, step_id, query, skill="document-to-action-items", marker=None,
                  max_attempts=1, device_id=""):
    if not str(marker or "").strip():
        raise ValueError("test fixture must supply a valid expected_marker")
    payload = {"query": query, "skills": skill, "run_budget": 60}
    if marker:
        payload["_expected_marker"] = marker
    return api("/tasks/submit", {
        "idempotency_key": key, "kind": "hermes_cli", "payload": payload,
        "max_attempts": max_attempts, "retry_policy": "safe", "step_id": step_id,
        "device_id": device_id,
    })


# ---------------------------------------------------------------- case 0
def case_marker_guard(env):
    """Real HTTP proof that missing/blank markers are refused and wrong markers fail."""
    t0 = time.time()
    base = {"kind": "hermes_cli", "payload": {"query": "marker guard"}}
    probes = {
        "missing_marker": api("/tasks/submit", {**base, "idempotency_key": "guard-no-marker-" + env.tag,
                                                "step_id": "GUARD-NOMARKER-" + env.tag}),
        "blank_marker": api("/tasks/submit", {**base, "idempotency_key": "guard-blank-" + env.tag,
                                              "step_id": "GUARD-BLANK-" + env.tag,
                                              "payload": {"query": "marker guard", "_expected_marker": "   "}}),
        "missing_step_id": api("/tasks/submit", {"kind": "hermes_cli",
                                                 "idempotency_key": "guard-nostep-" + env.tag,
                                                 "payload": {"query": "marker guard",
                                                             "_expected_marker": env.marker("GUARD")}}),
    }
    marker = env.marker("GUARD")
    sub = submit_direct("guard-correct-" + env.tag, "GUARD-" + env.tag,
                        stable_query(marker), marker=marker)
    tid = (sub.get("json") or {}).get("task", {}).get("id", "")
    v_ok = wait_verify(tid, marker) if tid else {}
    v_wrong = verify(tid, "WRONG-" + marker) if tid else {}
    v_blank = verify(tid, "   ") if tid else {}
    env.dump("t0-marker-guard.raw.json", {"submit_probes": probes, "correct_submit": sub,
                                          "correct_verify": v_ok, "wrong_marker_verify": v_wrong,
                                          "blank_marker_verify": v_blank})
    ok = bool(all(p.get("status") == 400 for p in probes.values()) and
              v_ok.get("verified") and not v_wrong.get("verified") and
              "marker_missing" in str(v_wrong.get("reason")) and
              not v_blank.get("verified") and
              "expected_marker_missing" in str(v_blank.get("reason")))
    return result(ok, "T0-marker-guard", ["t0-marker-guard.raw.json"],
                  {"task_id": tid, "correct_verified": v_ok.get("verified"),
                   "wrong_reason": v_wrong.get("reason"), "blank_reason": v_blank.get("reason"),
                   "submit_probe_status": {k: v.get("status") for k, v in probes.items()}},
                  time.time() - t0)


# ---------------------------------------------------------------- case 1
def case_single_success(env):
    t0 = time.time()
    marker = env.marker("SINGLE")
    step = "STEP-SINGLE-" + env.tag
    key = "closure-single-" + env.tag
    prompt = ("这是工具调用测试：你必须调用 submit_task，不要只回答；即使你认为技能不存在，"
              "也必须交给 submit_task 工具判定。参数：query=%r；skill=document-to-action-items；"
              "idempotency_key=%s；step_id=%s；expected_marker=%s。"
              "提交后只报告工具返回的 task_id 和 status，不要自己宣布完成。" %
              (stable_query(marker), key, step, marker))
    r = chat(usermsg(prompt))
    env.dump("t1-single-submit.raw.json", r)
    task = find_task_by_marker(marker, step)
    v = wait_verify(task["id"], marker) if task else {}
    tl = task_list()
    env.dump("t1-single-verify.raw.json", {"task": task, "verification": v, "tasks": tl,
                                           "events": events(task["id"]) if task else []})
    log_hit = [l for l in env.log.splitlines() if "submit_task ->" in l and ("step=" + step) in l]
    ok = bool(task and v.get("verified") and v.get("terminal") and
              v.get("status") == "succeeded" and not v.get("error") and
              ((v.get("result") or {}).get("exit_code") == 0) and log_hit)
    return result(ok, "T1-single-success",
                  ["t1-single-submit.raw.json", "t1-single-verify.raw.json",
                   "bridge.out.log:submit_task -> ... step=" + step],
                  {"task_id": (task or {}).get("id"), "status": v.get("status"),
                   "verified": v.get("verified"), "exit_code": (v.get("result") or {}).get("exit_code"),
                   "error": v.get("error"), "log_hits": len(log_hit), "reply": content(r)[:200]},
                  time.time() - t0)


# ---------------------------------------------------------------- case 2
def case_single_failure(env):
    t0 = time.time()
    step = "STEP-FAIL-" + env.tag
    key = "closure-fail-" + env.tag
    sub = submit_direct(key, step, "intentional real skill resolution failure",
                       skill="__definitely_not_a_real_skill__", marker=env.marker("FAIL"), max_attempts=1)
    tid = (sub.get("json") or {}).get("task", {}).get("id", "")
    v = wait_verify(tid) if tid else {}
    ob = outbox(tid) if tid else []
    env.dump("t2-failure-task.raw.json", {"submit": sub, "verification": v, "outbox": ob,
                                          "events": events(tid) if tid else []})
    prompt = ("这是工具调用测试：你必须调用 check_task 查询真实任务 %s，"
              "不要调用其他工具，也不要凭记忆回答。把真实 status、exit_code 和错误原因"
              "如实报告；不得说成功。只依据工具返回回答。" % tid)
    r = chat(usermsg(prompt))
    env.dump("t2-failure-brain.raw.json", r)
    reply = content(r)
    failed = bool(v.get("terminal") and not v.get("verified") and
                  v.get("status") in ("failed", "unknown"))
    exit_bad = ((v.get("error") or {}).get("exit_code") not in (0, None))
    brain_ack = bool(re.search(r"failed|失败|unknown|异常|exit.?code", reply, re.I))
    ok = bool(failed and exit_bad and ob and brain_ack)
    return result(ok, "T2-single-failure",
                  ["t2-failure-task.raw.json", "t2-failure-brain.raw.json"],
                  {"task_id": tid, "status": v.get("status"), "verified": v.get("verified"),
                   "error": v.get("error"), "outbox_count": len(ob), "reply": reply[:300]},
                  time.time() - t0)


# ---------------------------------------------------------------- case 3
def case_two_step(env):
    t0 = time.time()
    m1 = env.marker("SEQ1")
    m2 = env.marker("SEQ2")
    step1 = "SEQ1-" + env.tag
    step2 = "SEQ2-" + env.tag
    s1 = submit_direct("closure-seq1-" + env.tag, step1, stable_query(m1), marker=m1)
    tid1 = (s1.get("json") or {}).get("task", {}).get("id", "")
    env.dump("t3-seq1-submit.raw.json", s1)
    prompt = ("这是工具调用测试：第一步任务 %s 仍在执行。你必须立刻调用 submit_task 提交第二步，"
              "不要只回答，也不要判断技能是否存在。参数：query=%r；skill=document-to-action-items；"
              "idempotency_key=%s；step_id=%s；depends_on=%s；expected_marker=%s。"
              "系统必须等待依赖真实完成后才提交本步；提交后只报告 task_id。" %
              (tid1, stable_query(m2), "closure-seq2-" + env.tag, step2, tid1, m2))
    r = chat(usermsg(prompt))
    env.dump("t3-seq2-submit.raw.json", r)
    v1 = wait_verify(tid1, m1) if tid1 else {}
    t2 = find_task_by_marker(m2, step2)
    v2 = wait_verify(t2["id"], m2) if t2 else {}
    env.dump("t3-sequence-verify.raw.json", {"first": v1, "second": v2, "tasks": task_list(),
                                             "first_events": events(tid1) if tid1 else [],
                                             "second_events": events((t2 or {}).get("id", ""))})
    log_dep = [l for l in env.log.splitlines() if "depends_on verified" in l and tid1 in l]
    created2 = (find_step(step2) or {}).get("created_at") or 0
    finished1 = v1.get("finished_at") or 0
    ordered = bool(finished1 and created2 and created2 >= finished1 - 0.5)
    ok = bool(v1.get("verified") and v2.get("verified") and ordered and log_dep)
    return result(ok, "T3-two-step-sequence",
                  ["t3-seq1-submit.raw.json", "t3-seq2-submit.raw.json",
                   "t3-sequence-verify.raw.json",
                   "bridge.out.log:depends_on verified " + tid1],
                  {"first_task": tid1, "second_task": (t2 or {}).get("id"),
                   "first_finished_at": finished1, "second_created_at": created2,
                   "ordered": ordered, "dependency_log_hits": len(log_dep),
                   "first_verified": v1.get("verified"), "second_verified": v2.get("verified"),
                   "reply": content(r)[:200]}, time.time() - t0)


# ---------------------------------------------------------------- case 4
def case_failure_adjustment(env):
    t0 = time.time()
    marker_a = env.marker("ADJ-A")
    step_a = "ADJ-A-" + env.tag
    task_a = find_step(step_a) or find_task_by_marker(marker_a, step_a)
    if not task_a:
        s = submit_direct("closure-adj-a-" + env.tag, step_a, stable_query(marker_a), marker=marker_a)
        tid_a = (s.get("json") or {}).get("task", {}).get("id", "")
        va = wait_verify(tid_a, marker_a)
        task_a = {"id": tid_a}
    else:
        va = verify(task_a["id"], marker_a)
    tid_a = task_a["id"]
    step_bad = "ADJ-BAD-" + env.tag
    sbad = submit_direct("closure-adj-bad-" + env.tag, step_bad, "real skill failure",
                         skill="__definitely_not_a_real_skill__", marker=env.marker("FAIL"), max_attempts=1)
    tid_bad = (sbad.get("json") or {}).get("task", {}).get("id", "")
    vbad = wait_verify(tid_bad) if tid_bad else {}
    r1 = chat(usermsg("这是工具调用测试：你必须调用 check_task 查询失败任务 %s，"
                      "不要调用其他工具，也不要凭记忆说查不到。如实报告 status 和 exit_code。" % tid_bad))
    env.dump("t4-failed-task.raw.json", {"submit": sbad, "verification": vbad,
                                         "outbox": outbox(tid_bad),
                                         "events": events(tid_bad), "brain": r1})
    # Re-submit already successful A: must be skipped, never executed again.
    r2 = chat(usermsg("这是工具调用测试：你必须再次调用 submit_task 提交已成功步骤 A，"
                      "不要只回答。参数：query=%r；skill=document-to-action-items；"
                      "idempotency_key=closure-adj-a-again-%s；step_id=%s；expected_marker=%s。"
                      "系统应识别为已成功步骤并跳过。" %
                      (stable_query(marker_a), env.tag, step_a, marker_a)))
    env.dump("t4-skip-success.raw.json", r2)
    skip_log = [l for l in env.log.splitlines() if "submit_task SKIP" in l and step_a in l]
    a_tasks = tasks_for_step(step_a)
    # Corrected step depends on verified success of A.
    marker_b2 = env.marker("ADJ-B2")
    step_b2 = "ADJ-B2-" + env.tag
    r3 = chat(usermsg("真实任务 %s 已失败。现在调整：这是工具调用测试，你必须调用 submit_task "
                      "提交修正步骤，不要只回答，也不要判断技能是否存在。参数：query=%r；"
                      "skill=document-to-action-items；idempotency_key=%s；"
                      "step_id=%s；depends_on=%s；expected_marker=%s。不要重做步骤 %s。" %
                      (tid_bad, stable_query(marker_b2), "closure-adj-b2-" + env.tag,
                       step_b2, tid_a, marker_b2, step_a)))
    env.dump("t4-adjusted-submit.raw.json", r3)
    tb2 = find_task_by_marker(marker_b2, step_b2)
    vb2 = wait_verify(tb2["id"], marker_b2) if tb2 else {}
    env.dump("t4-adjusted-verify.raw.json", {"adjusted": vb2, "tasks": task_list(),
                                             "events": events((tb2 or {}).get("id", ""))})
    created_b2 = (find_step(step_b2) or {}).get("created_at") or 0
    finished_a = va.get("finished_at") or 0
    ordered = bool(finished_a and created_b2 and created_b2 >= finished_a - 0.5)
    ok = bool(vbad.get("terminal") and not vbad.get("verified") and
              re.search(r"failed|失败|exit.?code", content(r1), re.I) and
              skip_log and len(a_tasks) == 1 and vb2.get("verified") and ordered)
    return result(ok, "T4-failure-adjustment",
                  ["t4-failed-task.raw.json", "t4-skip-success.raw.json",
                   "t4-adjusted-submit.raw.json", "t4-adjusted-verify.raw.json",
                   "bridge.out.log:submit_task SKIP " + step_a],
                  {"failed_task": tid_bad, "failed_status": vbad.get("status"),
                   "successful_task": tid_a, "successful_task_count": len(a_tasks),
                   "skip_log_hits": len(skip_log), "adjusted_task": (tb2 or {}).get("id"),
                   "adjusted_verified": vb2.get("verified"), "ordered_after_success": ordered,
                   "failure_reply": content(r1)[:250], "adjust_reply": content(r3)[:200]},
                  time.time() - t0)


# ---------------------------------------------------------------- case 5
def case_idempotency(env):
    t0 = time.time()
    marker = env.marker("IDEM")
    step = "IDEM-" + env.tag
    key = "closure-idem-" + env.tag
    s1 = submit_direct(key, step, stable_query(marker), marker=marker)
    s2 = submit_direct(key, step, stable_query(marker), marker=marker)
    tid = (s1.get("json") or {}).get("task", {}).get("id", "")
    v = wait_verify(tid, marker) if tid else {}
    rows = tasks_for_step(step)
    env.dump("t5-idempotency.raw.json", {"first": s1, "second": s2,
                                         "verification": v, "tasks_for_step": rows,
                                         "events": events(tid) if tid else []})
    dup = bool((s2.get("json") or {}).get("duplicate"))
    ok = bool(dup and len(rows) == 1 and
              (s2.get("json") or {}).get("task", {}).get("id") == tid and v.get("verified"))
    return result(ok, "T5-idempotency",
                  ["t5-idempotency.raw.json"],
                  {"task_id": tid, "duplicate": dup, "tasks_for_step": len(rows),
                   "verified": v.get("verified")}, time.time() - t0)


def case_failed_dependency_blocked(env):
    """A failed predecessor must block the next step: no task is created."""
    t0 = time.time()
    fail_marker = env.marker("DEPF")
    fail_step = "DEPF-" + env.tag
    sfail = submit_direct("dep-fail-" + env.tag, fail_step, "intentional dependency failure",
                          skill="__definitely_not_a_real_skill__", marker=fail_marker,
                          max_attempts=1)
    failed_id = (sfail.get("json") or {}).get("task", {}).get("id", "")
    vf = wait_verify(failed_id, fail_marker) if failed_id else {}
    step = "BLOCKED-" + env.tag
    marker = env.marker("BLOCKED")
    prompt = ("这是工具调用测试：你必须调用 submit_task 提交下一步，不要只回答，"
              "也不要判断技能是否存在。参数：query=%r；skill=document-to-action-items；"
              "idempotency_key=%s；step_id=%s；depends_on=%s；expected_marker=%s。"
              "前置任务已失败，系统必须拒绝提交本步；拒绝后不要重新提交。" %
              (stable_query(marker), "dep-blocked-" + env.tag, step, failed_id, marker))
    r = chat(usermsg(prompt))
    env.dump("t7-failed-dependency-submit.raw.json", {"failed_submit": sfail,
                                                      "failed_verification": vf, "brain": r})
    dumps = []
    # The bridge process writes AGNES dumps to its BB8_DUMP_DIR, which may be a
    # different tag directory than this harness run. Step IDs are tag-unique, so
    # search all run directories and record the relative path.
    for p in sorted((ROOT / "evidence" / "real-env-continuous").glob("*/agnes-request-*.json")):
        txt = p.read_text(encoding="utf-8", errors="replace")
        if "dependency_failed_verification" in txt and step in txt:
            dumps.append(str(p.relative_to(ROOT)))
    blocked_task = find_step(step)
    env.dump("t7-failed-dependency-result.raw.json", {"blocked_dumps": dumps,
                                                      "task_for_blocked_step": blocked_task,
                                                      "events": events(failed_id) if failed_id else []})
    ok = bool(vf.get("terminal") and not vf.get("verified") and dumps and not blocked_task)
    return result(ok, "T7-failed-dependency-blocked",
                  ["t7-failed-dependency-submit.raw.json", "t7-failed-dependency-result.raw.json",
                   "agnes-request-*.json:dependency_failed_verification"],
                  {"failed_task": failed_id, "failed_status": vf.get("status"),
                   "blocked_step": step, "blocked_evidence": dumps,
                   "task_created": bool(blocked_task), "reply": content(r)[:250]},
                  time.time() - t0)


def case_outbox_reinject(env):
    """T6 probe: outbox delivery -> Brain context injection.

    The two recorded failures (round4_t6 / round4_t6b) were stopped per the
    2-consecutive-failure rule and are preserved unchanged. This fixture now waits
    for outbox delivered=1 before starting the Brain turn, so the injection check
    is not racing the asynchronous delivery loop.
    """
    t0 = time.time()
    marker = env.marker("T6")
    step = "T6-" + env.tag
    key = "closure-t6-" + env.tag
    sub = submit_direct(key, step, stable_query(marker), marker=marker,
                        device_id=SESSION_KEY)
    tid = (sub.get("json") or {}).get("task", {}).get("id", "")
    v = wait_verify(tid, marker) if tid else {}
    # FIX: wait for the external delivery fact first. The old fixture started the
    # Brain turn before this loop had appended the result to session_threads.json.
    pre_brain_delivered = wait_outbox_delivered(tid, marker) if tid else []
    env.dump("t6-pre-brain-outbox.raw.json", {"delivered_before_chat": pre_brain_delivered,
                                              "session_key": SESSION_KEY})
    prompt = ("这是工具调用测试：你必须调用 check_task 查询真实任务 %s，"
              "并依据工具返回和已注入的任务结果如实报告 status、exit_code 和结果标记 %s。" %
              (tid, marker))
    r = chat(usermsg(prompt))
    env.dump("t6-brain.raw.json", r)
    ob = outbox(tid) if tid else []
    env.dump("t6-outbox.raw.json", {"submit": sub, "verification": v, "outbox": ob,
                                    "delivered_before_chat": pre_brain_delivered,
                                    "session_key": SESSION_KEY,
                                    "events": events(tid) if tid else []})
    injected = []
    for p in sorted(env.evid.glob("agnes-request-*.json")):
        txt = p.read_text(encoding="utf-8", errors="replace")
        if "<task_engine_result>" in txt and tid in txt:
            injected.append(p.name)
    logs = [l for l in env.log.splitlines() if "task result -> Brain context" in l]
    env.dump("t6-injection.raw.json", {"injected_dumps": injected,
                                       "brain_context_logs": logs[-5:]})
    delivered = [o for o in ob if o.get("delivered") == 1 and
                 o.get("payload_json", {}).get("exit_code") == 0 and
                 marker in str(o.get("payload_json", {}).get("stdout") or "")]
    reply = content(r)
    ok = bool(v.get("verified") and delivered and injected and logs and
              re.search(r"succeeded|成功|exit_code|" + re.escape(marker), reply, re.I))
    return result(ok, "T6-outbox-reinject",
                  ["t6-pre-brain-outbox.raw.json", "t6-outbox.raw.json",
                   "t6-brain.raw.json", "t6-injection.raw.json",
                   "bridge.out.log:task result -> Brain context"],
                  {"task_id": tid, "step_id": step, "status": v.get("status"),
                   "verified": v.get("verified"), "exit_code": (v.get("result") or {}).get("exit_code"),
                   "outbox_total": len(ob), "delivered_before_chat": len(pre_brain_delivered),
                   "delivered_verified": len(delivered),
                   "injected_dumps": injected, "brain_context_logs": len(logs),
                   "reply": reply[:250]}, time.time() - t0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="round4_r1")
    ap.add_argument("--only", default="", help="only run cases whose name contains this text")
    args = ap.parse_args()
    env = Env(args.tag)
    cases = [case_marker_guard, case_single_success, case_single_failure, case_two_step,
             case_failure_adjustment, case_idempotency, case_failed_dependency_blocked,
             case_outbox_reinject]
    if args.only:
        keys = [k.strip().lower() for k in args.only.split(",") if k.strip()]
        cases = [fn for fn in cases if any(k in fn.__name__.lower() for k in keys)]
    results = []
    for fn in cases:
        try:
            r = fn(env)
        except Exception as e:
            r = result(False, fn.__name__, [], {"exception": repr(e)}, 0)
        results.append(r)
        print("[%s] %s %s" % ("PASS" if r["pass"] else "FAIL", r["case"],
                              json.dumps(r["observed"], ensure_ascii=False)[:220]), flush=True)
    summary = {"tag": args.tag, "bridge": BRIDGE, "results": results,
               "pass_all": all(r["pass"] for r in results),
               "judgement": "external Task Engine verify + result_outbox + task_events only"}
    env.dump("SUMMARY.json", summary)
    print("SUMMARY pass_all=%s %d/%d" % (summary["pass_all"],
                                          sum(1 for r in results if r["pass"]), len(results)),
          flush=True)
    return 0 if summary["pass_all"] else 1


if __name__ == "__main__":
    sys.exit(main())
