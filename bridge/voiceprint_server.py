# -*- coding: utf-8 -*-
"""本地声纹识别服务（sherpa-onnx 3dspeaker）。

兼容 xiaozhi-server 的 VoiceprintProvider 接口：
  GET  /voiceprint/health?key=K   -> {"status":"healthy"}
  POST /voiceprint/identify       -> {"speaker_id": "...", "score": 0.85}
       multipart: speaker_ids(逗号分隔) + file(16k mono 16bit WAV)

注册的声纹放在 VOICEPRINT_DIR，每个说话人一个 <speaker_id>.npy（embedding）。
"""
import io, os, glob, wave, sys
import numpy as np
import sherpa_onnx
from aiohttp import web

MODEL = r"G:\openmuse\models\3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx"
VP_DIR = r"G:\openmuse\models\voiceprints"
API_KEY = os.environ.get("VOICEPRINT_KEY", "vp-local-key")
PORT = int(os.environ.get("VOICEPRINT_PORT", "8010"))

_extractor = None


def get_extractor():
    global _extractor
    if _extractor is None:
        cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=MODEL, num_threads=2)
        _extractor = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)
    return _extractor


def load_db():
    db = {}
    for p in glob.glob(os.path.join(VP_DIR, "*.npy")):
        sid = os.path.splitext(os.path.basename(p))[0]
        try:
            db[sid] = np.load(p)
        except Exception as e:
            print("[vp] load fail %s: %s" % (p, e), flush=True)
    return db


def wav_to_samples(data: bytes):
    with wave.open(io.BytesIO(data), "rb") as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise ValueError("need mono 16-bit wav, got ch=%d width=%d" % (w.getnchannels(), w.getsampwidth()))
        sr = w.getframerate()
        raw = w.readframes(w.getnframes())
    x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    return x, sr


def embed(x, sr):
    if sr != 16000:
        raise ValueError("need 16kHz, got %d" % sr)
    if len(x) < 16000 * 0.3:
        raise ValueError("audio too short")
    ex = get_extractor()
    st = ex.create_stream()
    st.accept_waveform(sample_rate=16000, waveform=x)
    st.input_finished()
    if not ex.is_ready(st):
        return None
    return np.array(ex.compute(st), dtype=np.float32)


def cosine(a, b):
    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def _key_from(request):
    auth = request.headers.get("Authorization", "")
    return auth.replace("Bearer ", "").strip() or request.query.get("key", "")


async def health(request):
    if _key_from(request) != API_KEY:
        return web.json_response({"status": "unauthorized"}, status=401)
    return web.json_response({"status": "healthy"})


async def identify(request):
    if _key_from(request) != API_KEY:
        return web.json_response({"error": "unauthorized"}, status=401)

    reader = await request.multipart()
    speaker_ids, audio = [], None
    while True:
        part = await reader.next()
        if part is None:
            break
        if part.name == "speaker_ids":
            txt = (await part.read()).decode("utf-8", "replace")
            speaker_ids = [s.strip() for s in txt.split(",") if s.strip()]
        elif part.name == "file":
            audio = await part.read()

    if not audio:
        return web.json_response({"error": "no file"}, status=400)

    try:
        x, sr = wav_to_samples(audio)
        e = embed(x, sr)
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=400)

    if e is None:
        return web.json_response({"speaker_id": None, "score": 0.0})

    db = load_db()
    best_id, best_score = None, -1.0
    for sid in speaker_ids:
        ref = db.get(sid)
        if ref is None:
            continue
        s = cosine(e, ref)
        if s > best_score:
            best_score, best_id = s, sid

    if best_id is None:
        return web.json_response({"speaker_id": None, "score": 0.0})
    print("[vp] identify -> %s score=%.3f (candidates=%s)" % (best_id, best_score, speaker_ids), flush=True)
    return web.json_response({"speaker_id": best_id, "score": round(best_score, 4)})


async def enroll(request):
    """注册声纹：multipart speaker_id + file(wav) -> 存 <speaker_id>.npy"""
    if _key_from(request) != API_KEY:
        return web.json_response({"error": "unauthorized"}, status=401)

    reader = await request.multipart()
    speaker_id, audio = None, None
    while True:
        part = await reader.next()
        if part is None:
            break
        if part.name == "speaker_id":
            speaker_id = (await part.read()).decode("utf-8", "replace").strip()
        elif part.name == "file":
            audio = await part.read()

    if not speaker_id or not audio:
        return web.json_response({"error": "need speaker_id and file"}, status=400)

    try:
        x, sr = wav_to_samples(audio)
        e = embed(x, sr)
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=400)
    if e is None:
        return web.json_response({"error": "embedding not ready (audio too short/low quality)"}, status=400)

    os.makedirs(VP_DIR, exist_ok=True)
    out = os.path.join(VP_DIR, speaker_id + ".npy")
    np.save(out, e)
    print("[vp] enrolled %s -> %s (dim=%d)" % (speaker_id, out, len(e)), flush=True)
    return web.json_response({"ok": True, "speaker_id": speaker_id})


app = web.Application()
app.router.add_get("/voiceprint/health", health)
app.router.add_post("/voiceprint/identify", identify)
app.router.add_post("/voiceprint/enroll", enroll)

if __name__ == "__main__":
    os.makedirs(VP_DIR, exist_ok=True)
    if not os.path.exists(MODEL):
        print("MODEL MISSING:", MODEL)
        sys.exit(1)
    print("[vp] voiceprint server :%d  model=%s  key=%s" % (PORT, os.path.basename(MODEL), API_KEY), flush=True)
    web.run_app(app, host="127.0.0.1", port=PORT, print=None)
