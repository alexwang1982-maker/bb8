"""Thread-safe audio source gate for TTS playback.

The gate separates microphone frames that may enter the normal ASR path from
frames that may only be inspected by the trusted barge-in path.  It uses a
generation counter so frames queued before a reset can never be consumed after
that reset, even when the ASR consumer thread races with the event loop.
"""
from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from typing import Dict, Optional, Tuple


@dataclass(frozen=True)
class GatedFrame:
    generation: int
    frame: bytes
    source: str
    enqueued_at: float


MAX_BARGE_QUEUE_FRAMES = 64  # 约 1 秒音频；防止 barge-in 关闭时队列无限增长


class AudioSourceGate:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._generation = 0
        self._normal_open = True
        self._reason = "idle"
        self.normal_queue: "queue.Queue" = queue.Queue()
        self.barge_queue: "queue.Queue" = queue.Queue()
        self.counters: Dict[str, int] = {
            "normal_enqueued": 0,
            "barge_enqueued": 0,
            "stale_dropped": 0,
            "barge_queue_dropped": 0,
            "generations": 0,
        }

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    @property
    def normal_open(self) -> bool:
        with self._lock:
            return self._normal_open

    def state(self) -> Dict[str, object]:
        with self._lock:
            return {
                "generation": self._generation,
                "normal_open": self._normal_open,
                "reason": self._reason,
                "counters": dict(self.counters),
            }

    @staticmethod
    def _drain(q: "queue.Queue") -> int:
        dropped = 0
        while True:
            try:
                q.get_nowait()
                dropped += 1
            except queue.Empty:
                return dropped

    # 调用方必须已持有 self._lock；状态读取、generation 递增、队列清理和
    # normal_open 保留在同一个临界区完成，避免并发 suppress/open 被覆盖。
    def _rotate_locked(self, reason: str, normal_open: Optional[bool]) -> int:
        self._generation += 1
        self.counters["generations"] += 1
        if normal_open is not None:
            self._normal_open = bool(normal_open)
        self._reason = reason
        self._drain(self.normal_queue)
        self._drain(self.barge_queue)
        return self._generation

    def suppress(self, reason: str) -> int:
        """Block normal ASR ingress; frames go only to the barge-in queue."""
        with self._lock:
            return self._rotate_locked(reason, False)

    def open(self, reason: str) -> int:
        """Allow normal ASR ingress after a trusted listen/barge-in event."""
        with self._lock:
            return self._rotate_locked(reason, True)

    def rotate(self, reason: str) -> int:
        """Invalidate in-flight frames without changing source permission."""
        with self._lock:
            current = self._normal_open
            return self._rotate_locked(reason, current)

    def seal(self) -> int:
        """Drain pending frames without rotating; return the still-valid generation.

        Used by listen-stop finalization: the audio already copied for recognition
        keeps its source generation, while queued frames from the same round are
        discarded. A later reset/abort still rotates and invalidates that result.
        """
        with self._lock:
            current = self._generation
            self._drain(self.normal_queue)
            self._drain(self.barge_queue)
            return current

    def put(self, frame: bytes, source: str = "microphone") -> Tuple[str, GatedFrame]:
        with self._lock:
            item = GatedFrame(
                generation=self._generation,
                frame=bytes(frame),
                source=source,
                enqueued_at=time.monotonic(),
            )
            if self._normal_open:
                self.normal_queue.put(item)
                self.counters["normal_enqueued"] += 1
                return "normal", item
            if self.barge_queue.qsize() >= MAX_BARGE_QUEUE_FRAMES:
                try:
                    self.barge_queue.get_nowait()
                    self.counters["barge_queue_dropped"] += 1
                except queue.Empty:
                    pass
            self.barge_queue.put(item)
            self.counters["barge_enqueued"] += 1
            return "barge", item

    def is_current(self, generation: Optional[int]) -> bool:
        # None 表示“没有可核验的音频来源”，绝不是有效授权，必须 fail-closed。
        if generation is None:
            return False
        with self._lock:
            return generation == self._generation

    def note_stale(self) -> None:
        with self._lock:
            self.counters["stale_dropped"] += 1

    def drain_all(self) -> None:
        with self._lock:
            self._drain(self.normal_queue)
            self._drain(self.barge_queue)


def unpack_gated_frame(item) -> Tuple[Optional[int], bytes, str]:
    """Accept both new GatedFrame tuples and legacy raw PCM frames."""
    if isinstance(item, GatedFrame):
        return item.generation, item.frame, item.source
    if isinstance(item, tuple) and len(item) == 3:
        generation, frame, source = item
        return generation, bytes(frame), str(source)
    return None, bytes(item), "legacy"
