# PyInstaller spec for supr.bar — produces dist/suprbar/suprbar.exe
# Run:  pyinstaller --clean suprbar.spec
#
# Output is a *one-folder* bundle (faster startup than --onefile, easier to
# wrap with Inno Setup). The folder gets shipped via installer.iss.

from PyInstaller.utils.hooks import collect_submodules, collect_data_files

block_cipher = None

# Pull in everything pywebview / pystray load lazily.
hidden_imports = []
hidden_imports += collect_submodules("webview")
hidden_imports += collect_submodules("pystray")

# Static frontend files + any brand assets we ship in the bundle.
datas = []
datas += [("suprbar/static", "suprbar/static")]

a = Analysis(
    ["suprbar/__main__.py"],
    pathex=["."],
    binaries=[],
    datas=datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        # we don't need any of these — keep the bundle small
        "tkinter", "test", "unittest",
        "numpy", "pandas",
    ],
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
    name="suprbar",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,                 # tray app: no console window
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="suprbar/static/brand/suprbar.ico",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="suprbar",
)
