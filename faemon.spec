from PyInstaller.utils.hooks import collect_all

_yt_datas, _yt_bins, _yt_hidden = collect_all("yt_dlp")
_ytm_datas, _ytm_bins, _ytm_hidden = collect_all("ytmusicapi")
# certifi's cacert.pem is the single CA trust source for every outbound
# httpx call. The stdlib OpenSSL default paths are empty in frozen apps
# on macOS runners, so the bundle must be bundled explicitly.
_cert_datas, _cert_bins, _cert_hidden = collect_all("certifi")

a = Analysis(
    ["src/entry.py"],
    pathex=["src"],
    binaries=_yt_bins + _ytm_bins + _cert_bins,
    datas=_yt_datas + _ytm_datas + _cert_datas,
    hiddenimports=[
        # uvicorn — auto-selected protocol/loop/lifespan implementations
        "uvicorn.logging",
        "uvicorn.loops",
        "uvicorn.loops.auto",
        "uvicorn.protocols",
        "uvicorn.protocols.http",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan",
        "uvicorn.lifespan.on",
    ]
    + _yt_hidden
    + _ytm_hidden
    + _cert_hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="faemon",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
