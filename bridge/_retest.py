import urllib.request, json, time
base = "http://127.0.0.1:8890"
tok = open(r"G:\openmuse\data\server_token", "r", encoding="utf-8").read().strip()
h = {"Authorization": "Bearer " + tok, "Content-Type": "application/json"}

def post(path, body):
    req = urllib.request.Request(base + path, json.dumps(body).encode(), headers=h, method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())

def get(path):
    req = urllib.request.Request(base + path, headers=h)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())

t = post("/api/threads", {"title": "bridge-retest"})
tid = t.get("id") or t.get("thread", {}).get("id")
r = post("/api/threads/%s/send" % tid, {"text": "reply with exactly: OK"})
sent_ts = (r.get("event") or {}).get("ts", "")
deadline = time.time() + 45
result = "TIMEOUT"
while time.time() < deadline:
    time.sleep(2)
    evs = get("/api/threads/%s/events?limit=30" % tid)
    meta = evs.get("thread", {})
    news = [e for e in evs.get("events", []) if (e.get("ts") or "") >= sent_ts and e.get("type") in ("assistant", "notice")]
    if news and not meta.get("busy"):
        e = news[-1]
        result = "ASSISTANT: %s" % (e.get("text") or "")[:200] if e["type"] == "assistant" else "NOTICE: %s" % (e.get("text") or "")[:300]
        break
print(result)
