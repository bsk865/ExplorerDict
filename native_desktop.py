"""Native desktop entry; ships separately from the legacy reader."""
import argparse
import os
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir', type=Path)
    parser.add_argument('--verify-native', action='store_true')
    parser.add_argument('--verify-data-copy', action='store_true')
    parser.add_argument('--verify-relocation', type=Path)
    parser.add_argument('--relocation-stage', choices=('schedule', 'reopen'), default='schedule')
    args = parser.parse_args()
    if args.verify_relocation:
        if args.data_dir or args.verify_native or args.verify_data_copy:
            parser.error('Relocation verification uses its own isolated paths')
        from app.native.relocation_check import prepare_environment
        prepare_environment(args.verify_relocation, args.relocation_stage)
    isolated_ui = args.verify_native or bool(args.verify_relocation)
    if (args.verify_native or args.verify_data_copy) and not args.data_dir:
        parser.error('Verification requires an isolated --data-dir')
    if args.verify_data_copy and not (args.data_dir / '.native-compatibility-copy').is_file():
        parser.error('Data verification requires a prepared compatibility copy')
    if args.data_dir:
        os.environ['EXPLORER_DICT_DATA_DIR'] = str(args.data_dir.resolve())
    if args.verify_data_copy:
        os.environ['EXPLORER_DICT_STORAGE_CONFIG'] = str(args.data_dir.resolve() / 'storage-location.json')
        from app.native.compatibility import check_copy
        return check_copy(args.data_dir.resolve())
    from PySide6.QtWidgets import QApplication, QMessageBox
    from PySide6.QtCore import QTimer
    from app.paths import data_dir
    from app.single_instance import SingleInstance, data_dir_digest
    from app.activation import ActivationReceiver
    from app import capture_bridge
    app = QApplication(sys.argv[:1])
    if isolated_ui and app.platformName() == 'offscreen':
        from PySide6.QtGui import QFontDatabase, QFont
        fonts = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts'
        family = None
        for name in ('msyh.ttc', 'msyhbd.ttc'):
            font_id = QFontDatabase.addApplicationFont(str(fonts / name))
            families = QFontDatabase.applicationFontFamilies(font_id)
            if families and family is None:
                family = families[0]
        if family:
            app.setFont(QFont(family, 10))
    directory = data_dir()
    os.environ['EXPLORER_DICT_DATA_DIR'] = str(directory.resolve())
    if args.verify_native:
        os.environ['EXPLORER_DICT_STORAGE_CONFIG'] = str(directory / 'storage-location.json')
    identity = 'Local\\ExplorerDict.Reader.' + data_dir_digest(directory)
    guard = SingleInstance(name=identity)
    if not guard.acquire():
        capture_bridge.signal_event(identity + '.Show')
        return 0
    receiver = ActivationReceiver(name=identity + '.Show')
    receiver.create()
    from app.native.service import NativeService
    from app.native.window import NativeWindow
    service = NativeService(directory)
    window = NativeWindow(service, discovery_updates=not isolated_ui)
    window.capture_enabled = not isolated_ui
    activation = QTimer(window)
    def reveal():
        if receiver.poll():
            window.showNormal()
            window.raise_()
            window.activateWindow()
    activation.timeout.connect(reveal)
    activation.start(200)
    if not isolated_ui:
        window.run(capture_bridge.start_on_launch, lambda _: None)
    if args.verify_native:
        from app.native.verify import attach_verification
        attach_verification(window, directory)
    if args.verify_relocation:
        from app.native.relocation_check import attach_check
        attach_check(window, args.verify_relocation.resolve(), args.relocation_stage)
    window.show()
    try:
        return app.exec()
    finally:
        service.close()
        receiver.close()
        try:
            if not args.verify_native:
                stopped = True if args.verify_relocation else capture_bridge.stop_and_wait()
                from app.storage_location import apply_pending
                apply_pending(directory, components_stopped=stopped)
        finally:
            guard.release()


if __name__ == '__main__':
    raise SystemExit(main())
