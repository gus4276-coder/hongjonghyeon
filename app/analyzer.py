"""회의 종료 후 분석: 주제(섹터)별 요약 + 액션플랜 추출. Structured Outputs(JSON Schema) 사용."""
from __future__ import annotations

import json
import logging
from typing import Callable

import openai

from .config import ANALYSIS_PRICE, FALLBACK_ANALYSIS_MODEL, Settings
from .transcriber import make_client

log = logging.getLogger(__name__)

_str_list = {"type": "array", "items": {"type": "string"}}

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["title", "overview", "participants", "sections", "action_items", "decisions", "open_issues"],
    "properties": {
        "title": {"type": "string", "description": "회의 내용을 대표하는 짧은 제목"},
        "overview": {"type": "string", "description": "회의 전체 요약 3~5문장"},
        "participants": {**_str_list, "description": "발언에서 확인되는 참석자 이름/역할. 모르면 빈 배열"},
        "sections": {
            "type": "array",
            "description": "논의된 주제(섹터)별 묶음, 시간 순",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["topic", "time_range", "summary", "key_points", "decisions"],
                "properties": {
                    "topic": {"type": "string"},
                    "time_range": {"type": "string", "description": "예: 03:10 ~ 15:42"},
                    "summary": {"type": "string", "description": "2~4문장 요약"},
                    "key_points": {**_str_list, "description": "핵심 논의 사항(수치·근거 포함)"},
                    "decisions": {**_str_list, "description": "이 주제에서 확정된 사항. 없으면 빈 배열"},
                },
            },
        },
        "action_items": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["task", "owner", "due", "priority", "topic", "evidence"],
                "properties": {
                    "task": {"type": "string", "description": "동사로 끝나는 구체적 할 일"},
                    "owner": {"type": "string", "description": "담당자. 불명확하면 '미정'"},
                    "due": {"type": "string", "description": "기한. 언급 없으면 '미정'"},
                    "priority": {"type": "string", "enum": ["높음", "보통", "낮음"]},
                    "topic": {"type": "string", "description": "관련 섹션 topic"},
                    "evidence": {"type": "string", "description": "근거가 된 발언 시각, 예: 12:34"},
                },
            },
        },
        "decisions": {**_str_list, "description": "회의 전체의 주요 결정 사항"},
        "open_issues": {**_str_list, "description": "결론 나지 않은 이슈·추가 확인 필요 사항"},
    },
}

INSTRUCTIONS = """당신은 회의록 작성 전문가입니다. 화상회의 자동 전사본을 분석해 한국어 회의록을 만듭니다.

규칙
- 전사본은 음성인식 결과라 오탈자·동음이의어 오류가 있을 수 있습니다. 문맥으로 바로잡되, 없는 내용을 지어내지 마세요.
- sections: 논의 흐름에 따라 3~10개의 주제로 묶고 시간 순으로 정렬합니다. 잡담·인사는 제외합니다.
- action_items: 누군가 해야 할 일로 합의되었거나 요청·약속된 것만 추출합니다. 담당자·기한이 발화에 없으면 '미정'.
- 우선순위: 기한이 임박하거나 의사결정·고객 영향이 크면 '높음'.
- 화자 라벨: '{me}'는 이 프로그램 사용자, '참석자'는 화상회의 상대측(여러 명일 수 있음) 음성입니다. 발언 내용에서 이름이 드러나면 그 이름을 사용하세요.
- 숫자, 날짜, 금액, 고유명사는 정확히 보존합니다.
"""

MAP_INSTRUCTIONS = """다음은 긴 회의 전사본의 일부입니다. 이후 전체 회의록을 만들기 위한 중간 노트를 작성하세요.
시간 표기를 유지하며, 주제별 핵심 논의/수치/결정/할 일(담당자·기한 포함)/미결 이슈를 빠짐없이 bullet로 정리합니다."""

CHUNK_CHARS = 120_000   # 이보다 길면 분할 요약 후 통합(map-reduce)


