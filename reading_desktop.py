"""Desktop entry with a dedicated frameless window."""
import sys


def main():
    if '--headless' in sys.argv:
        from app.workspace_server import main as serve
        serve()
    else:
        from app.reading_shell import main as launch
        launch()


if __name__ == '__main__':
    main()
