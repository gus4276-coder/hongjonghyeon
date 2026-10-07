"""설정 저장/로드. API 키는 Windows DPAPI / macOS 키체인에 보관한다."""
from __future__ import annotations

import base64
import json
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

APP_NAME = "HiplazaMeetingNotes"
APP_TITLE = "Hiplaza 회의록"
APP_VERSION = "1.1.0"

# 2026-10 기준 가성비 모델 (설정에서 변경 가능)
DEFAULT_TRANSCRIBE_MODEL = "gpt-transcribe"      # $0.0045/분, prompt·keywords·languages 지원
DEFAULT_ANALYSIS_MODEL = "gpt-6-luna"           # $0.10 / $0.50 per 1M tokens
FALLBACK_ANALYSIS_MODEL = "gpt-5-mini"
FALLBACK_TRANSCRIBE_MODEL = "gpt-4o-mini-transcribe"

# 전사 단가(분당 USD). 모르는 모델은 0으로 표시만 생략
TRANSCRIBE_PRICE_PER_MIN = {
    "gpt-transcribe": 0.0045,
    "gpt-4o-mini-transcribe": 0.003,
    "gpt-4o-transcribe": 0.006,
    "whisper-1": 0.006,
}
# (input, output) USD per 1M tokens
ANALYSIS_PRICE = {
    "gpt-6-luna": (0.10, 0.50),
    "gpt-6.1-sol": (2.00, 10.00),
    "gpt-5-mini": (0.25, 2.00),
    "gpt-5-nano": (0.05, 0.40),
}


def _appdata_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    d = base / APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def _default_output_dir() -> str:
    docs = Path.home() / "Documents"
    return str((docs if docs.exists() else Path.home()) / "회의록")


CONFIG_PATH = _appdata_dir() / "config.json"
LOG_PATH = _appdata_dir() / "app.log"


# ---------------------------------------------------------------- DPAPI
_KC_SERVICE = APP_NAME
_KC_ACCOUNT = "openai_api_key"


def _keychain_set(text: str) -> bool:
    import subprocess
    r = subprocess.run(["/usr/bin/security", "add-generic-password", "-U", "-s", _KC_SERVICE, "-a", _KC_ACCOUNT,
                        "-w", text], capture_output=True)
    return r.returncode == 0


def _keychain_get() -> str:
    import subprocess
    r = subprocess.run(["/usr/bin/security", "find-generic-password", "-s", _KC_SERVICE, "-a", _KC_ACCOUNT, "-w"],
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def _protect(text: str) -> str:
    if not text:
        return ""
    if sys.platform == "darwin" and _keychain_set(text):
        return "keychain:"
    if sys.platform != "win32":
        return "b64:" + base64.b64encode(text.encode()).decode()
    import ctypes
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    raw = text.encode("utf-8")
    inp = BLOB(len(raw), ctypes.cast(ctypes.create_string_buffer(raw, len(raw)), ctypes.POINTER(ctypes.c_char)))
    out = BLOB()
    if not ctypes.windll.crypt32.CryptProtectData(ctypes.byref(inp), None, None, None, None, 0, ctypes.byref(out)):
        raise OSError("CryptProtectData failed")
    try:
        data = ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)
    return "dpapi:" + base64.b64encode(data).decode()


def _unprotect(blob: str) -> str:
    if not blob:
        return ""
    if blob == "keychain:":
        return _keychain_get()
    if blob.startswith("b64:"):
        return base64.b64decode(blob[4:]).decode()
    if blob.startswith("dpapi:") and sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class BLOB(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

        raw = base64.b64decode(blob[6:])
        inp = BLOB(len(raw), ctypes.cast(ctypes.create_string_buffer(raw, len(raw)), ctypes.POINTER(ctypes.c_char)))
        out = BLOB()
        if not ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(inp), None, None, None, None, 0, ctypes.byref(out)):
            return ""  # 다른 PC/계정에서 복사된 설정
        try:
            return ctypes.string_at(out.pbData, out.cbData).decode("utf-8")
        finally:
            ctypes.windll.kernel32.LocalFree(out.pbData)
    return ""


# ---------------------------------------------------------------- Settings
@dataclass
class Settings:
    api_key_enc: str = ""
    base_url: str = ""                       # 프록시/사내 게이트웨이 사용 시
    transcribe_model: str = DEFAULT_TRANSCRIBE_MODEL
    analysis_model: str = DEFAULT_ANALYSIS_MODEL
    languages: list[str] = field(default_factory=lambda: ["ko", "en"])
    keywords: list[str] = field(default_factory=list)   # 회사/제품/인명 용어집
    capture_system: bool = True              # 화상회의 상대방 음성(스피커 출력)
    capture_mic: bool = True                 # 내 목소리
    mic_device: str = ""                     # 빈 값 = 기본 마이크
    echo_guard: bool = True                  # 스피커 사용 시 마이크로 새는 상대 음성 중복 방지
    my_name: str = "나"
    output_dir: str = field(default_factory=_default_output_dir)
    save_audio: bool = False                 # 발화 구간 오디오(WAV) 보관

    # --- api key helpers
    @property
    def api_key(self) -> str:
        cache = self.__dict__.get("_key_cache")
        if cache is None or cache[0] != self.api_key_enc:
            cache = (self.api_key_enc, _unprotect(self.api_key_enc))
            self.__dict__["_key_cache"] = cache
        return cache[1] or os.environ.get("OPENAI_API_KEY", "")

    def set_api_key(self, key: str) -> None:
        self.__dict__.pop("_key_cache", None)
        self.api_key_enc = _protect(key.strip())

    def to_public(self) -> dict:
        d = asdict(self)
        d.pop("api_key_enc")
        k = self.api_key
        d["api_key_masked"] = (k[:7] + "…" + k[-4:]) if len(k) > 12 else ("설정됨" if k else "")
        return d


def _merge_admin_defaults(data: dict) -> dict:
    """설치 폴더(또는 설정 폴더)의 defaults.json(관리자 배포용)을 사용자 설정 아래에 깐다."""
    exe_dir = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent
    candidates = [exe_dir / "defaults.json", _appdata_dir() / "defaults.json"]
    p = next((c for c in candidates if c.exists()), None)
    if p:
        try:
            admin = json.loads(p.read_text("utf-8"))
            if "api_key" in admin and not data.get("api_key_enc"):
                data["api_key_enc"] = _protect(admin.pop("api_key"))
            return {**admin, **data}
        except Exception:
            pass
    return data


def load_settings() -> Settings:
    data: dict = {}
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text("utf-8"))
        except Exception:
            data = {}
    data = _merge_admin_defaults(data)
    known = {f for f in Settings.__dataclass_fields__}
    return Settings(**{k: v for k, v in data.items() if k in known})


def save_settings(s: Settings) -> None:
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(s), ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(CONFIG_PATH)
