# pyinstaller-hooks-contrib 의 기본 훅은 'webrtcvad' 배포 메타데이터를 찾지만
# 실제 설치 패키지는 'webrtcvad-wheels' 이므로 그 이름으로 덮어쓴다.
from PyInstaller.utils.hooks import copy_metadata

datas = copy_metadata("webrtcvad-wheels")
