@echo off
cd /d "%~dp0"
echo Starting editor. Your browser opens automatically (first start may take ~30s).
venv\Scripts\python.exe server.py
pause
