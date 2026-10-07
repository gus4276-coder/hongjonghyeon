# Hiplaza 회의록

Windows용 화상회의 **실시간 전사 + 회의 종료 후 주제별 요약 · 액션플랜 추출** 프로그램입니다.
Zoom · Teams · Google Meet · Webex 등 **어떤 회의 앱이든 상관없이** PC 스피커로 나오는 소리(상대방)와 마이크(나)를 함께 받아 적습니다.

| 단계 | 사용 모델 (2026-10 기준) | 비용 |
|---|---|---|
| 실시간 전사 | `gpt-transcribe` | 분당 $0.0045 (**무음 구간은 전송하지 않음**) |
| 회의 분석 | `gpt-6-luna` | 1시간 회의 기준 약 $0.01 |

**1시간 회의 기준 총 약 $0.15~0.25 (200~350원).** 비용은 화면 우측 상단에 실시간으로 표시됩니다.
쓸 수 없는 모델이면 자동으로 `gpt-4o-mini-transcribe` / `gpt-5-mini` 로 바꿔서 진행합니다.

---

## 사용자용: 설치와 사용

1. `HiplazaMeetingNotes-Setup-x.y.z.exe` 를 실행합니다. **관리자 권한이 필요 없습니다.**
   - Windows 10 에서 WebView2 가 없다고 나오면 안내에 따라 설치합니다(Windows 11 은 기본 설치되어 있음).
   - 설치 없이 쓰려면 `Portable.zip` 의 압축을 풀고 `HiplazaMeetingNotes.exe` 를 실행합니다.
2. 처음 실행하면 설정 창이 뜹니다. **OpenAI API 키**를 입력하고 **연결 테스트**를 누릅니다.
   - 키는 Windows 계정 단위로 암호화(DPAPI)되어 저장됩니다.
   - **용어집**에 회사명·제품명·사람 이름을 넣으면 고유명사 인식이 크게 좋아집니다.
3. 화상회의에 들어간 뒤 **● 녹음 시작** → 회의가 끝나면 **■ 종료** → **✨ 분석하기**.
   - 분석 전에 참석자·안건을 적어 두면 담당자를 더 정확하게 지정합니다.
4. 결과는 `문서\회의록\날짜_시각\` 폴더에 저장됩니다.
   - `전사본.txt`: 타임스탬프와 화자가 붙은 전체 전사본
   - `회의록.md`: 마크다운 회의록 (Notion·Confluence에 붙여넣기)
   - `회의록.html`: 서식이 있는 회의록 (메일·워드에 붙여넣기)
   - `analysis.json`, `session.json`: 원본 데이터

**🗂 기록** 버튼으로 지난 회의를 다시 열거나 분석할 수 있습니다. 회의 중에 프로그램이나 PC가 꺼져도 그때까지 전사된 내용은 `transcript.live.jsonl` 에 남아 있고, 다음 실행 때 자동으로 복구됩니다.

### 헤드셋 vs 스피커
- **헤드셋/이어폰**: 상대방 소리는 `참석자`, 내 목소리는 `나` 로 정확히 구분됩니다. 설정에서 *에코 방지*를 꺼도 됩니다.
- **노트북 스피커**: 상대 목소리가 마이크로도 들어가 같은 말이 두 번 적힐 수 있습니다. *에코 방지*(기본값 켜짐)가 이를 막습니다. 대신 상대방이 말하는 도중에 내가 동시에 한 짧은 말은 빠질 수 있습니다.
- 회의 중에 이어폰을 꽂거나 빼도 출력 장치를 자동으로 따라갑니다.

---

## 최적화 포인트

- **무음 제거(VAD)**: webrtcvad와 적응형 소음 임계값을 함께 써서 발화 구간만 잘라 보냅니다. 회의에는 침묵·대기 시간이 많아 전사 비용이 보통 20~40% 줄어듭니다.
- **발화 단위 분할**: 0.7초 쉬면 끊고, 12초가 넘으면 짧은 쉼에서도, 20초가 되면 무조건 끊습니다. 그래서 화면 지연은 보통 1~3초입니다. 앞에 0.3초를 덧붙여 첫 음절이 잘리지 않게 합니다.
- **16 kHz mono 리샘플링(soxr)**: 업로드 크기가 48 kHz 스테레오의 1/6로 줄어듭니다.
- **병렬 전사(4 workers)**: 결과가 순서와 다르게 도착해도 타임스탬프 기준으로 정렬해 표시합니다.
- **문맥 프롬프트**: 직전 전사 내용과 용어집을 프롬프트로 넘깁니다. `gpt-transcribe` 의 `keywords` / `languages` 파라미터도 사용합니다.
- **환각 필터**: 짧은 잡음 구간에서 STT가 만들어내는 문구("시청해주셔서 감사합니다" 등)와 같은 말이 계속 반복되는 결과를 제거합니다.
- **장애 대응**: 네트워크·429·5xx 오류는 자동으로 재시도합니다. 끝내 실패한 구간은 메모리에 보관했다가 종료할 때 다시 전사합니다.
- **Structured Outputs**: 분석 결과를 JSON Schema로 강제해 항상 같은 형식의 회의록이 나옵니다. 아주 긴 회의(120k자 이상)는 나눠서 요약한 뒤 합칩니다.
- **가벼운 UI**: Windows 내장 Edge WebView2를 쓰므로 Chromium을 함께 넣지 않아도 됩니다. 설치 파일은 약 40 MB입니다.

---

## 배포 담당자용: 빌드

### 방법 A. GitHub Actions (권장 — Windows PC 가 없어도 됨)
1. 이 폴더를 GitHub 저장소(private 가능)에 push 합니다.
2. 태그를 push 합니다.
   ```bash
   git tag v1.0.0
   ```
   ```bash
   git push origin v1.0.0
   ```
3. Actions 가 끝나면 Releases 에 `Setup.exe` 와 `Portable.zip` 이 올라옵니다. 이 링크를 사내에 공유하면 됩니다.
   (태그 없이 Actions 탭에서 *Run workflow* 를 눌러도 Artifacts 로 받을 수 있습니다.)

### 방법 B. Windows PC 에서 직접
Python 3.12와 [Inno Setup 6](https://jrsoftware.org/isdl.php)를 설치한 뒤 `build\build.bat` 를 실행합니다. 결과는 `dist\` 폴더에 생깁니다.

### 회사 공통 설정 일괄 배포
`defaults.example.json` 을 `defaults.json` 으로 복사해 용어집과 모델을 채웁니다. 이 파일을 `Setup.exe` 와 같은 폴더에 두고 함께 배포하면 설치할 때 적용됩니다. 사용자가 설정에서 바꾼 값이 항상 우선합니다.
> `defaults.json` 에 `"api_key"` 를 넣을 수도 있지만, 파일이 유출되면 키도 함께 유출됩니다. 가능하면 사용자별 키나 [프로젝트 키](https://platform.openai.com/api-keys)를 쓰고, 사용량 한도를 걸어 두세요.

### 개발 실행 (Mac/Linux 는 마이크만 지원)
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt && .venv/bin/python run.py
```
- `STT_TEST_FILE=sample.wav` 를 설정하면 장치 대신 WAV 파일을 실시간으로 흘려 테스트합니다. `STT_DEBUG=1` 은 WebView 개발자 도구를 켭니다.

