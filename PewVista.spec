# -*- mode: python ; coding: utf-8 -*-
# PyInstaller onedir recipe: collect the app's Python modules, native DLLs, and
# dynamic media/OBS imports under dist/PewVista/_internal.
# Runtime assets are copied beside the executable by build_desktop_app.bat.

from PyInstaller.utils.hooks import collect_submodules, copy_metadata

block_cipher = None

hidden_imports = [
    'obsws_python',
    'imageio',
    'imageio_ffmpeg',
    'pypdf',
    'PIL',
    'PIL.Image',
    'PIL.ImageDraw',
    'PIL.ImageFont',
    'PIL.ImageOps',
    'PIL.ImageSequence',
    'PIL.ImageTk',
    'sqlite3',
    'tkinter',
    'tkinter.ttk',
    'tkinter.font',
    'tkinter.messagebox',
    'tkinter.filedialog',
    'tkinter.colorchooser',
    'tkinter.simpledialog',
    'ctypes',
    'ctypes.wintypes',
]
hidden_imports += collect_submodules('obsws_python')
hidden_imports += collect_submodules('imageio')
hidden_imports += collect_submodules('sqlcipher3')
hidden_imports += collect_submodules('cryptography')

excludes = [
    'PySide6',
    'PySide6.QtCore',
    'PySide6.QtGui',
    'PySide6.QtWidgets',
    'shiboken6',
    'matplotlib',
    'scipy',
    'torch',
    'notebook',
    'IPython',
    'pytest',
    'unittest',
]

a = Analysis(
    ['main.py'],
    pathex=['.'],
    binaries=[],
    datas=copy_metadata('imageio'),
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='PewVista',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='Resourses\\PewVista.ico',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='PewVista',
)
