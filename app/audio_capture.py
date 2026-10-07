"""오디오 캡처.

Windows: WASAPI loopback(스피커로 나오는 화상회의 음성 = Zoom/Teams/Meet 등 앱 무관) + 마이크.
macOS: ScreenCaptureKit 도우미(syscap, Swift) 로 시스템 소리 + sounddevice 마이크.
Linux: 개발용으로 마이크만 지원.

콜백 스레드에서는 바이트를 큐에 넣기만 하고, 변환/리샘플/VAD는 소스별 워커 스레드에서 처리한다.
"""
from __future__ import annotations

import logging
import queue
import sys
import threading
import time
from typing import Callable

import numpy as np

from .vad import SR

log = logging.getLogger(__name__)

try:
    import soxr
except Exception:  # pragma: no cover
    soxr = None

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
if IS_WIN:
    import pyaudiowpatch as pyaudio  # type: ignore
else:
    pyaudio = None
    try:
        import sounddevice as sd  # type: ignore
    except Exception:  # pragma: no cover
        sd = None


class _Resampler:
    def __init__(self, in_rate: int):
        self.in_rate = int(in_rate)
        self._rs = soxr.ResampleStream(self.in_rate, SR, 1, dtype="float32", quality="HQ") if (soxr and self.in_rate != SR) else None
        self._frac = 0.0

    def __call__(self, mono: np.ndarray) -> np.ndarray:
        if self.in_rate == SR:
            return mono
        if self._rs is not None:
            return self._rs.resample_chunk(mono)
        # soxr 미설치 시 선형보간 fallback
        n_out = int(len(mono) * SR / self.in_rate)
        if n_out <= 0:
            return np.zeros(0, np.float32)
        x = np.linspace(0, len(mono) - 1, n_out)
        return np.interp(x, np.arange(len(mono)), mono).astype(np.float32)


class SourceWorker:
    """원시 PCM(float32 interleaved) → 16k mono → on_audio 콜백."""

    def __init__(self, name: str, rate: int, channels: int, on_audio: Callable[[str, np.ndarray, float], None], t0: float):
        self.name = name
        self.channels = channels
        self.rs = _Resampler(rate)
        self.on_audio = on_audio
        self.t0 = t0
        self.q: queue.Queue[bytes | None] = queue.Queue(maxsize=2000)
        self.level = 0.0
        self._stop = False
        self._th = threading.Thread(target=self._run, name=f"audio-{name}", daemon=True)
        self._th.start()

    def push(self, data: bytes) -> None:
        try:
            self.q.put_nowait(data)
        except queue.Full:
            pass  # 과부하 시 드롭(실시간성 우선)

    def stop(self) -> None:
        self._stop = True
        self.q.put(None)
        self._th.join(timeout=3)

    def _run(self) -> None:
        idle_since = time.monotonic()
        while True:
            try:
                data = self.q.get(timeout=0.1)
            except queue.Empty:
                data = b""
            if data is None or (self._stop and self.q.empty()):
                break
            now = time.monotonic()
            if not data:
                # WASAPI loopback 은 재생음이 없으면 콜백이 오지 않는다 → 무음을 채워 발화 종료를 감지
                gap = now - idle_since
                if gap >= 0.1:
                    self.level *= 0.5
                    self.on_audio(self.name, np.zeros(int(SR * gap), np.float32), now - self.t0)
                    idle_since = now
                continue
            idle_since = now
            x = np.frombuffer(data, dtype=np.float32)
            if self.channels > 1:
                x = x.reshape(-1, self.channels).mean(axis=1)
            y = self.rs(x.astype(np.float32, copy=False))
            if y.size:
                self.level = max(self.level * 0.7, float(np.abs(y).max()))
                self.on_audio(self.name, y, now - self.t0)


