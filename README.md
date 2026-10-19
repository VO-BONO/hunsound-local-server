# HUN STUDIO 로컬 서버

HUNSOUND(https://hunsound.netlify.app)의 로컬 처리 서버입니다.
스템 분리·믹싱·마스터링 최종 렌더링은 이 서버가 있어야 동작합니다.

## 실행 방법

### Mac
1. `HUN STUDIO 실행.command` 더블클릭
2. 브라우저에서 http://127.0.0.1:8765/ 접속

### Windows
1. `HUN STUDIO 실행.bat` 더블클릭
2. 브라우저에서 http://127.0.0.1:8765/ 접속

> 첫 실행 시 필요한 Python 패키지를 자동으로 설치합니다.
> 종료하려면 터미널/명령 프롬프트 창을 닫으세요.

## 준비물

- Python 3.10 이상 (https://www.python.org/downloads/)
- ffmpeg (믹싱·마스터링용)
  - Mac: `brew install ffmpeg`
  - Windows: https://www.gyan.dev/ffmpeg/builds/
- 스템 분리 모델은 첫 실행 시 자동 다운로드됩니다 (HTDemucs 6S)

## 수동 실행

```bash
python3 -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
python server.py
```

## API

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | /api/health | 서버 상태 |
| POST | /api/upload | 음원 업로드 |
| POST | /api/stem | 6스템 분리 (보컬/기타/베이스/피아노/드럼/나머지) |
| POST | /api/mixing | 믹싱 |
| POST | /api/master | 마스터링 (LUFS/True Peak) |
| POST | /api/vocaldyne | 보컬 피치 보정 (v1.0 미구현 — 명확한 안내 반환) |
| POST | /api/vocal_analysis | 보컬 음정 분석 (librosa 필요) |
| POST | /api/export | 파일 변환 |
| POST | /api/enhance-export | 오디오 향상 내보내기 |
| GET | /api/jobs | 작업 목록 |
| GET | /api/jobs/{id} | 작업 상태 |
| GET | /api/files/{id}/{파일명} | 결과 파일 다운로드 |

## 폴더 구조

```
hunsound-local-server/
├── server.py              # 메인 서버
├── requirements.txt       # Python 의존성
├── HUN STUDIO 실행.command # Mac 실행 스크립트
├── HUN STUDIO 실행.bat     # Windows 실행 스크립트
├── web/                   # 웹 UI (HUNSOUND 빌드본)
└── data/                  # 업로드·결과 파일 (자동 생성)
```

## 라이선스

BY 이정훈 · JINSEONG MIRAE HIGH SCHOOL
