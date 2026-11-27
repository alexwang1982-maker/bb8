# -*- coding: utf-8 -*-
"""注册声纹：把 16k 单声道 WAV 提取成 embedding 存到 voiceprints/<id>.npy。

用法:
  python enroll_voice.py <speaker_id> <wav1> [wav2 ...]
多个 wav 会取平均，更稳。
"""
import sys, os, io, wave, glob
import numpy as np
import sherpa_onnx

MODEL = r"G:\openmuse\models\3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx"
VP_DIR = r"G:\openmuse\models\voiceprints"


def wav_to_samples(path):
    with wave.open(path, "rb") as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise ValueError("%s: need mono 16-bit, got ch=%d width=%d" % (path, w.getnchannels(), w.getsampwidth()))
        sr = w.getframerate()
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0, sr


def embed(x, sr):
    if sr != 16000:
        raise ValueError("need 16kHz, got %d" % sr)
    cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=MODEL, num_threads=2)
    ex = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)
    st = ex.create_stream()
    st.accept_waveform(sample_rate=16000, waveform=x)
    st.input_finished()
    if not ex.is_ready(st):
        return None
    return np.array(ex.compute(st), dtype=np.float32)


def main():
    if len(sys.argv) < 3:
        print("usage: enroll_voice.py <speaker_id> <wav1> [wav2 ...]")
        return 1
    sid = sys.argv[1]
    paths = sys.argv[2:]
    embs = []
    for p in paths:
        try:
            x, sr = wav_to_samples(p)
            e = embed(x, sr)
            if e is not None:
                embs.append(e)
                print("  ok:", os.path.basename(p), "len=%.1fs" % (len(x)/sr))
            else:
                print("  skip(not ready):", p)
        except Exception as exc:
            print("  fail:", p, "->", exc)
    if not embs:
        print("no valid audio")
        return 2
    avg = np.mean(np.stack(embs), axis=0)
    os.makedirs(VP_DIR, exist_ok=True)
    out = os.path.join(VP_DIR, sid + ".npy")
    np.save(out, avg)
    print("saved:", out, "from %d sample(s), dim=%d" % (len(embs), len(avg)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