# --------------------------------------------------------------------------- Windows
class WindowsCapture:
    def __init__(self, on_audio, capture_system: bool, capture_mic: bool, mic_device: str = ""):
        self.on_audio = on_audio
        self.want_system = capture_system
        self.want_mic = capture_mic
        self.mic_device = mic_device
        self.pa = pyaudio.PyAudio()
        self.streams: dict[str, tuple] = {}
        self.workers: dict[str, SourceWorker] = {}
        self.t0 = time.monotonic()
        self._watch_stop = threading.Event()
        self._loopback_name = ""
        self.errors: list[str] = []

    # --- device helpers
    def _wasapi(self):
        return self.pa.get_host_api_info_by_type(pyaudio.paWASAPI)

    def _default_loopback(self):
        try:
            return self.pa.get_default_wasapi_loopback()
        except Exception:
            wasapi = self._wasapi()
            spk = self.pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
            for lb in self.pa.get_loopback_device_info_generator():
                if spk["name"] in lb["name"]:
                    return lb
        return None

    def _mic_info(self):
        wasapi = self._wasapi()
        if self.mic_device:
            for i in range(self.pa.get_device_count()):
                d = self.pa.get_device_info_by_index(i)
                if d["hostApi"] == wasapi["index"] and d["maxInputChannels"] > 0 and not d.get("isLoopbackDevice") and d["name"] == self.mic_device:
                    return d
        idx = wasapi.get("defaultInputDevice", -1)
        return self.pa.get_device_info_by_index(idx) if idx is not None and idx >= 0 else None

    @staticmethod
    def list_mics() -> list[str]:
        pa = pyaudio.PyAudio()
        try:
            wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
            out = []
            for i in range(pa.get_device_count()):
                d = pa.get_device_info_by_index(i)
                if d["hostApi"] == wasapi["index"] and d["maxInputChannels"] > 0 and not d.get("isLoopbackDevice"):
                    out.append(d["name"])
            return out
        finally:
            pa.terminate()

    # --- stream control
    def _open(self, name: str, info, pa=None) -> None:
        # 소스마다 별도 PyAudio 인스턴스 → 루프백 재시작 시 마이크 스트림에 영향 없음
        pa = pa or pyaudio.PyAudio()
        rate = int(info["defaultSampleRate"])
        ch = max(1, min(2, int(info["maxInputChannels"])))
        worker = SourceWorker(name, rate, ch, self.on_audio, self.t0)

        def cb(in_data, frame_count, time_info, status):
            worker.push(in_data)
            return (None, pyaudio.paContinue)

        try:
            stream = pa.open(format=pyaudio.paFloat32, channels=ch, rate=rate, input=True,
                             input_device_index=info["index"], frames_per_buffer=int(rate * 0.05),
                             stream_callback=cb)
            stream.start_stream()
        except Exception:
            worker.stop()
            if pa is not self.pa:
                pa.terminate()
            raise
        self.streams[name] = (pa, stream, info)
        self.workers[name] = worker
        log.info("opened %s: %s @%d ch=%d", name, info["name"], rate, ch)

    def _close(self, name: str) -> None:
        s = self.streams.pop(name, None)
        if s:
            pa, stream, _ = s
            try:
                stream.stop_stream()
                stream.close()
            except Exception:
                pass
            if pa is not self.pa:
                try:
                    pa.terminate()
                except Exception:
                    pass
        w = self.workers.pop(name, None)
        if w:
            w.stop()

    def start(self) -> None:
        if self.want_system:
            lb = self._default_loopback()
            if lb:
                self._open("system", lb)
                self._loopback_name = lb["name"]
            else:
                self.errors.append("스피커(루프백) 장치를 찾지 못했습니다.")
        if self.want_mic:
            mic = self._mic_info()
            if mic:
                try:
                    self._open("mic", mic)
                except Exception as e:
                    self.errors.append(f"마이크를 열 수 없습니다: {e}")
            else:
                self.errors.append("마이크 장치를 찾지 못했습니다.")
        if not self.streams:
            raise RuntimeError(" / ".join(self.errors) or "오디오 장치를 열지 못했습니다.")
        threading.Thread(target=self._watchdog, daemon=True).start()

    def _watchdog(self) -> None:
        """회의 중 이어폰 연결 등으로 기본 출력 장치가 바뀌면 루프백을 자동으로 다시 연다."""
        while not self._watch_stop.wait(3.0):
            if not self.want_system:
                continue
            try:
                # 장치 목록 갱신을 위해 새 인스턴스로 조회
                pa2 = pyaudio.PyAudio()
                lb = None
                try:
                    lb = pa2.get_default_wasapi_loopback()
                except Exception:
                    pass
                if lb and lb["name"] != self._loopback_name:
                    log.info("output device changed: %s -> %s", self._loopback_name, lb["name"])
                    self._close("system")
                    self._open("system", lb, pa2)
                    self._loopback_name = lb["name"]
                else:
                    pa2.terminate()
            except Exception as e:
                log.warning("watchdog: %s", e)

    def levels(self) -> dict[str, float]:
        return {k: w.level for k, w in self.workers.items()}

    def stop(self) -> None:
        self._watch_stop.set()
        for name in list(self.streams):
            self._close(name)
        try:
            self.pa.terminate()
        except Exception:
            pass


