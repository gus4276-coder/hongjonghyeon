@echo off
REM Windows 에서 직접 빌드: Python 3.12 + Inno Setup 6 설치 후 실행
setlocal
cd /d %~dp0\..
if not exist .venv (py -3.12 -m venv .venv || python -m venv .venv)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt pyinstaller==6.* || exit /b 1
pyinstaller build\app.spec --noconfirm --distpath dist --workpath build\work || exit /b 1
set ISCC="%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if exist %ISCC% (
  %ISCC% build\installer.iss || exit /b 1
  echo.
  echo 완료: dist\HiplazaMeetingNotes-Setup-*.exe
) else (
  echo Inno Setup 이 없어 설치 파일은 건너뜀. 실행 파일: dist\HiplazaMeetingNotes\HiplazaMeetingNotes.exe
)
endlocal
