# Native reader runtime, isolated from both legacy executable directories.
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, copy_metadata

root = Path(SPECPATH)
data = [(str(root/'web/index.html'), 'web'),
        (str(root/'web/assets/generated'), 'web/assets/generated'),
        (str(root/'models/multilingual-e5-small'), 'models/multilingual-e5-small'),
        (str(root/'assets/app.ico'), 'assets'),
        (str(root/'assets/native-chevron.svg'), 'assets'),
        (str(root/'assets/native-chevron-up.svg'), 'assets')]
data += [(str(path), 'web') for path in (root/'web').glob('*.css')]
data += [(str(path), 'web/assets') for path in (root/'web/assets').glob('*brand-mark.svg')]
data += [(str(root/'web/assets/native-icons.json'), 'web/assets')]
for package in ('ziamath', 'ziafont', 'latex2mathml'):
    data += collect_data_files(package) + copy_metadata(package)
a = Analysis([str(root/'native_desktop.py')], pathex=[str(root)], binaries=[], datas=data,
             hiddenimports=['pymupdf', 'bs4', 'onnxruntime', 'tokenizers', 'numpy'],
             hookspath=[], hooksconfig={}, runtime_hooks=[],
             excludes=['pytest', 'tkinter', 'PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets',
                       'PySide6.QtWebEngineQuick', 'PySide6.QtQml', 'PySide6.QtQuick'], noarchive=False)
a.binaries = [('pymupdf/'+name, source, kind) if name in {'icuuc.dll', 'icudt78.dll'} else (name, source, kind)
              for name, source, kind in a.binaries]
assert not any('webengine' in name.lower() for name, _, _ in a.binaries), 'Browser engine entered native runtime'
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='探索词典-原生版', debug=False,
          bootloader_ignore_signals=False, strip=False, upx=False, console=False,
          contents_directory='_native_runtime', icon=str(root/'assets/app.ico'))
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='探索词典-原生版')
