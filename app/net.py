"""사내망 대응: 시스템 프록시 · OS 인증서 저장소 사용 + 연결 실패 원인 진단."""
from __future__ import annotations

import logging
import ssl
import sys
import urllib.request

import openai

log = logging.getLogger(__name__)

try:
    import truststore  # Windows 인증서 저장소/macOS 키체인의 루트 인증서 사용 (회사 SSL 검사 장비 대응)
except Exception:  # pragma: no cover
    truststore = None


def resolve_proxy(setting: str) -> str | None:
    """설정값: '' = 시스템 프록시 자동, 'none' = 직접 연결, 그 외 = 프록시 URL."""
    s = (setting or "").strip()
    if s.lower() in ("none", "off", "direct", "사용 안 함"):
        return None
    if s:
        return s if "://" in s else "http://" + s
    # Windows: 인터넷 옵션(레지스트리) / macOS: 네트워크 설정 / 환경변수 HTTPS_PROXY
    proxies = urllib.request.getproxies()
    p = proxies.get("https") or proxies.get("http")
    if p and "://" not in p:
        p = "http://" + p
    return p or None


def ssl_context() -> ssl.SSLContext | bool:
    if truststore is not None:
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    return True


def http_client(proxy_setting: str, timeout: float):
    proxy = resolve_proxy(proxy_setting)
    log.info("network: proxy=%s truststore=%s", proxy or "direct", truststore is not None)
    factory = getattr(openai, "DefaultHttpx2Client", None) or openai.DefaultHttpxClient
    return factory(proxy=proxy, verify=ssl_context(), timeout=timeout, trust_env=False)


def diagnose(e: Exception) -> str:
    """예외를 사용자가 이해할 수 있는 원인 + 조치로 바꾼다."""
    chain, cur = [], e
    while cur is not None and len(chain) < 6:
        chain.append(f"{type(cur).__name__}: {cur}")
        cur = cur.__cause__ or cur.__context__
    raw = " | ".join(chain).lower()
    status = getattr(e, "status_code", None)

    if status == 401:
        return "API 키가 올바르지 않습니다. 키를 다시 확인하세요."
    if status == 403 and ("<html" in raw or "blocked" in raw or "차단" in raw or "forbidden" in raw and "openai" not in raw):
        return ("회사 보안 장비(프록시/방화벽)가 api.openai.com 접속을 정책적으로 차단하고 있습니다. "
                "IT 부서에 api.openai.com 허용(화이트리스트)을 요청하거나, 회사 승인 게이트웨이 주소를 고급 설정에 입력하세요.")
    if status == 403:
        return "OpenAI 가 요청을 거부했습니다(권한/지역 제한). 키의 프로젝트 권한을 확인하세요."
    if status == 407 or "407" in raw or "proxy authentication" in raw:
        return ("프록시가 사용자 인증을 요구합니다. 고급 설정의 프록시에 http://아이디:비밀번호@프록시주소:포트 형식으로 입력하거나, "
                "IT 부서에 이 프로그램(또는 api.openai.com)의 인증 예외를 요청하세요.")
    if "certificate" in raw or "ssl" in raw or "tls" in raw:
        return ("회사 보안 장비가 HTTPS 를 검사하면서 인증서가 바뀌어 연결이 거부되었습니다. "
                "회사 루트 인증서가 Windows 인증서 저장소에 설치되어 있어야 합니다(IT 부서 문의). "
                f"[상세: {chain[-1][:120]}]")
    if "name resolution" in raw or "getaddrinfo" in raw or "nodename" in raw or "name or service" in raw:
        return ("api.openai.com 주소를 찾을 수 없습니다(DNS 차단 또는 외부 인터넷 불가). "
                "회사 프록시가 있다면 고급 설정에 프록시 주소를 입력하세요(브라우저 프록시 설정과 동일).")
    if "timed out" in raw or "timeout" in raw:
        return ("api.openai.com 에 연결하는 중 시간이 초과되었습니다(방화벽 차단 가능성). "
                "회사 프록시가 있다면 고급 설정에 입력하고, 없다면 IT 부서에 api.openai.com:443 허용을 요청하세요.")
    if "connect" in raw or "refused" in raw or "reset" in raw:
        return ("api.openai.com 에 연결할 수 없습니다(방화벽/프록시 차단). "
                "브라우저에서 https://api.openai.com 이 열리는지 확인하고, 열린다면 고급 설정에 프록시 주소를 입력하세요. "
                f"[상세: {chain[-1][:120]}]")
    return str(e)[:300]


def describe() -> str:
    p = resolve_proxy("")
    return f"시스템 프록시: {p or '없음'} · 인증서: {'OS 저장소' if truststore else '기본(certifi)'} · {sys.platform}"
