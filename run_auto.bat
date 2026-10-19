@echo off
cd /d "%~dp0"
rem 구글 드라이브 데스크톱 동기화 폴더 경로를 아래에 맞게 수정하세요.
set WATCH=G:\내 드라이브\설교영상
venv\Scripts\python.exe autopipe.py --watch "%WATCH%"
pause
