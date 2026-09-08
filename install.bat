@echo off
setlocal
cd /d "%~dp0"

REM  Installs both toolchains: Python for the backend, npm for the interface.

where py >nul 2>nul
if %errorlevel%==0 (set PY=py) else (set PY=python)

if not exist ".venv\Scripts\python.exe" (
    %PY% -m venv .venv
    if errorlevel 1 goto :err
)
call ".venv\Scripts\activate.bat"
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if errorlevel 1 goto :err

where npm >nul 2>nul
if errorlevel 1 (
    echo.
    echo [WARN] npm was not found. Node 20.x is required to build the interface.
    echo        The backend is installed; install Node and re-run this script.
    goto :done
)

pushd web
call npm ci --no-audit --no-fund
if errorlevel 1 (
    popd
    goto :err
)
popd

:done
echo.
echo Installation completed. Run build.bat to produce dist\TelegramCenter.exe
pause
exit /b 0

:err
echo.
echo Installation failed.
pause
exit /b 1
