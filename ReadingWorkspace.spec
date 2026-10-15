# Separate runtime; never replace the capture helper's existing _runtime.
from pathlib import Path
root=Path(SPECPATH)
a=Analysis([str(root/'reading_desktop.py')],pathex=[str(root)],binaries=[],datas=[(str(root/'web'),'web')],hiddenimports=['pymupdf'],hookspath=[],hooksconfig={},runtime_hooks=[],excludes=['pytest','tkinter'],noarchive=False)
pyz=PYZ(a.pure)
exe=EXE(pyz,a.scripts,[],exclude_binaries=True,name='探索词典-阅读版6',debug=False,bootloader_ignore_signals=False,strip=False,upx=False,console=False,contents_directory='_reading_runtime_v6',icon=str(root/'assets/app.ico'))
coll=COLLECT(exe,a.binaries,a.datas,strip=False,upx=False,name='探索词典-阅读版6')
