#!/bin/bash
# HUN STUDIO 실행 (Mac용)
# 더블클릭하면 로컬 처리 서버가 실행됩니다.

cd "$(dirname "$0")"

echo "=============================="
echo " HUN STUDIO 로컬 서버 시작 중..."
echo "=============================="

# Python 확인
if ! command -v python3 &> /dev/null; then
    echo ""
    echo "[오류] Python3가 설치되어 있지 않습니다."
    echo "https://www.python.org/downloads/ 에서 Python 3.10 이상을 설치해주세요."
    echo ""
    read -p "아무 키나 눌러 종료..."
    exit 1
fi

# ffmpeg 확인 (없으면 경고만)
if ! command -v ffmpeg &> /dev/null; then
    echo ""
    echo "[알림] ffmpeg이 없습니다. 믹싱·마스터링에 필요합니다."
    echo "설치: brew install ffmpeg"
    echo ""
fi

# 가상환경 확인/생성
if [ ! -d "venv" ]; then
    echo "가상환경을 만듭니다..."
    python3 -m venv venv
fi

source venv/bin/activate

# 의존성 설치 확인
if ! python -c "import fastapi" 2>/dev/null; then
    echo "필요한 패키지를 설치합니다..."
    pip install --upgrade pip
    pip install -r requirements.txt
fi

echo ""
echo "서버 주소: http://127.0.0.1:8765/"
echo "브라우저에서 위 주소로 접속하세요."
echo "종료하려면 이 창을 닫으세요."
echo "=============================="
echo ""

python server.py
