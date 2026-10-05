# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build description for the AnkiGPT Desktop backend.

Run through desktop-app/scripts/build-backend.ps1, which builds from a clean virtual
environment and then runs the self-check. See desktop-app/SPEC.md section 8.1.

  - One-folder build. A one-file build unpacks itself to a temp folder on every start,
    which is slow and is the pattern antivirus tools flag most often.
  - Console subsystem. The Electron shell spawns it hidden; a windowed build would
    have no stdout, and stdout is how the backend talks to the shell.
  - No UPX, which also triggers antivirus false positives.
"""

import os

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

BACKEND = SPECPATH  # noqa: F821  (set by PyInstaller: the folder holding this file)
DESKTOP = os.path.dirname(BACKEND)
ROOT = os.path.dirname(DESKTOP)

datas = [
    (os.path.join(ROOT, "app", "templates"), "app/templates"),
    (os.path.join(ROOT, "app", "static"), "app/static"),
    (os.path.join(BACKEND, "selfcheck", "sample.pdf"), "selfcheck"),
]
# The PDF stack is the part that fails quietly. `app/services/pdf.py` imports the layout
# library inside `try` blocks and falls back to plain text when anything is missing, so a
# bundle without the ONNX models or their YAML files would not crash: it would just read
# every PDF worse. Collect the data explicitly, and let --self-check prove it worked.
# `mupdf-devel` is headers and import libraries for compiling against MuPDF; not needed.
datas += collect_data_files("pymupdf", excludes=["mupdf-devel/**"])
datas += collect_data_files("pymupdf4llm")

binaries = collect_dynamic_libs("pymupdf") + collect_dynamic_libs("onnxruntime")

hiddenimports = (
    # Imported inside `try` blocks or by name, where PyInstaller's analysis can miss them.
    collect_submodules("pymupdf.layout")
    + collect_submodules("pymupdf4llm")
    + ["onnxruntime", "waitress", "sqlalchemy.dialects.sqlite"]
)

a = Analysis(  # noqa: F821
    [os.path.join(BACKEND, "entry.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # gunicorn is the web deployment's server and does not run on Windows. The rest are
    # standard-library or build-time packages the app never imports.
    excludes=["gunicorn", "tkinter", "pytest", "PyInstaller", "pip", "setuptools", "wheel"],
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ankigpt-backend",
    icon=os.path.join(DESKTOP, "resources", "icon.ico"),
    console=True,
    upx=False,
    debug=False,
    strip=False,
    disable_windowed_traceback=False,
)
coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="ankigpt-backend",
)