# --------------------------------------------------------------------------- 개발용(mac/linux)
class DevCapture:
    def __init__(self, on_audio, capture_system: bool, capture_mic: bool, mic_device: str = ""):
        self.on_audio = on_audio
        self.want_mic = capture_mic
        self.mic_device = mic_device or None
        self.t0 = time.monotonic()
        self.workers: dict[str, SourceWorker] = {}
        self.errors: list[str] = []
        self._stream = None
        if capture_system:
            self.errors.append("시스템 오디오 캡처는 Windows 에서만 지원됩니다.")

    @staticmethod
    def list_mics() -> list[str]:
        if sd is None:
            return []
        return [d["name"] for d in sd.query_devices() if d["max_input_channels"] > 0]

    def start(self) -> None:
        if not self.want_mic or sd is None:
            raise RuntimeError("사용 가능한 입력 장치가 없습니다.")
        info = sd.query_devices(self.mic_device, "input")
        rate = int(info["default_samplerate"])
        w = SourceWorker("mic", rate, 1, self.on_audio, self.t0)
        self.workers["mic"] = w
        self._stream = sd.InputStream(device=self.mic_device, channels=1, samplerate=rate, dtype="float32",
                                      blocksize=int(rate * 0.05), callback=lambda d, f, t, s: w.push(d.tobytes()))
        self._stream.start()

    def levels(self) -> dict[str, float]:
        return {k: w.level for k, w in self.workers.items()}

    def stop(self) -> None:
        if self._stream:
            self._stream.stop()
            self._stream.close()
        for w in self.workers.values():
            w.stop()


