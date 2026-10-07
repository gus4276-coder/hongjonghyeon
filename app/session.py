"""회의 세션: 전사 결과 보관 + 실시간 디스크 기록(중간에 꺼져도 유실 없음)."""
from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path


@dataclass
class Entry:
    id: int
    source: str
    speaker: str
    t_start: float
    t_end: float
    text: str


def fmt_ts(sec: float) -> str:
    sec = int(max(0, sec))
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def safe_name(s: str) -> str:
    return re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", s).strip()[:60] or "회의"


class Session:
    def __init__(self, output_dir: str, title: str = ""):
        self.started_at = datetime.now()
        self.title = title or f"회의 {self.started_at:%Y-%m-%d %H:%M}"
        self.dir = Path(output_dir) / f"{self.started_at:%Y-%m-%d_%H%M%S}"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.entries: list[Entry] = []
        self.version = 0
        self.audio_seconds = 0.0
        self.ended_at: datetime | None = None
        self._lock = threading.Lock()
        self._next_id = 0
        self._live = open(self.dir / "transcript.live.jsonl", "a", encoding="utf-8")

    def add(self, source: str, speaker: str, t_start: float, t_end: float, text: str) -> Entry:
        with self._lock:
            e = Entry(self._next_id, source, speaker, round(t_start, 2), round(t_end, 2), text)
            self._next_id += 1
            # 병렬 전사로 도착 순서가 섞일 수 있으므로 시작 시각 기준 삽입
            i = len(self.entries)
            while i > 0 and self.entries[i - 1].t_start > e.t_start:
                i -= 1
            self.entries.insert(i, e)
            self.version += 1
            self._live.write(json.dumps(asdict(e), ensure_ascii=False) + "\n")
            self._live.flush()
            return e

    def recent_text(self, source: str | None = None, chars: int = 300) -> str:
        with self._lock:
            parts, n = [], 0
            for e in reversed(self.entries):
                if source and e.source != source:
                    continue
                parts.append(e.text)
                n += len(e.text)
                if n >= chars:
                    break
        return " ".join(reversed(parts))[-chars:]

    def snapshot(self) -> list[dict]:
        with self._lock:
            return [asdict(e) for e in self.entries]

    def transcript_text(self) -> str:
        with self._lock:
            return "\n".join(f"[{fmt_ts(e.t_start)}] {e.speaker}: {e.text}" for e in self.entries)

    def duration(self) -> float:
        end = self.ended_at or datetime.now()
        return (end - self.started_at).total_seconds()

    def finish(self) -> None:
        self.ended_at = datetime.now()
        try:
            self._live.close()
        except Exception:
            pass
        (self.dir / "전사본.txt").write_text(
            f"{self.title}\n{self.started_at:%Y-%m-%d %H:%M} ~ {self.ended_at:%H:%M}\n\n{self.transcript_text()}\n",
            "utf-8")
        self.save_meta()

    def save_meta(self, extra: dict | None = None) -> None:
        meta_p = self.dir / "session.json"
        meta = json.loads(meta_p.read_text("utf-8")) if meta_p.exists() else {}
        meta.update({
            "title": self.title,
            "started_at": self.started_at.isoformat(timespec="seconds"),
            "ended_at": self.ended_at.isoformat(timespec="seconds") if self.ended_at else None,
            "audio_seconds": round(self.audio_seconds, 1),
            "entries": self.snapshot(),
        })
        if extra:
            meta.update(extra)
        meta_p.write_text(json.dumps(meta, ensure_ascii=False, indent=1), "utf-8")

    @classmethod
    def load(cls, folder: str) -> "Session":
        d = Path(folder)
        meta = json.loads((d / "session.json").read_text("utf-8"))
        s = cls.__new__(cls)
        s.dir = d
        s.title = meta.get("title", d.name)
        s.started_at = datetime.fromisoformat(meta["started_at"])
        s.ended_at = datetime.fromisoformat(meta["ended_at"]) if meta.get("ended_at") else None
        s.audio_seconds = meta.get("audio_seconds", 0)
        s.entries = [Entry(**e) for e in meta.get("entries", [])]
        s.version = 1
        s._lock = threading.Lock()
        s._next_id = len(s.entries)
        s._live = open(d / "transcript.live.jsonl", "a", encoding="utf-8")
        return s


def list_sessions(output_dir: str, limit: int = 50) -> list[dict]:
    root = Path(output_dir)
    if not root.exists():
        return []
    out = []
    for d in sorted((p for p in root.iterdir() if p.is_dir()), reverse=True)[:limit]:
        meta_p = d / "session.json"
        if not meta_p.exists():
            _recover(d)
        if not meta_p.exists():
            continue
        try:
            m = json.loads(meta_p.read_text("utf-8"))
        except Exception:
            continue
        out.append({"path": str(d), "title": m.get("title", d.name), "started_at": m.get("started_at"),
                    "analyzed": (d / "analysis.json").exists(), "entries": len(m.get("entries", []))})
    return out



def _recover(d: Path) -> None:
    """비정상 종료로 session.json 이 없으면 실시간 기록(jsonl)으로 복구한다."""
    live = d / "transcript.live.jsonl"
    if not live.exists():
        return
    entries = []
    for line in live.read_text("utf-8").splitlines():
        try:
            entries.append(json.loads(line))
        except Exception:
            pass
    if not entries:
        return
    entries.sort(key=lambda e: e["t_start"])
    try:
        started = datetime.strptime(d.name, "%Y-%m-%d_%H%M%S")
    except ValueError:
        started = datetime.fromtimestamp(live.stat().st_ctime)
    meta = {"title": f"회의 {started:%Y-%m-%d %H:%M} (복구됨)", "started_at": started.isoformat(timespec="seconds"),
            "ended_at": datetime.fromtimestamp(live.stat().st_mtime).isoformat(timespec="seconds"),
            "audio_seconds": 0, "entries": entries}
    (d / "session.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), "utf-8")
