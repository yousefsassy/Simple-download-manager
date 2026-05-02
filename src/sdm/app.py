from __future__ import annotations

import sys


def main() -> int:
    try:
        from PySide6.QtWidgets import QApplication
    except ModuleNotFoundError:
        print("PySide6 is not installed. Run: python -m pip install -e .")
        return 1

    from .ui.main_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("Simple Download Manager")
    window = MainWindow()
    window.resize(1180, 720)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
