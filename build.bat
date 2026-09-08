@echo off
setlocal
cd /d "%~dp0"

REM  Full pipeline: frontend -> tests -> single exe.
REM  Nothing here ever touches %APPDATA%\TelegramCenter.

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Run install.bat first.
    pause
    exit /b 1
)
call ".venv\Scripts\activate.bat"

where npm >nul 2>nul
if errorlevel 1 (
    echo [ERROR] npm was not found in PATH. Node 20.x is required to build the UI.
    pause
    exit /b 1
)

echo [CLEAN] removing previous build output...
if exist "build" rmdir /s /q "build"
if exist "dist" rmdir /s /q "dist"
if exist "web\dist" rmdir /s /q "web\dist"
for /d /r %%d in (__pycache__) do @if exist "%%d" rmdir /s /q "%%d"

echo [WEB] installing pinned frontend dependencies...
pushd web
call npm ci --no-audit --no-fund
if errorlevel 1 (
    popd
    echo.
    echo [ERROR] npm ci failed.
    pause
    exit /b 1
)

echo [WEB] building the React bundle...
call npm run build
if errorlevel 1 (
    popd
    echo.
    echo [ERROR] Frontend build failed.
    pause
    exit /b 1
)
popd

if not exist "web\dist\index.html" (
    echo [ERROR] web\dist\index.html is missing after the build.
    pause
    exit /b 1
)

echo [TEST] running the test suite...
python -m pytest -q
if errorlevel 1 (
    echo.
    echo [ERROR] Tests failed - build stopped.
    pause
    exit /b 1
)

echo [BUILD] PyInstaller (onefile)...
python -m PyInstaller --noconfirm --clean --distpath dist --workpath build TelegramCenter.spec
if errorlevel 1 (
    echo.
    echo [ERROR] Build failed.
    pause
    exit /b 1
)

echo [CLEAN] removing intermediate build files, keeping only the exe...
if exist "build" rmdir /s /q "build"

echo.
echo [OK] dist\TelegramCenter.exe   (single portable file)
echo      user data stays in %%APPDATA%%\TelegramCenter
echo.
pause
