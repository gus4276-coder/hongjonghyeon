"""분석 결과(dict) → HTML. 앱 화면 표시와 '회의록.html' 파일(메일/워드 붙여넣기용) 모두에 사용."""
from __future__ import annotations

from html import escape as e

_PRI = {"높음": "high", "보통": "mid", "낮음": "low"}

FILE_CSS = """
body{font-family:'Malgun Gothic','Apple SD Gothic Neo',sans-serif;max-width:900px;margin:32px auto;padding:0 16px;color:#1f2328;line-height:1.6}
h1{font-size:24px;margin:0 0 4px}h2{font-size:18px;margin:28px 0 10px;border-bottom:2px solid #e5e7eb;padding-bottom:4px}
h3{font-size:16px;margin:18px 0 6px}.meta{color:#6b7280;font-size:13px}.tr{color:#6b7280;font-weight:400;font-size:13px;margin-left:6px}
table{border-collapse:collapse;width:100%;font-size:14px}th,td{border:1px solid #d0d7de;padding:6px 8px;text-align:left;vertical-align:top}
th{background:#f6f8fa}.pri{display:inline-block;padding:0 8px;border-radius:10px;font-size:12px}
.pri.high{background:#fde2e1;color:#b42318}.pri.mid{background:#fef3c7;color:#92400e}.pri.low{background:#e0f2fe;color:#075985}
.dec{color:#047857}
"""


def render_html(a: dict, date_str: str, duration_str: str, fallback_title: str = "", standalone: bool = False) -> str:
    h: list[str] = []
    title = a.get("title") or fallback_title
    h.append(f"<h1>{e(title)}</h1>")
    meta = f"{e(date_str)} · {e(duration_str)}"
    if a.get("participants"):
        meta += " · 참석: " + e(", ".join(a["participants"]))
    h.append(f'<div class="meta">{meta}</div>')
    h.append(f"<h2>개요</h2><p>{e(a.get('overview', ''))}</p>")

    if a.get("action_items"):
        h.append("<h2>액션 플랜</h2><table><thead><tr><th>#</th><th>할 일</th><th>담당</th><th>기한</th><th>우선순위</th><th>관련 주제</th></tr></thead><tbody>")
        for i, t in enumerate(a["action_items"], 1):
            p = t.get("priority", "보통")
            ev = f' <span class="tr">{e(t["evidence"])}</span>' if t.get("evidence") else ""
            h.append(f"<tr><td>{i}</td><td>{e(t.get('task', ''))}{ev}</td><td>{e(t.get('owner', ''))}</td>"
                     f"<td>{e(t.get('due', ''))}</td><td><span class='pri {_PRI.get(p, 'mid')}'>{e(p)}</span></td>"
                     f"<td>{e(t.get('topic', ''))}</td></tr>")
        h.append("</tbody></table>")

    if a.get("decisions"):
        h.append("<h2>주요 결정 사항</h2><ul>" + "".join(f"<li>{e(d)}</li>" for d in a["decisions"]) + "</ul>")

    h.append("<h2>주제별 요약</h2>")
    for i, s in enumerate(a.get("sections", []), 1):
        h.append(f"<h3>{i}. {e(s.get('topic', ''))}<span class='tr'>{e(s.get('time_range', ''))}</span></h3>")
        h.append(f"<p>{e(s.get('summary', ''))}</p><ul>")
        h += [f"<li>{e(x)}</li>" for x in s.get("key_points", [])]
        h += [f"<li class='dec'>✅ 결정: {e(x)}</li>" for x in s.get("decisions", [])]
        h.append("</ul>")

    if a.get("open_issues"):
        h.append("<h2>미결 이슈 / 추가 확인</h2><ul>" + "".join(f"<li>{e(x)}</li>" for x in a["open_issues"]) + "</ul>")

    body = "\n".join(h)
    if not standalone:
        return body
    return (f"<!doctype html><html lang='ko'><head><meta charset='utf-8'><title>{e(title)}</title>"
            f"<style>{FILE_CSS}</style></head><body>{body}</body></html>")
