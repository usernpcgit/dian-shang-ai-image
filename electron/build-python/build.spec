# -*- mode: python ; coding: utf-8 -*-
# PyInstaller 单文件打包：把 proxy.py + access.py（依赖仅 requests）打成 proxy-bin 可执行。
# 运行方式（在 electron/ 目录下）：
#   pyinstaller build-python/build.spec --distpath app/proxy-bin --workpath build-python/build --noconfirm
# 输出：app/proxy-bin/proxy-bin（mac）/ app/proxy-bin/proxy-bin.exe（win）
import os

HERE = os.getcwd()                      # CI/本地运行时 cwd 应为 electron/
APP_DIR = os.path.join(HERE, "app")     # 业务文件由 sync-app.js 复制到这里

block_cipher = None

a = Analysis(
    [os.path.join(APP_DIR, "proxy.py")],
    pathex=[APP_DIR],
    binaries=[],
    datas=[],
    hiddenimports=["access", "requests"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "unittest", "pydoc", "idlelib", "ensurepip"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
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
    name="proxy-bin",
    debug=False,
    boot_script=None,
    strip=False,
    upx=True,
    runtime_tmpdir=None,
    console=True,   # 保留控制台便于排查；如要彻底静默可改 False
)