class Analyzer:
    def __init__(self, settings: Settings, on_progress: Callable[[str], None] = lambda m: None):
        self.s = settings
        self.model = settings.analysis_model
        self.client = make_client(settings, timeout=300)
        self.on_progress = on_progress
        self.usage_in = 0
        self.usage_out = 0

    @property
    def cost_usd(self) -> float:
        pin, pout = ANALYSIS_PRICE.get(self.model, (0, 0))
        return (self.usage_in * pin + self.usage_out * pout) / 1e6

    def _respond(self, instructions: str, text: str, schema: dict | None = None) -> str:
        kwargs: dict = {"model": self.model, "instructions": instructions, "input": text}
        if schema:
            kwargs["text"] = {"format": {"type": "json_schema", "name": "meeting_analysis", "schema": schema, "strict": True}}
        try:
            r = self.client.responses.create(**kwargs)
        except openai.NotFoundError:
            if self.model == FALLBACK_ANALYSIS_MODEL:
                raise
            self.on_progress(f"'{self.model}' 사용 불가 → {FALLBACK_ANALYSIS_MODEL} 로 분석합니다")
            self.model = FALLBACK_ANALYSIS_MODEL
            return self._respond(instructions, text, schema)
        if r.usage:
            self.usage_in += r.usage.input_tokens or 0
            self.usage_out += r.usage.output_tokens or 0
        return r.output_text

    def analyze(self, transcript: str, title: str = "", context: str = "") -> dict:
        if not transcript.strip():
            raise ValueError("전사된 내용이 없습니다.")
        head = f"회의 제목(사용자 입력): {title}\n" if title else ""
        if context.strip():
            head += f"사전 정보(참석자/안건 등): {context.strip()}\n"
        instr = INSTRUCTIONS.replace("{me}", self.s.my_name or "나")

        body = transcript
        if len(transcript) > CHUNK_CHARS:
            chunks = _split(transcript, CHUNK_CHARS)
            notes = []
            for i, c in enumerate(chunks, 1):
                self.on_progress(f"긴 회의 분할 분석 {i}/{len(chunks)}…")
                notes.append(f"### 파트 {i}\n" + self._respond(MAP_INSTRUCTIONS, c))
            body = "아래는 회의 전사본을 파트별로 정리한 중간 노트입니다.\n\n" + "\n\n".join(notes)

        self.on_progress("주제별 요약 · 액션플랜 추출 중…")
        raw = self._respond(instr, f"{head}\n=== 전사본 ===\n{body}", SCHEMA)
        data = json.loads(raw)
        data["_meta"] = {"model": self.model, "input_tokens": self.usage_in,
                         "output_tokens": self.usage_out, "cost_usd": round(self.cost_usd, 4)}
        return data


def _split(text: str, size: int) -> list[str]:
    lines, chunks, cur, n = text.splitlines(), [], [], 0
    for ln in lines:
        if n + len(ln) > size and cur:
            chunks.append("\n".join(cur))
            cur, n = cur[-5:], sum(len(x) for x in cur[-5:])  # 약간의 겹침으로 문맥 유지
        cur.append(ln)
        n += len(ln) + 1
    if cur:
        chunks.append("\n".join(cur))
    return chunks


def to_markdown(a: dict, session_title: str, date_str: str, duration_str: str) -> str:
    L = [f"# {a.get('title') or session_title}", "",
         f"- 일시: {date_str} ({duration_str})"]
    if a.get("participants"):
        L.append("- 참석자: " + ", ".join(a["participants"]))
    L += ["", "## 개요", a.get("overview", ""), ""]
    if a.get("decisions"):
        L += ["## 주요 결정 사항"] + [f"- {d}" for d in a["decisions"]] + [""]
    if a.get("action_items"):
        L += ["## 액션 플랜", "", "| # | 할 일 | 담당 | 기한 | 우선순위 | 관련 주제 |", "|---|---|---|---|---|---|"]
        for i, t in enumerate(a["action_items"], 1):
            cells = [t.get("task", ""), t.get("owner", ""), t.get("due", ""), t.get("priority", ""), t.get("topic", "")]
            L.append(f"| {i} | " + " | ".join(c.replace("|", "/") for c in cells) + " |")
        L.append("")
    L.append("## 주제별 요약")
    for i, s in enumerate(a.get("sections", []), 1):
        L += ["", f"### {i}. {s.get('topic', '')}  `{s.get('time_range', '')}`", s.get("summary", "")]
        L += [f"- {p}" for p in s.get("key_points", [])]
        L += [f"- ✅ 결정: {d}" for d in s.get("decisions", [])]
    if a.get("open_issues"):
        L += ["", "## 미결 이슈 / 추가 확인"] + [f"- {x}" for x in a["open_issues"]]
    return "\n".join(L) + "\n"
