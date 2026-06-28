"""Универсальный виджет этапа (форк upakovka_widget.py под seller-platform).

Один виджет вместо пяти furniture-виджетов. Флоу упаковщика (терминальный этап):
скан QR/ввод номера → validate_order → скачать этикетку заказа (single PDF) → печать →
send_work_process(EARNING). Чекбокс «Брак» → send_work_process(PENALTY) без печати.

Убрано относительно оригинала: чекбокс «Фасад» (furniture), bulk-режим «получить все
этикетки», переключатель «скачать и распечатать» (теперь единственный режим — по заказу).
Сохранено 1:1: _SCAN_LOCK, синхронная очистка поля + возврат фокуса (анти-потеря пика),
печать в фоновом потоке, сигналы UI.
"""
import os
import tempfile
import threading

import fitz
from PyQt5.QtCore import pyqtSignal, QObject
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QPushButton, QLineEdit, QTextEdit,
    QMessageBox, QLabel, QCheckBox, QHBoxLayout,
)

from api import (
    USER_CONTEXT, validate_order, send_work_process,
    download_package_by_order, print_pdf,
)

# Один скан за раз — иначе потоки топчут общее состояние и можно напечатать чужую этикетку.
_SCAN_LOCK = threading.Lock()


class WorkerSignals(QObject):
    message = pyqtSignal(str)
    show_warning = pyqtSignal(str, str)


class StageWidget(QWidget):
    def __init__(self, stage_code, stage_name=""):
        super().__init__()
        self.stage_code = stage_code
        self.stage_name = stage_name or stage_code
        self.setWindowTitle(self.stage_name)
        self.setGeometry(100, 100, 800, 600)
        font = QFont("Arial", 12)
        self.layout = QVBoxLayout()

        header = QLabel(f"Этап: {self.stage_name}")
        header.setFont(QFont("Arial", 16, QFont.Bold))
        self.layout.addWidget(header)

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Отсканируйте QR или введите номер заказа...")
        self.search_input.setFont(font)
        self.search_input.setStyleSheet("""
            QLineEdit { border: 2px solid #CCCCCC; border-radius: 10px; padding: 8px; }
            QLineEdit:focus { border: 2px solid #0078D7; }
        """)
        self.layout.addWidget(self.search_input)

        self.penalty_checkbox = QCheckBox("Брак")
        self.penalty_checkbox.setFont(QFont("Arial", 14, QFont.Bold))
        self.penalty_checkbox.setStyleSheet("""
            QCheckBox { color: red; spacing: 10px; }
            QCheckBox::indicator { width: 50px; height: 50px; }
            QCheckBox::indicator:checked { background-color: red; border: 2px solid black; border-radius: 5px; }
            QCheckBox::indicator:unchecked { background-color: white; border: 2px solid black; border-radius: 5px; }
        """)
        checkbox_row = QHBoxLayout()
        checkbox_row.addWidget(self.penalty_checkbox)
        checkbox_row.addStretch()
        self.layout.addLayout(checkbox_row)

        self.search_input.returnPressed.connect(self.search_text)

        self.search_button = QPushButton("Найти и распечатать")
        self.search_button.setFont(font)
        self.search_button.setMinimumHeight(45)
        self.search_button.setStyleSheet("""
            QPushButton { background-color: #0078D7; color: white; border: none; border-radius: 10px; }
            QPushButton:hover { background-color: #005A9E; }
        """)
        self.search_button.clicked.connect(self.search_text)
        self.layout.addWidget(self.search_button)

        self.console_label = QLabel("Консоль:")
        self.console_label.setFont(font)
        self.layout.addWidget(self.console_label)

        self.results = QTextEdit()
        self.results.setFont(font)
        self.results.setReadOnly(True)
        self.results.setStyleSheet("""
            QTextEdit { border: 1px solid #CCCCCC; border-radius: 8px; padding: 6px; background-color: #FAFAFA; }
        """)
        self.layout.addWidget(self.results)

        self.setLayout(self.layout)

        self.signals = WorkerSignals()
        self.signals.message.connect(self.append_console)
        self.signals.show_warning.connect(lambda t, m: QMessageBox.warning(self, t, m))

        self.search_input.setFocus()

    # === UI ===
    def append_console(self, text):
        self.results.append(text)
        self.results.verticalScrollBar().setValue(self.results.verticalScrollBar().maximum())

    def clear_search_input(self):
        self.search_input.clear()
        self.search_input.setFocus()
        self.penalty_checkbox.setChecked(False)

    # === Логика ===
    def search_text(self):
        query = self.search_input.text().strip()
        if not query:
            self.signals.show_warning.emit("Ошибка", "Введите номер заказа!")
            return

        # Состояние чекбокса читаем СЕЙЧАС (UI-поток): clear ниже его сбросит.
        operation_type = "PENALTY" if self.penalty_checkbox.isChecked() else "EARNING"

        # СРАЗУ освобождаем поле и возвращаем фокус — сканер примет следующий QR без потери,
        # пока этот заказ обрабатывается в фоне (валидация + загрузка PDF + печать).
        self.clear_search_input()

        def dispatch():
            with _SCAN_LOCK:
                success, message = validate_order(query)
                if not success:
                    self.signals.message.emit(f"❌ Валидация заказа не пройдена: {message}")
                    return
                if message:
                    self.signals.message.emit(f"ℹ️ {message}")

                if operation_type == "PENALTY":
                    self.signals.message.emit(f"⚠️ Брак по заказу {query} — печать пропущена.")
                    ok, msg = send_work_process(query, "PENALTY")
                    self.signals.message.emit(("✅ " if ok else "❌ ") + (msg or ""))
                    return

                self._earning_flow(query)

        threading.Thread(target=dispatch, daemon=True).start()

    def _earning_flow(self, query):
        if not USER_CONTEXT:
            self.signals.show_warning.emit("Ошибка", "Нет авторизованных пользователей!")
            return
        username = USER_CONTEXT[0]["username"]

        self.signals.message.emit(f"📦 Получение этикетки по заказу {query} ...")
        ok_dl, msg, pdf_bytes = download_package_by_order(username, query)
        if not ok_dl or not pdf_bytes:
            self.signals.message.emit(f"❌ {msg}")
            return

        try:
            temp_pdf_path = os.path.join(tempfile.gettempdir(), "sp_single_package.pdf")
            with open(temp_pdf_path, "wb") as f:
                f.write(pdf_bytes)
            # Печатаем первую (единственную) страницу заказа.
            with fitz.open(temp_pdf_path) as doc:
                page_count = doc.page_count
            if page_count == 0:
                self.signals.message.emit(f"⚠️ Пустой PDF этикетки заказа {query}.")
                return
        except Exception as e:
            self.signals.message.emit(f"❌ Ошибка обработки PDF заказа {query}: {e}")
            return

        self.signals.message.emit("🖨️ Отправка на печать...")
        success = print_pdf(temp_pdf_path)
        if not success:
            self.signals.message.emit("⚠️ Принтер не найден или недоступен — ЗП не начислена.")
            return

        self.signals.message.emit("✅ Печать выполнена успешно!")
        ok_wp, wp_msg = send_work_process(query, "EARNING")
        self.signals.message.emit(("✅ " if ok_wp else "❌ ") + (wp_msg or ""))
