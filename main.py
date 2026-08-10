#!/usr/bin/env python3
"""Wimmich - Startpunkt.

Copyright (C) 2026 Harald Krichel. Freie Software unter der GNU GPL v3
oder später; siehe LICENSE. Ohne jede Gewährleistung.
"""

from __future__ import annotations

import sys

from PyQt6.QtWidgets import QApplication

from wimmich import APP_NAME, __version__, theme
from wimmich.config import Config
from wimmich.icon import app_icon
from wimmich.mainwindow import MainWindow


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)
    app.setOrganizationName("krichel")
    app.setStyle("Fusion")
    app.setWindowIcon(app_icon())

    # Gespeichertes Erscheinungsbild (hell/dunkel) schon vor dem ersten
    # Fenster anwenden, damit nichts kurz im falschen Thema aufblitzt.
    startup_config = Config()
    theme.set_theme(str(startup_config["theme"] or "dunkel"))
    app.setStyleSheet(theme.STYLESHEET)

    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
