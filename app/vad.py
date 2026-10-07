"""음성 구간 검출(VAD) + 발화 단위 분할.

무음은 API로 보내지 않으므로 전사 비용이 실제 발화 시간만큼만 든다.
16 kHz mono float32, 30 ms 프레임 기준.
"""
from __future__ import annotations

import collections
import time
from dataclasses import dataclass

import numpy as np

SR = 16000
FRAME = 480  # 30 ms
FRAME_SEC = FRAME / SR

try:
    import webrtcvad  # webrtcvad-wheels
except Exception:  # pragma: no cover
    webrtcvad = None


@dataclass
class Segment:
    source: str          # "system" | "mic"
    t_start: float       # 세션 시작 기준 초
    t_end: float
    audio: np.ndarray    # float32 16k mono

    @property
    def duration(self) -> float:
        return len(self.audio) / SR


class FrameVAD:
    """webrtcvad + 적응형 에너지 임계값. 둘 다 통과해야 음성으로 본다."""

    def __init__(self, aggressiveness: int = 2):
        self._vad = webrtcvad.Vad(aggressiveness) if webrtcvad else None
        self._noise = 1e-4          # 배경 소음 RMS 추정치
        self.last_rms = 0.0

    def __call__(self, frame: np.ndarray) -> bool:
        rms = float(np.sqrt(np.mean(frame * frame)) + 1e-9)
        self.last_rms = rms
        # 소음 바닥: 내려갈 땐 빠르게, 올라갈 땐 천천히 따라간다
        self._noise = self._noise * 0.95 + rms * 0.05 if rms < self._noise else self._noise * 0.999 + rms * 0.001
        loud = rms > max(0.004, self._noise * 2.5)
        if not loud:
            return False
        if self._vad is None:
            return True
        pcm = (np.clip(frame, -1, 1) * 32767).astype(np.int16).tobytes()
        return self._vad.is_speech(pcm, SR)


class Segmenter:
    """프레임을 받아 발화 단위 Segment를 내보낸다.

    - 300 ms pre-roll 로 첫 음절 잘림 방지
    - 700 ms 무음이면 발화 종료
    - 길어지면(12 s~) 짧은 쉼(250 ms)에서도 끊고, 20 s 에서 강제 컷 → 실시간성 유지
    """

    PREROLL = 10
    START_VOTES = 4           # 최근 10프레임 중 4개 이상 음성이면 시작
    END_SILENCE = 23          # ~700 ms
    SOFT_MAX = int(12 / FRAME_SEC)
    SOFT_SILENCE = 8          # ~250 ms
    HARD_MAX = int(20 / FRAME_SEC)
    MIN_SPEECH = int(0.45 / FRAME_SEC)

    def __init__(self, source: str, emit, gate=None):
        self.source = source
        self.emit = emit
        self.gate = gate              # gate() -> True 이면 이 프레임을 무음 취급 (에코 방지)
        self.vad = FrameVAD()
        self._buf = np.zeros(0, dtype=np.float32)
        self._pre = collections.deque(maxlen=self.PREROLL)
        self._votes = collections.deque(maxlen=self.PREROLL)
        self._frames: list[np.ndarray] = []
        self._active = False
        self._silence = 0
        self._speech = 0
        self._t0 = 0.0
        self.clock = 0.0              # 이 소스의 오디오 시간(초)
        self.last_speech = 0.0        # 마지막 음성 프레임의 monotonic 시각(에코 방지용)

    def feed(self, samples: np.ndarray, t_wall: float | None = None) -> None:
        # 오디오 시계가 실제 시간과 1초 이상 어긋나면(장치 재시작 등) 보정
        if t_wall is not None and abs(t_wall - self.clock) > 1.0 and not self._active:
            self.clock = t_wall
        self._buf = np.concatenate([self._buf, samples]) if self._buf.size else samples
        n = len(self._buf) // FRAME
        for i in range(n):
            self._process(self._buf[i * FRAME:(i + 1) * FRAME])
        self._buf = self._buf[n * FRAME:].copy()

    def _process(self, frame: np.ndarray) -> None:
        speech = self.vad(frame)
        if speech:
            self.last_speech = time.monotonic()
        if speech and self.gate is not None and self.gate():
            speech = False
        self.clock += FRAME_SEC

        if not self._active:
            self._pre.append(frame)
            self._votes.append(speech)
            if sum(self._votes) >= self.START_VOTES:
                self._active = True
                self._frames = list(self._pre)
                self._t0 = self.clock - len(self._frames) * FRAME_SEC
                self._silence = 0
                self._speech = sum(self._votes)
                self._pre.clear()
                self._votes.clear()
            return

        self._frames.append(frame)
        if speech:
            self._speech += 1
            self._silence = 0
        else:
            self._silence += 1
        n = len(self._frames)
        if (self._silence >= self.END_SILENCE
                or (n >= self.SOFT_MAX and self._silence >= self.SOFT_SILENCE)
                or n >= self.HARD_MAX):
            self._finish()

    def flush(self) -> None:
        if self._active:
            self._finish()

    def _finish(self) -> None:
        keep_tail = 7  # 끝 무음은 ~200 ms 만 남긴다
        frames = self._frames
        if self._silence > keep_tail:
            frames = frames[: len(frames) - (self._silence - keep_tail)]
        speech = self._speech
        self._active = False
        self._frames = []
        self._silence = 0
        self._speech = 0
        if speech < self.MIN_SPEECH or not frames:
            return
        audio = np.concatenate(frames).astype(np.float32)
        self.emit(Segment(self.source, self._t0, self._t0 + len(audio) / SR, audio))
