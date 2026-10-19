# Separate runtime; never replace the capture helper's existing _runtime.
from pathlib import Path
root=Path(SPECPATH)
a=Analysis([str(root/'reading_desktop.py')],pathex=[str(root)],binaries=[],datas=[(str(root/'web'),'web')],hiddenimports=['pymupdf','bs4','PySide6'],hookspath=[],hooksconfig={},runtime_hooks=[],excludes=['pytest','tkinter'],noarchive=False)
# MuPDF's ICU must not shadow Windows ICU in QtWebEngine's child process.
# Python loads these dependencies next to the MuPDF extension instead.
a.binaries=[('pymupdf/'+name, source, kind) if name in {'icuuc.dll','icudt78.dll'} else (name,source,kind) for name,source,kind in a.binaries]
pyz=PYZ(a.pure)
exe=EXE(pyz,a.scripts,[],exclude_binaries=True,name='探索词典-初版1',debug=False,bootloader_ignore_signals=False,strip=False,upx=False,console=False,contents_directory='_reading_runtime_v7',icon=str(root/'assets/app.ico'))
coll=COLLECT(exe,a.binaries,a.datas,strip=False,upx=False,name='探索词典-初版1')
