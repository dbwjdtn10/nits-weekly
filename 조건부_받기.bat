@echo off
rem encoding: CP949 (Korean Windows cmd) - do not save as UTF-8
cd /d "%~dp0"
echo 조건부 주간 파일을 받는 중입니다...
git pull --ff-only origin main
if errorlevel 1 (
  echo.
  echo 받지 못했습니다. 위에 나온 글을 Claude에게 보여 주세요.
  pause
  exit /b 1
)
start "" "%~dp0조건부"
