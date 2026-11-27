# -*- coding: utf-8 -*-
"""Real-environment camera + Qwen3-VL probe against the deployed bridge.py."""
from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path

BRIDGE = Path(r"G:\openmuse\xiaozhi-server\bridge\bridge.py")
sys.path.insert(0, str(BRIDGE.parent))
sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location("bb8_bridge_real_probe", BRIDGE)
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)

print("CAM_URLS=" + json.dumps(bridge.CAM_URLS, ensure_ascii=False), flush=True)
print("VISION_MODEL=" + bridge.SF_VISION_MODEL, flush=True)
print("CAPTURE_TIMEOUT=" + str(bridge.CAPTURE_TIMEOUT), flush=True)
print("SF_VISION_TIMEOUT=" + str(bridge.SF_VISION_TIMEOUT), flush=True)

t0 = time.time()
frame = bridge.capture_image()
print("CAPTURE_OK=" + str(frame is not None), flush=True)
print("CAPTURE_BYTES=" + str(len(frame) if frame else 0), flush=True)
print("CAPTURE_SECONDS=%.3f" % (time.time() - t0), flush=True)

if frame:
    t1 = time.time()
    question = "你看到了什么"
    try:
        reply = bridge.vision_look(question)
        print("VLM_SECONDS=%.3f" % (time.time() - t1), flush=True)
        print("VLM_REPLY=" + str(reply).strip(), flush=True)
        print("VLM_IS_FAIL_MSG=" + str(str(reply).strip() == bridge.VISION_FAIL_MSG), flush=True)
    except Exception as exc:
        print("VLM_EXCEPTION=" + repr(exc), flush=True)
