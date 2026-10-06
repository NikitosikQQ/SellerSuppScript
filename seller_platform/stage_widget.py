"""Универсальный виджет этапа (форк upakovka_widget.py под seller-platform).

Один виджет вместо пяти furniture-виджетов. Два режима печати этикеток (как в цеху):

1) BULK (основной, кнопка «Получить актуальные этикетки»): один PDF со всеми этикетками
   незавершённых заказов грузится локально; каждый скан ищет в нём страницу и печатает её
   БЕЗ сетевого вызова за этикеткой — быстро на потоке, устойчиво к сети.
2) ОДИНОЧНЫЙ (чекбокс «Скачать и распечатать»): этикетка тянется по одному заказу
   (`/orders/{n}/package`) на каждый скан — добор заказа, которого нет в bulk-наборе.

Поиск страницы — label_lookup: сначала по коду этикетки из ответа валидации (labelCode; у
OZON с 05.10.2026 — scanit «ii…», номера отправления на новой этикетке нет), затем прежний
поиск по номеру (старый макет OZON, WB, ЯМ). Код на нескольких страницах — отказ печати.

Оба режима при печати начисляют ЗП по stageCode (EARNING); «Брак» → PENALTY без печати.
Дедуп на бэке (canAdvanceTo) защищает от двойного начисления при перепечатке.

Убрано относительно seller-support: чекбокс «Фасад» (furniture). Сохранено 1:1: _SCAN_LOCK,
синхронная очистка поля + возврат фокуса (анти-потеря пика), печать в фоне.
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
    download_packages, download_package_by_order, print_pdf, play_notification_sound,
)
from label_lookup import (
    AMBIGUOUS_CODE, NOT_FOUND, build_code_index, resolve_page, resolve_single_order_page,
    result_message,
)

# Один скан за раз — иначе потоки топчут общее состояние и можно напечатать чужую этикетку.
_SCAN_LOCK = threading.Lock()


class WorkerSignals(QObject):
    message = pyqtSignal(str)
    clear = pyqtSignal()
    set_path = pyqtSignal(str)
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

        self.path_input = QLineEdit()
        self.path_input.setPlaceholderText("Путь к загруженному PDF появится здесь")
        self.path_input.setFont(font)
        self.path_input.setReadOnly(True)
        self.path_input.setStyleSheet("""
            QLineEdit { border: 2px solid #CCCCCC; border-radius: 10px; padding: 8px; }
            QLineEdit:focus { border: 2px solid #0078D7; }
        """)
        self.layout.addWidget(self.path_input)

        self.load_button = QPushButton("Получить актуальные этикетки")
        self.load_button.setFont(font)
        self.load_button.setMinimumHeight(45)
        self.load_button.clicked.connect(self.fetch_labels_from_server)
        self.load_button.setStyleSheet("""
            QPushButton { background-color: #0078D7; color: white; border: none; border-radius: 10px; }
            QPushButton:hover { background-color: #005A9E; }
        """)
        self.layout.addWidget(self.load_button)

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

        self.download_and_print_checkbox = QCheckBox("Скачать и распечатать")
        self.download_and_print_checkbox.setFont(QFont("Arial", 14, QFont.Bold))
        self.download_and_print_checkbox.setStyleSheet("""
            QCheckBox { color: grey; spacing: 10px; }
            QCheckBox::indicator { width: 50px; height: 50px; }
            QCheckBox::indicator:checked { background-color: grey; border: 2px solid black; border-radius: 5px; }
            QCheckBox::indicator:unchecked { background-color: white; border: 2px solid black; border-radius: 5px; }
        """)

        checkbox_row = QHBoxLayout()
        checkbox_row.addWidget(self.penalty_checkbox)
        checkbox_row.addSpacing(20)
        checkbox_row.addWidget(self.download_and_print_checkbox)
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

        # Bulk-набор (загружается кнопкой) и одиночный (на каждый скан в режиме «Скачать и распечатать»).
        self.doc = None
        self.pages_text = None
        # Код этикетки (OZON scanit «ii…») → страницы bulk-PDF; строится при загрузке PDF.
        self.code_index = {}
        self.single_doc = None
        self.single_pages_text = None

        self.signals = WorkerSignals()
        self.signals.message.connect(self.append_console)
        self.signals.clear.connect(self.clear_search_input)
        self.signals.set_path.connect(self.path_input.setText)
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
        # «Скачать и распечатать» — залипающий режим, НЕ сбрасываем между сканами.

    # === Bulk-загрузка этикеток ===
    def fetch_labels_from_server(self):
        if not USER_CONTEXT:
            self.signals.show_warning.emit("Ошибка", "Нет авторизованных пользователей!")
            return
        username = USER_CONTEXT[0]["username"]
        self.signals.message.emit("📦 Получение актуальных этикеток...")

        def worker():
            success, msg, pdf_bytes = download_packages(username, False)
            if msg:
                self.signals.message.emit(msg)
            if not success or not pdf_bytes:
                return
            try:
                # Документ — из памяти, а не из файла: файл перезаписывается при каждой загрузке,
                # а открытый с диска документ MuPDF дочитывает по требованию — и мог бы прочитать
                # страницы уже из нового PDF. Файл ниже — только копия для поля пути.
                doc = fitz.open(stream=pdf_bytes, filetype="pdf")
                pages_text = [doc[p].get_text("text").splitlines() for p in range(len(doc))]
                code_index = build_code_index(pages_text)
                # Подмена набора — под тем же замком, что и обработка скана: иначе скан мог найти
                # страницу в одном PDF, а напечатать её из другого (чужая этикетка).
                with _SCAN_LOCK:
                    self.doc, self.pages_text, self.code_index = doc, pages_text, code_index
            except Exception as e:
                self.signals.message.emit(f"❌ Ошибка обработки PDF: {e}")
                return
            try:
                temp_pdf_path = os.path.join(tempfile.gettempdir(), "sp_packages.pdf")
                with open(temp_pdf_path, "wb") as f:
                    f.write(pdf_bytes)
                self.signals.set_path.emit(temp_pdf_path)
            except OSError as e:
                # Копия на диске нужна только для поля пути — печать идёт из памяти.
                self.signals.message.emit(f"ℹ️ Копию PDF на диск сохранить не удалось: {e}")
            self.signals.message.emit(
                f"✅ PDF загружен ({len(doc)} страниц, кодов этикеток OZON: {len(code_index)}). "
                f"Сканируйте заказы.")

        threading.Thread(target=worker, daemon=True).start()

    # === Скан ===
    def search_text(self):
        query = self.search_input.text().strip()
        is_download = self.download_and_print_checkbox.isChecked()
        if not query:
            self.signals.show_warning.emit("Ошибка", "Введите номер заказа!")
            return
        if not is_download and not self.doc:
            self.signals.show_warning.emit(
                "Ошибка", "Сначала нажмите «Получить актуальные этикетки» или включите «Скачать и распечатать».")
            return

        # Состояние чекбоксов читаем СЕЙЧАС (UI-поток): clear ниже сбросит «Брак».
        operation_type = "PENALTY" if self.penalty_checkbox.isChecked() else "EARNING"

        # СРАЗУ освобождаем поле и возвращаем фокус — сканер примет следующий QR без потери,
        # пока этот заказ обрабатывается в фоне (валидация + поиск/загрузка + печать).
        self.clear_search_input()

        def dispatch():
            with _SCAN_LOCK:
                # label_code — код с этикетки (OZON scanit «ii…»): по нему ищется страница.
                ok, msg, label_code = validate_order(query)
                if not ok:
                    self.signals.message.emit(f"❌ Валидация заказа не пройдена: {msg}")
                    return
                if msg:
                    self.signals.message.emit(f"ℹ️ {msg}")

                if operation_type == "PENALTY":
                    self.signals.message.emit(f"⚠️ Брак по заказу {query} — печать пропущена.")
                    self._accrue(query, "PENALTY")
                    return

                self.signals.message.emit(f"🔍 Поиск {query} ...")
                if is_download:
                    self._single_flow(query, label_code)
                else:
                    self._bulk_flow(query, label_code)

        threading.Thread(target=dispatch, daemon=True).start()

    # === Режимы EARNING ===
    def _bulk_flow(self, query, label_code):
        # Снимок под _SCAN_LOCK (dispatch держит замок): поиск и печать — по одному PDF.
        doc, pages_text, code_index = self.doc, self.pages_text, self.code_index
        page, how = resolve_page(pages_text, code_index, query, label_code)
        if page is None:
            if how == NOT_FOUND:
                # Привычная цеху формулировка; остальные исходы — сообщения label_lookup.
                self.signals.message.emit(f"⚠️ Заказ {query} не найден в загруженных этикетках.")
            else:
                self.signals.message.emit(result_message(how, query, label_code, page))
            if how == AMBIGUOUS_CODE:
                play_notification_sound()
            return
        self.signals.message.emit(result_message(how, query, label_code, page))
        self.signals.message.emit("🖨️ Печать...")
        if self._extract_and_print(doc, page):
            self.signals.message.emit("✅ Печать выполнена успешно!")
            self._accrue(query, "EARNING")
        else:
            self.signals.message.emit("⚠️ Принтер не найден или недоступен — ЗП не начислена.")

    def _single_flow(self, query, label_code):
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
            # Из памяти, как и bulk-набор: временный файл здесь не нужен.
            self.single_doc = fitz.open(stream=pdf_bytes, filetype="pdf")
            self.single_pages_text = [
                self.single_doc[p].get_text("text").splitlines() for p in range(len(self.single_doc))
            ]
        except Exception as e:
            self.signals.message.emit(f"❌ Ошибка обработки PDF заказа {query}: {e}")
            return
        if self.single_doc.page_count == 0:
            self.signals.message.emit(f"⚠️ Пустой PDF этикетки заказа {query}.")
            return

        # Сервер отдал PDF только этого заказа: единственная страница печатается, даже если ни
        # код, ни номер в тексте не нашлись; несколько страниц без совпадения — не гадаем.
        page, how = resolve_single_order_page(self.single_pages_text, query, label_code)
        self.signals.message.emit(result_message(how, query, label_code, page))
        if page is None:
            if how == AMBIGUOUS_CODE:
                play_notification_sound()
            return
        self.signals.message.emit("🖨️ Печать...")
        if self._extract_and_print(self.single_doc, page):
            self.signals.message.emit("✅ Печать выполнена успешно!")
            self._accrue(query, "EARNING")
        else:
            self.signals.message.emit("⚠️ Принтер не найден или недоступен — ЗП не начислена.")

    # === Хелперы ===
    def _accrue(self, query, operation_type):
        ok, msg = send_work_process(query, operation_type)
        self.signals.message.emit(("✅ " if ok else "❌ ") + (msg or ""))

    def _extract_and_print(self, doc, page_num):
        temp = tempfile.mktemp("sp_pkg.pdf")
        writer = fitz.open()
        writer.insert_pdf(doc, from_page=page_num, to_page=page_num)
        writer.save(temp)
        writer.close()
        return print_pdf(temp)
