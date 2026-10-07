"""발화 Segment → OpenAI 전사 (병렬, 재시도, 실패분 보관)."""
from __future__ import annotations

import io
import logging
import re
import threading
import time
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

import numpy as np
import openai

from .config import FALLBACK_TRANSCRIBE_MODEL, Settings
from .vad import SR, Segment

log = logging.getLogger(__name__)

# 무음/잡음에서 STT 모델이 자주 만들어내는 문구(환각) — 짧은 구간에서만 걸러낸다
_HALLUCINATIONS = [
    r"시청해\s*주셔서\s*감사합니다", r"구독(과|\s*와)?\s*좋아요", r"MBC\s*뉴스", r"KBS\s*뉴스",
    r"자막\s*(제공|by)", r"다음\s*영상에서\s*만나요",
    r"thank you for watching", r"please subscribe",
]
_HALLU_RE = re.compile("|".join(_HALLUCINATIONS), re.I)


def to_wav_bytes(audio: np.ndarray) -> bytes:
    pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm)
    return buf.getvalue()


def make_client(settings: Settings, timeout: float = 60) -> openai.OpenAI:
    return openai.OpenAI(api_key=settings.api_key, base_url=settings.base_url or None,
                         max_retries=3, timeout=timeout)


class Transcriber:
    def __init__(self, settings: Settings, context_fn: Callable[[str], str],
                 on_text: Callable[[Segment, str], None], on_status: Callable[[str, str], None],
                 audio_dir: Path | None = None, workers: int = 4):
        self.s = settings
        self.model = settings.transcribe_model
        self.client = make_client(settings)
        self.context_fn = context_fn
        self.on_text = on_text
        self.on_status = on_status        # (level, message)
        self.audio_dir = audio_dir
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="stt")
        self.pending = 0
        self.failed: list[Segment] = []
        self.sent_seconds = 0.0
        self._extras_ok = True            # languages/keywords 파라미터 지원 여부
        self._lock = threading.Lock()
        self._seq = 0

    # ------------------------------------------------------------------ public
    def submit(self, seg: Segment) -> None:
        with self._lock:
            self.pending += 1
            self._seq += 1
            seq = self._seq
        if self.audio_dir is not None:
            try:
                self.audio_dir.mkdir(exist_ok=True)
                (self.audio_dir / f"{seg.t_start:08.1f}_{seg.source}.wav").write_bytes(to_wav_bytes(seg.audio))
            except Exception as e:
                log.warning("audio save: %s", e)
        self.pool.submit(self._job, seg, seq)

    def retry_failed(self) -> int:
        with self._lock:
            segs, self.failed = self.failed, []
        for s in segs:
            self.submit(s)
        return len(segs)

    def wait_idle(self, timeout: float = 120) -> bool:
        end = time.monotonic() + timeout
        while self.pending > 0 and time.monotonic() < end:
            time.sleep(0.2)
        return self.pending == 0

    def shutdown(self) -> None:
        self.pool.shutdown(wait=False, cancel_futures=True)

    # ------------------------------------------------------------------ internal
    def _job(self, seg: Segment, seq: int) -> None:
        try:
            text = None
            for attempt in range(4):
                try:
                    text = self._call(seg)
                    break
                except (openai.APIConnectionError, openai.RateLimitError, openai.InternalServerError, openai.APITimeoutError) as e:
                    wait = (5, 15, 30, 0)[attempt]
                    self.on_status("warn", f"전사 재시도 중… ({type(e).__name__})")
                    if wait:
                        time.sleep(wait)
                except openai.AuthenticationError:
                    self.on_status("error", "API 키가 올바르지 않습니다. 설정을 확인하세요.")
                    break
                except openai.PermissionDeniedError as e:
                    self.on_status("error", f"API 권한 오류: {e.message}")
                    break
            if text is None:
                with self._lock:
                    self.failed.append(seg)
                self.on_status("warn", f"전사 실패 구간 {len(self.failed)}개 (종료 후 재시도)")
                return
            text = self._clean(text, seg)
            if text:
                self.on_text(seg, text)
        except Exception as e:  # 예기치 못한 오류도 세그먼트는 보존
            log.exception("transcribe job")
            with self._lock:
                self.failed.append(seg)
            self.on_status("warn", f"전사 오류: {e}")
        finally:
            with self._lock:
                self.pending -= 1

    def _call(self, seg: Segment) -> str:
        prompt_parts = []
        if self.s.keywords:
            prompt_parts.append("용어: " + ", ".join(self.s.keywords[:50]))
        ctx = self.context_fn(seg.source)
        if ctx:
            prompt_parts.append(ctx)
        kwargs: dict = {
            "model": self.model,
            "file": ("segment.wav", to_wav_bytes(seg.audio), "audio/wav"),
            "response_format": "json",
        }
        if prompt_parts:
            kwargs["prompt"] = "\n".join(prompt_parts)[-800:]
        if self.model == "gpt-transcribe" and self._extras_ok:
            extra = {}
            if self.s.languages:
                extra["languages"] = self.s.languages
            kw = [k.replace("<", "").replace(">", "").strip() for k in self.s.keywords if k.strip()]
            if kw:
                extra["keywords"] = kw[:100]
            if extra:
                kwargs["extra_body"] = extra
        elif len(self.s.languages) == 1 and self.model != "gpt-transcribe":
            kwargs["language"] = self.s.languages[0]

        try:
            r = self.client.audio.transcriptions.create(**kwargs)
        except openai.NotFoundError:
            if self.model != FALLBACK_TRANSCRIBE_MODEL:
                self.on_status("warn", f"'{self.model}' 모델을 쓸 수 없어 {FALLBACK_TRANSCRIBE_MODEL} 로 전환합니다.")
                self.model = FALLBACK_TRANSCRIBE_MODEL
                return self._call(seg)
            raise
        except openai.BadRequestError as e:
            msg = str(e).lower()
            if self._extras_ok and ("languages" in msg or "keywords" in msg):
                log.warning("extras rejected: %s", e)
                self._extras_ok = False
                return self._call(seg)
            if "too short" in msg or "audio_too_short" in msg:
                return ""
            raise
        with self._lock:
            self.sent_seconds += seg.duration
        return getattr(r, "text", "") or ""

    @staticmethod
    def _clean(text: str, seg: Segment) -> str:
        t = re.sub(r"\s+", " ", text).strip()
        if not t:
            return ""
        if seg.duration < 4 and _HALLU_RE.search(t):
            return ""
        # 같은 구절 반복 환각 압축 ("네 네 네 네 네 네" 등)
        t = re.sub(r"(\b.{1,20}?\b)(?:\s*\1){4,}", r"\1", t)
        return t