class MacCapture(DevCapture):
    """시스템 소리는 syscap 도우미 프로세스(16 kHz mono float32 stdout)로, 마이크는 sounddevice 로 받는다."""

    PERM_MSG = ("시스템 소리 녹음 권한이 필요합니다. 시스템 설정 → 개인정보 보호 및 보안 → "
                "‘화면 및 시스템 오디오 녹음’에서 Hiplaza 회의록을 켠 뒤 앱을 다시 실행하세요.")

    def __init__(self, on_audio, capture_system: bool, capture_mic: bool, mic_device: str = ""):
        super().__init__(on_audio, False, capture_mic, mic_device)
        self.want_system = capture_system
        self._proc = None

    @staticmethod
    def helper_path() -> str:
        from pathlib import Path
        base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent / "macos"))
        return str(base / "syscap")

    def _start_system(self) -> None:
        import subprocess
        proc = subprocess.Popen([self.helper_path()], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, bufsize=0)
        status: list[str] = []
        ready = threading.Event()

        def read_err():
            for line in iter(proc.stderr.readline, b""):
                msg = line.decode("utf-8", "replace").strip()
                log.info("syscap: %s", msg)
                if not ready.is_set():
                    status.append(msg)
                    ready.set()

        threading.Thread(target=read_err, daemon=True).start()
        if not ready.wait(10) or not status or status[0] != "READY":
            proc.kill()
            st = status[0] if status else "timeout"
            if st == "ERR_PERMISSION":
                subprocess.Popen(["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"])
                raise PermissionError(self.PERM_MSG)
            raise RuntimeError(f"시스템 소리 캡처 실패: {st}")

        w = SourceWorker("system", SR, 1, self.on_audio, self.t0)
        self.workers["system"] = w
        self._proc = proc

        def read_out():
            while True:
                chunk = proc.stdout.read(3200)   # 50 ms
                if not chunk:
                    break
                w.push(chunk[: len(chunk) // 4 * 4])
        threading.Thread(target=read_out, daemon=True).start()

    def start(self) -> None:
        if self.want_system:
            try:
                self._start_system()
            except Exception as e:
                self.errors.append(str(e))
        if self.want_mic and sd is not None:
            try:
                super().start()
            except Exception as e:
                self.errors.append(f"마이크를 열 수 없습니다: {e} (시스템 설정 → 개인정보 보호 및 보안 → 마이크 확인)")
        if not self.workers:
            raise RuntimeError(" / ".join(self.errors) or "오디오 장치를 열지 못했습니다.")

    def stop(self) -> None:
        if self._proc:
            try:
                self._proc.stdin.close()
                self._proc.terminate()
                self._proc.wait(3)
            except Exception:
                self._proc.kill()
            self._proc = None
        super().stop()


class FileCapture:
    """테스트용: WAV 파일을 실제 시간(또는 speed 배속)으로 흘려보낸다. 환경변수 STT_TEST_FILE."""

    def __init__(self, path: str, on_audio, speed: float = 1.0):
        import wave
        with wave.open(path, "rb") as wf:
            self.rate = wf.getframerate()
            ch = wf.getnchannels()
            raw = wf.readframes(wf.getnframes())
            x = np.frombuffer(raw, np.int16).astype(np.float32) / 32768
            self.data = x.reshape(-1, ch).mean(axis=1) if ch > 1 else x
        self.on_audio = on_audio
        self.speed = speed
        self.t0 = time.monotonic()
        self.workers: dict[str, SourceWorker] = {}
        self.errors: list[str] = []
        self._stop = threading.Event()

    @staticmethod
    def list_mics() -> list[str]:
        return []

    def start(self) -> None:
        w = SourceWorker("system", self.rate, 1, self.on_audio, self.t0)
        self.workers["system"] = w

        def run():
            step = int(self.rate * 0.05)
            for i in range(0, len(self.data), step):
                if self._stop.is_set():
                    break
                w.push(self.data[i:i + step].astype(np.float32).tobytes())
                time.sleep(0.05 / self.speed)
        threading.Thread(target=run, daemon=True).start()

    def levels(self):
        return {k: w.level for k, w in self.workers.items()}

    def stop(self) -> None:
        self._stop.set()
        for w in self.workers.values():
            w.stop()


def make_capture(on_audio, capture_system: bool, capture_mic: bool, mic_device: str = ""):
    import os
    test = os.environ.get("STT_TEST_FILE")
    if test:
        return FileCapture(test, on_audio, float(os.environ.get("STT_TEST_SPEED", "1")))
    cls = WindowsCapture if IS_WIN else MacCapture if IS_MAC else DevCapture
    return cls(on_audio, capture_system, capture_mic, mic_device)


def list_mics() -> list[str]:
    try:
        return (WindowsCapture if IS_WIN else DevCapture).list_mics()
    except Exception as e:
        log.warning("list_mics: %s", e)
        return []
