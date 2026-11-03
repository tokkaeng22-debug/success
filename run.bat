@echo off
cd /d "%~dp0"
echo Starting. Your browser opens automatically (first start may take ~30s).
echo   Editor      : http://127.0.0.1:5173
echo   Auto edit   : http://127.0.0.1:5173/auto
venv\Scripts\python.exe server.py
pause
