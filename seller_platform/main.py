r"""Точка входа форка упаковщика под Seller Platform.

Запуск из исходников:  ..\.venv\Scripts\python.exe main.py
Сборка .exe:           ..\.venv\Scripts\pyinstaller.exe seller-platform-packer.spec
config.ini (host / verify_ssl) лежит рядом с этим файлом / с .exe.
"""
import sys

import urllib3
from PyQt5.QtWidgets import QApplication

from api import VERIFY_SSL
from auth_gui import AuthGUI


def main():
    # Подавляем шум InsecureRequestWarning только когда TLS-проверка осознанно отключена
    # (локальная отладка). На проде verify_ssl=true — предупреждений нет, ничего не глушим.
    if not VERIFY_SSL:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    app = QApplication(sys.argv)
    window = AuthGUI()
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
