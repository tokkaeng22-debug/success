@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem Google Drive folder to watch. Change this path if yours is different.
set WATCH=G:\내 드라이브\설교영상
venv\Scripts\python.exe autopipe.py --watch "%WATCH%"
pause
