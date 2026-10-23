# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec: one onedir bundle with three executables (docs/m2-design.md §6).

    uv run pyinstaller RunnerDeck.spec --noconfirm --clean   ->   dist/RunnerDeck/

- RunnerDeck.exe        windowed GUI (src/runnerdeck/__main__.py), carries the icon
- runnerdeck-helper.exe console helper that owns run.cmd's console (stdlib only)
- runnerdeck-cli.exe    console CLI for troubleshooting (scripts/entry_cli.py)

All three use exclude_binaries=True and share a single COLLECT, so the Qt / Python runtime is
shipped once. Windows only: the hidden imports for keyring's Windows backend
(pywin32-ctypes) resolve to nothing elsewhere, which is harmless for a parse check.
"""

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

APP_NAME = "RunnerDeck"
ICON = "src/runnerdeck/ui/resources/runnerdeck.ico"
RESOURCES = ("src/runnerdeck/ui/resources", "runnerdeck/ui/resources")

# tkinter is never used; PyInstaller would otherwise bundle Tcl/Tk when it is importable.
COMMON_EXCLUDES = ["tkinter", "_tkinter", "Tkinter", "tcl", "tk"]
# Nothing in the console executables needs Qt.
NO_QT = ["PyQt5", "qfluentwidgets", "PySide2", "PySide6", "PyQt6"]

# keyring finds its Windows backend by import and talks to the credential manager through
# pywin32-ctypes (win32ctypes.pywin32 / win32ctypes.core, loaded dynamically).
KEYRING_HIDDEN = [
    *collect_submodules("keyring.backends"),
    "keyring.backends.Windows",
    *collect_submodules("win32ctypes"),
    "win32ctypes.pywin32",
    "win32ctypes.core",
]
# runnerdeck.__version__ reads its own distribution metadata; keyring enumerates backend
# entry points through importlib.metadata. truststore reads no metadata at runtime.
METADATA = [*copy_metadata("runnerdeck"), *copy_metadata("keyring")]

# ---- RunnerDeck.exe (GUI) ----------------------------------------------------------------
a_gui = Analysis(
    ["src/runnerdeck/__main__.py"],
    pathex=["src"],
    binaries=[],
    datas=[*collect_data_files("qfluentwidgets"), RESOURCES, *METADATA],
    hiddenimports=[
        *collect_submodules("qfluentwidgets"),
        *KEYRING_HIDDEN,
        "truststore",
        "runnerdeck.app.controller",
        "runnerdeck.ui.main_window",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[*COMMON_EXCLUDES],
    noarchive=False,
    optimize=0,
)
pyz_gui = PYZ(a_gui.pure)
exe_gui = EXE(
    pyz_gui,
    a_gui.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
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
    icon=ICON,
)

# ---- runnerdeck-helper.exe (console, stdlib only) ------------------------------------------
a_helper = Analysis(
    ["src/runnerdeck/core/console_helper.py"],
    pathex=["src"],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        *COMMON_EXCLUDES,
        *NO_QT,
        "runnerdeck",
        "httpx",
        "pydantic",
        "keyring",
        "psutil",
        "truststore",
    ],
    noarchive=False,
    optimize=0,
)
pyz_helper = PYZ(a_helper.pure)
exe_helper = EXE(
    pyz_helper,
    a_helper.scripts,
    [],
    exclude_binaries=True,
    name="runnerdeck-helper",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

# ---- runnerdeck-cli.exe (console) ------------------------------------------------------------
a_cli = Analysis(
    ["scripts/entry_cli.py"],
    pathex=["src"],
    binaries=[],
    datas=[*METADATA],
    hiddenimports=[*KEYRING_HIDDEN, "truststore"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[*COMMON_EXCLUDES, *NO_QT],
    noarchive=False,
    optimize=0,
)
pyz_cli = PYZ(a_cli.pure)
exe_cli = EXE(
    pyz_cli,
    a_cli.scripts,
    [],
    exclude_binaries=True,
    name="runnerdeck-cli",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

# ---- dist/RunnerDeck/ ------------------------------------------------------------------------
coll = COLLECT(
    exe_gui,
    a_gui.binaries,
    a_gui.datas,
    exe_helper,
    a_helper.binaries,
    a_helper.datas,
    exe_cli,
    a_cli.binaries,
    a_cli.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=APP_NAME,
)
