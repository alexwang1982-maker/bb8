# -*- coding: utf-8 -*-
"""C 方案：无硬件的电量估算提醒（2026-11-22）。

背景：BB8 18650 电池没有接 ESP32 ADC，做不了真实电量检测。
用户选 C：纯软件按"累计对话轮数"估算使用量，到阈值语音提醒"该充电了"。
注意：这不是真测电压，只是使用量估算，仅作提醒，不精确。

状态存 JSON 文件（按设备 key 记轮数 + 上次提醒时间），重启不丢。
开关：POWER_REMINDER_ENABLED=0 关闭；阈值：POWER_REMINDER_TURNS（默认 25）。
"""
import json, os, time
from pathlib import Path

_STATE_FILE = Path(os.environ.get("POWER_REMINDER_STATE",
                                  r"G:\openmuse\xiaozhi-server\bridge\power_reminder_state.json"))
_TURNS = int(os.environ.get("POWER_REMINDER_TURNS", "25"))      # 每 25 轮聊天提醒一次充电
_REMIND_MSG = "话说你用了有一阵子了，方便的话给小智充充电吧，免得等会儿没电。"

_state = None


def _load():
    global _state
    if _state is not None:
        return _state
    try:
        _state = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        _state = {}
    return _state


def _save():
    try:
        _STATE_FILE.write_text(json.dumps(_state, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def note_turn(key):
    """每轮对话调用一次；返回 True 表示这轮该附加充电提醒。"""
    if os.environ.get("POWER_REMINDER_ENABLED", "1") == "0":
        return False
    s = _load()
    rec = s.get(key) or {"turns": 0, "last_remind": 0}
    rec["turns"] = int(rec.get("turns", 0)) + 1
    due = rec["turns"] >= _TURNS
    if due:
        rec["turns"] = 0
        rec["last_remind"] = time.time()
    s[key] = rec
    _save()
    return due


def reminder_text():
    return _REMIND_MSG


if __name__ == "__main__":
    import sys, io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    k = "test_key"
    print("阈值:", _TURNS, "轮；提醒语:", _REMIND_MSG)
    s = _load(); s.pop(k, None); _save()
    for i in range(1, _TURNS + 2):
        if note_turn(k):
            print("第%d轮 -> 触发提醒" % i)
