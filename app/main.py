"""앱 진입점: pywebview(Edge WebView2) 창 + JS ↔ Python 브리지."""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import audio_capture
from .analyzer import Analyzer, to_markdown
from .config import (APP_TITLE, APP_VERSION, LOG_PATH, TRANSCRIBE_PRICE_PER_MIN, Settings, load_settings,
                     save_settings)
from .report import render_html
from .session import Session, fmt_ts, list_sessions, safe_name
from .transcriber import Transcriber, make_client
from .vad import Segment, Segmenter

log = logging.getLogger("app")


def _resource(rel: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base / rel


def _setup_logging() -> None:
    h = logging.handlers.RotatingFileHandler(LOG_PATH, maxBytes=2_000_000, backupCount=2, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(h)
    if not getattr(sys, "frozen", False):
        root.addHandler(logging.StreamHandler())
    for noisy in ("httpx", "httpcore", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


class Controller:
    """상태: idle → recording → finishing → stopped → analyzing → done"""

    def __init__(self):
        self.settings: Settings = load_settings()
        self.state = "idle"
        self.session: Session | None = None
        self.capture = None
        self.transcriber: Transcriber | None = None
        self.segmenters: dict[str, Segmenter] = {}
        self.analysis: dict | None = None
        self.analysis_html = ""
        self.analysis_cost = 0.0
        self._msgs: list[dict] = []
        self._msg_lock = threading.Lock()
        self._last_msg = ("", 0.0)

    # ---------------------------------------------------------------- messages
    def notify(self, level: str, text: str) -> None:
        # 같은 메시지 폭주 방지
        if self._last_msg[0] == text and time.monotonic() - self._last_msg[1] < 5:
            return
        self._last_msg = (text, time.monotonic())
        log.log(logging.ERROR if level == "error" else logging.INFO, text)
        with self._msg_lock:
            self._msgs.append({"level": level, "text": text})

    def drain(self) -> list[dict]:
        with self._msg_lock:
            m, self._msgs = self._msgs, []
        return m

    # ---------------------------------------------------------------- recording
    def start(self, title: str) -> dict:
        if self.state in ("recording", "finishing", "analyzing"):
            return {"ok": False, "error": "이미 진행 중입니다."}
        s = self.settings
        if not s.api_key:
            return {"ok": False, "error": "먼저 설정에서 OpenAI API 키를 입력하세요."}
        if not (s.capture_system or s.capture_mic):
            return {"ok": False, "error": "캡처할 소리(상대방/내 목소리)를 하나 이상 선택하세요."}

        sess = Session(s.output_dir, title.strip())
        self.analysis, self.analysis_html, self.analysis_cost = None, "", 0.0
        tr = Transcriber(s, context_fn=lambda src: sess.recent_text(None, 250),
                         on_text=self._on_text, on_status=self.notify,
                         audio_dir=(sess.dir / "audio") if s.save_audio else None)

        segs: dict[str, Segmenter] = {}
        segs["system"] = Segmenter("system", self._on_segment)
        sys_seg = segs["system"]
        guard = (lambda: time.monotonic() - sys_seg.last_speech < 0.35) if (s.echo_guard and s.capture_system) else None
        segs["mic"] = Segmenter("mic", self._on_segment, gate=guard)

        def on_audio(source: str, samples, t_wall: float) -> None:
            seg = segs.get(source)
            if seg:
                seg.feed(samples, t_wall)

        cap = audio_capture.make_capture(on_audio, s.capture_system, s.capture_mic, s.mic_device)
        try:
            cap.start()
        except Exception as e:
            log.exception("capture start")
            tr.shutdown()
            return {"ok": False, "error": f"오디오 장치 오류: {e}"}

        self.session, self.transcriber, self.segmenters, self.capture = sess, tr, segs, cap
        for err in getattr(cap, "errors", []):
            self.notify("warn", err)
        self.state = "recording"
        log.info("recording started: %s", sess.dir)
        return {"ok": True, "title": sess.title}

    def _on_segment(self, seg: Segment) -> None:
        if self.session:
            self.session.audio_seconds += seg.duration
        if self.transcriber:
            self.transcriber.submit(seg)

    def _on_text(self, seg: Segment, text: str) -> None:
        if not self.session:
            return
        speaker = (self.settings.my_name or "나") if seg.source == "mic" else "참석자"
        self.session.add(seg.source, speaker, seg.t_start, seg.t_end, text)

    def stop(self) -> dict:
        if self.state != "recording":
            return {"ok": False, "error": "녹음 중이 아닙니다."}
        self.state = "finishing"
        threading.Thread(target=self._finish, daemon=True).start()
        return {"ok": True}

    def _finish(self) -> None:
        try:
            if self.capture:
                self.capture.stop()
            for seg in self.segmenters.values():
                seg.flush()
            tr = self.transcriber
            if tr:
                tr.wait_idle(180)
                if tr.failed:
                    self.notify("info", f"실패한 {len(tr.failed)}개 구간 재전사 중…")
                    tr.retry_failed()
                    tr.wait_idle(180)
                if tr.failed:
                    self.notify("warn", f"{len(tr.failed)}개 구간은 전사하지 못했습니다(네트워크 확인).")
                tr.shutdown()
            if self.session:
                self.session.finish()
        except Exception as e:
            log.exception("finish")
            self.notify("error", f"종료 처리 오류: {e}")
        self.capture = None
        self.state = "stopped"

    # ---------------------------------------------------------------- analysis
    def analyze(self, title: str, context: str) -> dict:
        if self.state not in ("stopped", "done") or not self.session:
            return {"ok": False, "error": "분석할 회의가 없습니다."}
        if not self.session.entries:
            return {"ok": False, "error": "전사된 내용이 없습니다."}
        if not self.settings.api_key:
            return {"ok": False, "error": "API 키가 없습니다."}
        if title.strip():
            self.session.title = title.strip()
        self.state = "analyzing"
        threading.Thread(target=self._analyze, args=(context,), daemon=True).start()
        return {"ok": True}

    def _analyze(self, context: str) -> None:
        sess = self.session
        try:
            an = Analyzer(self.settings, on_progress=lambda m: self.notify("info", m))
            a = an.analyze(sess.transcript_text(), sess.title, context)
            date_str = f"{sess.started_at:%Y-%m-%d %H:%M}"
            dur = fmt_ts(sess.duration())
            self.analysis = a
            self.analysis_cost = an.cost_usd
            self.analysis_html = render_html(a, date_str, dur, sess.title)
            (sess.dir / "analysis.json").write_text(json.dumps(a, ensure_ascii=False, indent=1), "utf-8")
            (sess.dir / "회의록.md").write_text(to_markdown(a, sess.title, date_str, dur), "utf-8")
            (sess.dir / "회의록.html").write_text(render_html(a, date_str, dur, sess.title, standalone=True), "utf-8")
            sess.save_meta({"analysis_title": a.get("title", "")})
            self.notify("ok", f"분석 완료 · 저장: {sess.dir}")
            self.state = "done"
        except Exception as e:
            log.exception("analyze")
            from .net import diagnose
            self.notify("error", f"분석 실패: {diagnose(e)}")
            self.state = "stopped"

    # ---------------------------------------------------------------- history
    def open_session(self, path: str) -> dict:
        if self.state in ("recording", "finishing", "analyzing"):
            return {"ok": False, "error": "진행 중인 작업이 끝난 후 열 수 있습니다."}
        try:
            sess = Session.load(path)
        except Exception as e:
            return {"ok": False, "error": f"열 수 없습니다: {e}"}
        self.session = sess
        self.analysis, self.analysis_html, self.analysis_cost = None, "", 0.0
        ap = sess.dir / "analysis.json"
        if ap.exists():
            self.analysis = json.loads(ap.read_text("utf-8"))
            self.analysis_html = render_html(self.analysis, f"{sess.started_at:%Y-%m-%d %H:%M}",
                                             fmt_ts(sess.duration()), sess.title)
            self.analysis_cost = self.analysis.get("_meta", {}).get("cost_usd", 0.0)
        self.state = "done" if self.analysis else "stopped"
        return {"ok": True, "title": sess.title}

    # ---------------------------------------------------------------- polling
    def poll(self, since_id: int) -> dict:
        sess = self.session
        entries = [x for x in sess.snapshot() if x["id"] >= since_id] if sess else []
        tr = self.transcriber
        sec = tr.sent_seconds if (tr and self.state != "done") else (sess.audio_seconds if sess else 0)
        price = TRANSCRIBE_PRICE_PER_MIN.get(tr.model if tr else self.settings.transcribe_model, 0)
        return {
            "state": self.state,
            "title": sess.title if sess else "",
            "elapsed": sess.duration() if sess else 0,
            "levels": self.capture.levels() if self.capture else {},
            "entries": entries,
            "pending": tr.pending if tr else 0,
            "cost": round(sec / 60 * price + self.analysis_cost, 4),
            "speech_min": round(sec / 60, 1),
            "analysis_html": self.analysis_html if self.state == "done" else "",
            "folder": str(sess.dir) if sess else "",
            "messages": self.drain(),
        }

    def shutdown(self) -> None:
        if self.state == "recording":
            try:
                self.capture.stop()
                for seg in self.segmenters.values():
                    seg.flush()
                if self.transcriber:
                    self.transcriber.wait_idle(15)
                self.session.finish()
            except Exception:
                log.exception("shutdown")


class Api:
    """JS 에서 window.pywebview.api.<method>() 로 호출. 내부 객체는 _ 접두사로 숨긴다."""

    def __init__(self, ctl: Controller):
        self._ctl = ctl

    def app_info(self):
        return {"title": APP_TITLE, "version": APP_VERSION, "windows": sys.platform == "win32"}

    def get_settings(self):
        return self._ctl.settings.to_public()

    def save_settings(self, data: dict):
        s = self._ctl.settings
        if data.get("api_key"):
            s.set_api_key(data["api_key"])
        for k in ("base_url", "proxy", "transcribe_model", "analysis_model", "mic_device", "my_name", "output_dir"):
            if k in data:
                setattr(s, k, str(data[k]).strip())
        for k in ("capture_system", "capture_mic", "echo_guard", "save_audio"):
            if k in data:
                setattr(s, k, bool(data[k]))
        if "languages" in data:
            s.languages = [x.strip() for x in str(data["languages"]).replace(",", " ").split() if x.strip()]
        if "keywords" in data:
            s.keywords = [x.strip() for x in str(data["keywords"]).replace(",", "\n").splitlines() if x.strip()]
        save_settings(s)
        return {"ok": True, "settings": s.to_public()}

    def test_api(self):
        s = self._ctl.settings
        if not s.api_key:
            return {"ok": False, "error": "API 키가 없습니다."}
        try:
            c = make_client(s, timeout=15)
            missing = []
            for m in (s.transcribe_model, s.analysis_model):
                try:
                    c.models.retrieve(m)
                except Exception as e:  # noqa
                    if getattr(e, "status_code", None) != 404:
                        raise  # 네트워크/인증 오류는 실패로 보고
                    missing.append(m)
            if missing:
                return {"ok": True, "warn": f"연결 성공. 단, 이 키로 쓸 수 없는 모델: {', '.join(missing)} (자동으로 대체 모델 사용)"}
            return {"ok": True}
        except Exception as e:
            from .net import describe, diagnose
            log.warning("test_api failed: %r (%s)", e, describe())
            return {"ok": False, "error": diagnose(e), "net": describe()}

    def list_mics(self):
        return audio_capture.list_mics()

    def start(self, title: str = ""):
        return self._ctl.start(title)

    def stop(self):
        return self._ctl.stop()

    def analyze(self, title: str = "", context: str = ""):
        return self._ctl.analyze(title, context)

    def poll(self, since_id: int = 0):
        return self._ctl.poll(int(since_id))

    def history(self):
        return list_sessions(self._ctl.settings.output_dir)

    def open_session(self, path: str):
        return self._ctl.open_session(path)

    def get_markdown(self):
        c = self._ctl
        if not (c.analysis and c.session):
            return ""
        return to_markdown(c.analysis, c.session.title, f"{c.session.started_at:%Y-%m-%d %H:%M}", fmt_ts(c.session.duration()))

    def get_transcript(self):
        return self._ctl.session.transcript_text() if self._ctl.session else ""

    def open_folder(self, path: str = ""):
        p = path or (str(self._ctl.session.dir) if self._ctl.session else self._ctl.settings.output_dir)
        Path(p).mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(p)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", p])
        return True

    def choose_folder(self):
        import webview
        r = webview.windows[0].create_file_dialog(webview.FOLDER_DIALOG, directory=self._ctl.settings.output_dir)
        return r[0] if r else ""


def run() -> None:
    _setup_logging()
    import webview

    ctl = Controller()
    api = Api(ctl)
    debug = bool(os.environ.get("STT_DEBUG"))
    win = webview.create_window(f"{APP_TITLE} {APP_VERSION}", url=str(_resource("app/ui/index.html")), js_api=api,
                                width=1280, height=840, min_size=(920, 620), background_color="#0f1115")
    win.events.closing += lambda: ctl.shutdown()
    log.info("start %s %s", APP_TITLE, APP_VERSION)
    webview.start(gui="edgechromium" if sys.platform == "win32" else None, debug=debug,
                  private_mode=False, storage_path=str(LOG_PATH.parent / "webview"))


if __name__ == "__main__":
    run()
