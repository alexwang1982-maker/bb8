# -*- coding: utf-8 -*-
import json
import urllib.request

BASE = "http://127.0.0.1:8900"


def post(path, body, stream=False):
    req = urllib.request.Request(
        BASE + path,
        json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        if stream:
            raw = resp.read().decode("utf-8", "replace")
            chunks = [line for line in raw.splitlines() if line.startswith("data: ")]
            return raw, chunks
        return json.loads(resp.read().decode("utf-8")), []


print("=== non-stream ===")
non_stream, _ = post("/v1/chat/completions", {
    "model": "openmuse",
    "stream": False,
    "messages": [
        {"role": "system", "content": "You are a concise test assistant."},
        {"role": "user", "content": "Reply with exactly: BRIDGE_OK"},
    ],
})
print("NON_STREAM_REPLY:", non_stream.get("choices", [{}])[0].get("message", {}).get("content", "")[:300])
print("NON_STREAM_FINISH:", non_stream.get("choices", [{}])[0].get("finish_reason"))

print("=== stream ===")
raw, chunks = post("/v1/chat/completions", {
    "model": "openmuse",
    "stream": True,
    "messages": [
        {"role": "system", "content": "You are a concise test assistant."},
        {"role": "user", "content": "Reply with exactly: SSE_OK"},
    ],
}, stream=True)
content = ""
for line in chunks:
    payload = line[6:]
    if payload == "[DONE]":
        continue
    obj = json.loads(payload)
    content += obj.get("choices", [{}])[0].get("delta", {}).get("content", "")
print("STREAM_REPLY:", content[:300])
print("STREAM_CHUNKS:", len(chunks))
print("STREAM_DONE:", "data: [DONE]" in raw)
