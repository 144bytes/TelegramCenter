@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\pythonw.exe" (
    ".venv\Scripts\pythonw.exe" main.py
) else (
    python main.py
)
