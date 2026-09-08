# -*- mode: python ; coding: utf-8 -*-
# Build:  pyinstaller TelegramCenter.spec   (normally via build.bat)
# Output: a single portable file  dist\TelegramCenter.exe
# Work files go to  build\  and are deleted afterwards.
#
# The built React bundle is embedded read-only under web/dist inside the EXE
# and served from memory-mapped storage at runtime. No user data is ever
# written there — that all lives in %APPDATA%\TelegramCenter.

from pathlib import Path

block_cipher = None

WEB_DIST = Path("web") / "dist"
if not (WEB_DIST / "index.html").is_file():
    raise SystemExit(
        "web/dist/index.html is missing - build the frontend first:\n"
        "    cd web && npm ci && npm run build")

# every file of the bundle, keeping its path inside web/dist
web_datas = [
    (str(path), str(Path("web") / "dist" / path.relative_to(WEB_DIST).parent))
    for path in WEB_DIST.rglob("*") if path.is_file()
]

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=[("icon.ico", ".")] + web_datas,
    hiddenimports=[
        "telethon", "qrcode",
        "socks", "python_socks", "python_socks.async_.asyncio",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        "imageio_ffmpeg", "numpy", "PIL", "pytest", "_pytest",
        "matplotlib", "pandas", "scipy", "IPython", "tornado", "setuptools",
    ],
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="TelegramCenter",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,
    icon="icon.ico",
)
