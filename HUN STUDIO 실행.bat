@echo off
REM HUN STUDIO 실행 (Windows용)
REM 더블클릭하면 로컬 처리 서버가 실행됩니다.
cd /d "%~dp0"

echo ==============================
echo  HUN STUDIO 로컬 서버 시작 중...
echo ==============================

where python >nul 2>&1
if %errorlevel% neq 0 (
    echo.
    echo [오류] Python이 설치되어 있지 않습니다.
    echo https://www.python.org/downloads/ 에서 Python 3.10 이상을 설치해주세요.
    echo 설치 시 "Add python.exe to PATH"를 꼭 체크하세요.
    echo.
    pause
    exit /b 1
)

where ffmpeg >nul 2>&1
if %errorlevel% neq 0 (
    echo.
    echo [알림] ffmpeg이 없습니다. 믹싱·마스터링에 필요합니다.
    echo https://www.gyan.dev/ffmpeg/builds/ 에서 받아 PATH에 추가하세요.
    echo.
)

if not exist "venv" (
    echo 가상환경을 만듭니다...
    python -m venv venv
)

call venv\Scripts\activate.bat

python -c "import fastapi" 2>nul
if %errorlevel% neq 0 (
    echo 필요한 패키지를 설치합니다...
    python -m pip install --upgrade pip
    pip install -r requirements.txt
)

echo.
echo 서버 주소: http://127.0.0.1:8765/
echo 브라우저에서 위 주소로 접속하세요.
echo 종료하려면 이 창을 닫으세요.
echo ==============================
echo.

python server.py
pause
