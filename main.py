#!/usr/bin/env python3
"""Wimmich - Startpunkt."""

from __future__ import annotations

import sys

from PyQt6.QtWidgets import QApplication

from wimmich import APP_NAME, __version__, theme
from wimmich.mainwindow import MainWindow


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)
    app.setOrganizationName("krichel")
    app.setStyle("Fusion")
    app.setStyleSheet(theme.STYLESHEET)

    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