---

## 구조
```
run.py                  진입점
app/main.py             pywebview 창, JS↔Python API, 상태 관리
app/audio_capture.py    WASAPI loopback + 마이크 캡처, 장치 변경 감시, 리샘플
app/vad.py              VAD + 발화 분할
app/transcriber.py      병렬 전사, 재시도, 환각 필터
app/analyzer.py         주제별 요약·액션플랜 (Structured Outputs, map-reduce)
app/report.py           회의록 HTML 렌더링
app/session.py          세션 저장, 크래시 복구, 기록 목록
app/config.py           설정, DPAPI 키 암호화, 모델/단가
app/ui/index.html       UI (단일 파일, 다크모드 지원)
build/                  PyInstaller spec, Inno Setup 스크립트, build.bat
```

## 문제 해결
| 증상 | 해결 |
|---|---|
| ‘상대’ 레벨 바가 움직이지 않음 | 회의 앱의 스피커가 Windows *기본 출력 장치*로 설정되어 있는지 확인 |
| ‘나’ 레벨 바가 움직이지 않음 | 설정에서 마이크 장치를 선택하고, Windows 개인정보 설정에서 *마이크 접근*을 허용 |
| 같은 말이 두 번 적힘 | 설정에서 *에코 방지*를 켜거나 헤드셋을 사용 |
| 인증 오류 | API 키와 결제 수단(크레딧)을 확인 |
| 기타 | 로그 파일: `%APPDATA%\HiplazaMeetingNotes\app.log` |

## 개인정보
음성은 발화 구간만 OpenAI API로 전송됩니다. OpenAI API 데이터는 기본적으로 모델 학습에 쓰이지 않습니다. 전사본과 회의록은 각 PC의 로컬 폴더에만 저장됩니다. 회의를 녹음하기 전에 참석자에게 알리세요.

## 사용한 오픈소스
[openai-python](https://github.com/openai/openai-python) (Apache-2.0) · [pywebview](https://github.com/r0x0r/pywebview) (BSD) · [PyAudioWPatch](https://github.com/s0d3s/PyAudioWPatch) (MIT) · [py-webrtcvad-wheels](https://github.com/daanzu/py-webrtcvad-wheels) (MIT) · [python-soxr](https://github.com/dofuuz/python-soxr) (LGPL-2.1) · [NumPy](https://numpy.org) (BSD) · [PyInstaller](https://pyinstaller.org) (GPL + 예외) · [Inno Setup](https://jrsoftware.org)
