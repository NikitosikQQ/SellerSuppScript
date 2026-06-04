import os
import platform
import re
import tempfile
import threading

import fitz
from PyQt5.QtCore import pyqtSignal, QObject
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QPushButton, QLineEdit, QTextEdit,
    QMessageBox, QLabel, QCheckBox, QHBoxLayout, QApplication
)
import time as _time

# Диагностика ввода сканера / смены фокуса (баг потери второго пика, кража фокуса Xprinter).
# По умолчанию ВЫКЛЮЧЕНА — релизная сборка лог не пишет. Включается переменной окружения
# SS_SCAN_DEBUG=1: тогда пишет в %TEMP%\scan_debug.log с миллисекундными метками.
_SCAN_DEBUG = os.environ.get("SS_SCAN_DEBUG") == "1"
_DBG_PATH = os.path.join(tempfile.gettempdir(), "scan_debug.log")


def _dbg(msg):
    if not _SCAN_DEBUG:
        return
    try:
        with open(_DBG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{_time.strftime('%H:%M:%S')}.{int(_time.time() * 1000) % 1000:03d}  {msg}\n")
    except Exception:
        pass


_DBG_FOCUS_HOOKED = False

from seller_supp_api import download_packages, send_work_process, USER_CONTEXT, \
    validate_order, download_package_by_order, print_pdf  # импортируем методы и контекст

if platform.system() == "Windows":
    import win32api

# Сериализация обработки сканов: пики принимаются мгновенно (поле чистится сразу,
# UI не виснет), но обрабатываются СТРОГО ПО ОДНОМУ. Иначе два потока топчут общее
# состояние (self.single_doc, фикс. temp-файлы) → можно напечатать чужую этикетку.
_SCAN_LOCK = threading.Lock()


class WorkerSignals(QObject):
    message = pyqtSignal(str)
    clear = pyqtSignal()
    set_path = pyqtSignal(str)
    show_warning = pyqtSignal(str, str)


class UpakovkaWidget(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Упаковка")
        self.setGeometry(100, 100, 800, 600)
        font = QFont("Arial", 12)
        self.layout = QVBoxLayout()

        # === Интерфейс ===
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
        self.search_input.setPlaceholderText("Введите номер заказа...")
        self.search_input.setFont(font)
        # Диагностика (только при SS_SCAN_DEBUG=1): лог ввода в поле + глобальный лог смены фокуса
        if _SCAN_DEBUG:
            self.search_input.textChanged.connect(lambda t: _dbg(f"textChanged: '{t}'"))
            global _DBG_FOCUS_HOOKED
            if not _DBG_FOCUS_HOOKED:
                app = QApplication.instance()
                if app is not None:
                    app.focusChanged.connect(
                        lambda old, new: _dbg(
                            f"focusChanged: {type(old).__name__ if old else None} -> {type(new).__name__ if new else None}"))
                    _DBG_FOCUS_HOOKED = True
        self.search_input.setStyleSheet("""
            QLineEdit { border: 2px solid #CCCCCC; border-radius: 10px; padding: 8px; }
            QLineEdit:focus { border: 2px solid #0078D7; }
        """)
        self.layout.addWidget(self.search_input)

        self.penalty_checkbox = QCheckBox("Брак")
        self.penalty_checkbox.setFont(QFont("Arial", 14, QFont.Bold))
        self.penalty_checkbox.setStyleSheet("""
            QCheckBox {
                color: red;
                spacing: 10px;
            }
            QCheckBox::indicator {
                width: 50px;
                height: 50px;
            }
            QCheckBox::indicator:checked {
                background-color: red;
                border: 2px solid black;
                border-radius: 5px;
            }
            QCheckBox::indicator:unchecked {
                background-color: white;
                border: 2px solid black;
                border-radius: 5px;
            }
        """)

        self.facade_checkbox = QCheckBox("Фасад")
        self.facade_checkbox.setFont(QFont("Arial", 14, QFont.Bold))
        self.facade_checkbox.setStyleSheet("""
                    QCheckBox {
                        color: green;
                        spacing: 10px;
                    }
                    QCheckBox::indicator {
                        width: 50px;
                        height: 50px;
                    }
                    QCheckBox::indicator:checked {
                        background-color: green;
                        border: 2px solid black;
                        border-radius: 5px;
                    }
                    QCheckBox::indicator:unchecked {
                        background-color: white;
                        border: 2px solid black;
                        border-radius: 5px;
                    }
                """)

        self.download_and_print_checkbox = QCheckBox("Скачать и распечатать")
        self.download_and_print_checkbox.setFont(QFont("Arial", 14, QFont.Bold))
        self.download_and_print_checkbox.setStyleSheet("""
                            QCheckBox {
                                color: grey;
                                spacing: 10px;
                            }
                            QCheckBox::indicator {
                                width: 50px;
                                height: 50px;
                            }
                            QCheckBox::indicator:checked {
                                background-color: grey;
                                border: 2px solid black;
                                border-radius: 5px;
                            }
                            QCheckBox::indicator:unchecked {
                                background-color: white;
                                border: 2px solid black;
                                border-radius: 5px;
                            }
                        """)

        # Создаем горизонтальный контейнер
        checkbox_row = QHBoxLayout()
        # Добавляем чекбоксы в этот горизонтальный layout
        checkbox_row.addWidget(self.penalty_checkbox)
        checkbox_row.addSpacing(20)
        checkbox_row.addWidget(self.facade_checkbox)
        checkbox_row.addSpacing(20)
        checkbox_row.addWidget(self.download_and_print_checkbox)

        # Можно добавить растяжку, чтобы они не прилипали к левому краю
        checkbox_row.addStretch()

        # Добавляем горизонтальный layout в основной вертикальный layout
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
        self.doc = None
        self.pages_text = None

        self.single_doc = None
        self.single_pages_text = None


        # === Сигналы ===
        self.signals = WorkerSignals()
        self.signals.message.connect(self.append_console)
        self.signals.clear.connect(self.clear_search_input)
        self.signals.set_path.connect(self.path_input.setText)
        self.signals.show_warning.connect(lambda t, m: QMessageBox.warning(self, t, m))

    # === Методы интерфейса ===
    def append_console(self, text):
        self.results.append(text)
        self.results.verticalScrollBar().setValue(self.results.verticalScrollBar().maximum())

    def clear_search_input(self):
        self.search_input.clear()
        self.search_input.setFocus()
        self.penalty_checkbox.setChecked(False)
        self.facade_checkbox.setChecked(False)
        self.download_and_print_checkbox.setChecked(False)

    # === Логика ===
    def fetch_labels_from_server(self):
        if not USER_CONTEXT:
            self.signals.show_warning.emit("Ошибка", "Нет авторизованных пользователей!")
            return

        username = USER_CONTEXT[0]["username"]
        self.signals.message.emit("📦 Получение актуальных этикеток...")

        def worker():
            success, msg, pdf_bytes = download_packages(username, False)
            self.signals.message.emit(msg)
            if not success or not pdf_bytes:
                return
            try:
                temp_pdf_path = os.path.join(tempfile.gettempdir(), "packages.pdf")
                with open(temp_pdf_path, "wb") as f:
                    f.write(pdf_bytes)

                self.doc = fitz.open(temp_pdf_path)
                self.pages_text = [
                    self.doc[page].get_text("text").splitlines()
                    for page in range(len(self.doc))
                ]
                self.signals.set_path.emit(temp_pdf_path)
                self.signals.message.emit(f"✅ PDF загружен ({len(self.doc)} страниц).")
            except Exception as e:
                self.signals.message.emit(f"❌ Ошибка обработки PDF: {e}")

        threading.Thread(target=worker, daemon=True).start()

    def send_to_server(self, query, operation_type):
        def sender():
            success, msg = send_work_process(query, operation_type)
            if success:
                self.signals.message.emit(f"✅ {msg}")
            else:
                self.signals.message.emit(f"❌ Ошибка при обработке: {msg}")
        threading.Thread(target=sender, daemon=True).start()

    def print_page(self, page_num, query, operation_type):
        def worker():
            try:
                temp_pdf = tempfile.mktemp("packages.pdf")
                writer = fitz.open()
                writer.insert_pdf(self.doc, from_page=page_num, to_page=page_num)
                writer.save(temp_pdf)
                writer.close()

                success = print_pdf(temp_pdf)

                if success:
                    self.signals.message.emit("✅ Печать выполнена успешно!")
                    self.send_to_server(query, operation_type)
                    pass  # поле уже очищено синхронно в обработчике; повторный clear из потока стирал бы следующий пик
                else:
                    self.signals.message.emit("⚠️ Принтер не найден или недоступен.")
                    pass  # поле уже очищено синхронно в обработчике; повторный clear из потока стирал бы следующий пик
            except Exception as e:
                self.signals.message.emit(f"❌ Ошибка при печати: {e}")
                pass  # поле уже очищено синхронно в обработчике; повторный clear из потока стирал бы следующий пик

        threading.Thread(target=worker, daemon=True).start()

    def search_text(self):
        _dbg(f">>> search_text fired, field='{self.search_input.text()}'")  # DEBUG
        is_download = self.download_and_print_checkbox.isChecked()
        if not is_download:
            if not self.doc:
                self.signals.show_warning.emit("Ошибка", "Сначала получите PDF с сервера!")
                return

        query = self.search_input.text().strip()
        if not query:
            self.signals.show_warning.emit("Ошибка", "Введите строку для поиска!")
            return

        # Состояния чекбоксов читаем СЕЙЧАС, на UI-потоке: clear_search_input ниже
        # их сбрасывает, а Qt-виджеты нельзя трогать из фонового потока.
        is_facade = self.facade_checkbox.isChecked()
        operation_type = "PENALTY" if self.penalty_checkbox.isChecked() else "EARNING"

        # СРАЗУ освобождаем поле и возвращаем фокус (синхронно — мы на UI-потоке).
        # Сканер примет следующий QR без потери, пока этот обрабатывается в фоне:
        # валидация (сеть) + поиск по PDF + печать — всё уходит в поток dispatch().
        # Раньше validate_order висел тут, на UI, и при двух пиках подряд второй ввод
        # терялся, т.к. интерфейс замирал на сетевой запрос.
        self.clear_search_input()

        def worker():
            found_lines = []

            for page_num, text in enumerate(self.pages_text):
                filtered_text = [line.strip() for line in text if re.fullmatch(r"[0-9\- ]+", line.strip())]
                # Ozon иногда разбивает номер заказа на две соседние строки в PDF и/или
                # вставляет пробел внутри номера. Сначала пробуем склейки соседних строк,
                # затем сравниваем без пробелов где угодно — покрывает оба случая.
                joined_pairs = [
                    filtered_text[i] + filtered_text[i + 1]
                    for i in range(len(filtered_text) - 1)
                ]
                search_lines = filtered_text + joined_pairs
                query_nospace = query.replace(" ", "").lower()
                for line in search_lines:
                    clean_line = line.split(" ", 1)[0]
                    if clean_line.lower() == query.lower() or line.replace(" ", "").lower() == query_nospace:
                        found_lines.append((page_num, clean_line))

            if found_lines:
                first_page, first_line = found_lines[0]
                self.signals.message.emit(f"✅ Найдено: {query} на стр. {first_page + 1}")

                # Освобождаем поле и возвращаем фокус ДО запуска печати,
                # чтобы сканер мог сразу принимать следующий QR без потери ввода.
                # Печать дальше идёт в отдельном thread — она не должна держать UI.
                pass  # поле уже очищено синхронно в обработчике; повторный clear из потока стирал бы следующий пик

                if operation_type == "PENALTY":
                    self.signals.message.emit("⚠️ Брак — печать пропущена.")
                    self.send_to_server(query, operation_type)
                else:
                    self.signals.message.emit("🖨️ Отправка на печать...")
                    self.print_page(first_page, query, operation_type)
                return

            if len(query) > 4:
                saved_suffix = query[-4:]
                short_query = query[:-4]
                potential_labels = []
                for page_num, text in enumerate(self.pages_text):
                    for line in text:
                        if short_query.lower() in line.lower():
                            potential_labels.append((page_num, line.strip()))

                if potential_labels:
                    pages_with_short_query = set(page_num for page_num, _ in potential_labels)
                    for page_num in pages_with_short_query:
                        for fragment in " ".join(self.pages_text[page_num]).split():
                            frag_clean = fragment.strip()
                            if len(frag_clean) == 4 and frag_clean.lower() == saved_suffix.lower():
                                self.signals.message.emit(
                                    f"✅ Найдено (частичный поиск): {query} на стр. {page_num + 1}"
                                )
                                # Освобождаем поле и возвращаем фокус ДО печати — см. комментарий выше.
                                pass  # поле уже очищено синхронно в обработчике; повторный clear из потока стирал бы следующий пик
                                if operation_type == "PENALTY":
                                    self.signals.message.emit("⚠️ Брак — печать пропущена.")
                                    self.send_to_server(query, operation_type)
                                else:
                                    self.signals.message.emit("🖨️ Отправка на печать...")
                                    self.print_page(page_num, query, operation_type)
                                return

            self.signals.message.emit(f"⚠️ Строка {query} не найдена.")
            pass  # поле уже очищено синхронно в обработчике; повторный clear из потока стирал бы следующий пик

        def worker_single_package():
            if not USER_CONTEXT:
                self.signals.show_warning.emit("Ошибка", "Нет авторизованных пользователей!")
                return

            username = USER_CONTEXT[0]["username"]
            self.signals.message.emit(f"📦 Получение этикетки по номеру заказа {query} ...")

            success_download, msg, pdf_bytes = download_package_by_order(username, query)
            self.signals.message.emit(msg)
            if not success_download or not pdf_bytes:
                pass  # поле уже очищено синхронно в обработчике; повторный clear из потока стирал бы следующий пик
                return
            try:
                temp_pdf_path = os.path.join(tempfile.gettempdir(), "single_package.pdf")
                with open(temp_pdf_path, "wb") as f:
                    f.write(pdf_bytes)

                self.single_doc = fitz.open(temp_pdf_path)
                self.single_pages_text = [
                    self.single_doc[page].get_text("text").splitlines()
                    for page in range(len(self.single_doc))
                ]
                self.signals.message.emit(f"✅ PDF для заказа {query} загружен.")
            except Exception as e:
                self.signals.message.emit(f"❌ Ошибка обработки PDF для заказа {query}: {e}")
                return

            found_lines = []

            for page_num, text in enumerate(self.single_pages_text):
                filtered_text = [line.strip() for line in text if re.fullmatch(r"[0-9\- ]+", line.strip())]
                # Ozon иногда разбивает номер заказа на две соседние строки в PDF и/или
                # вставляет пробел внутри номера. Сначала пробуем склейки соседних строк,
                # затем сравниваем без пробелов где угодно — покрывает оба случая.
                joined_pairs = [
                    filtered_text[i] + filtered_text[i + 1]
                    for i in range(len(filtered_text) - 1)
                ]
                search_lines = filtered_text + joined_pairs
                query_nospace = query.replace(" ", "").lower()
                for line in search_lines:
                    clean_line = line.split(" ", 1)[0]
                    if clean_line.lower() == query.lower() or line.replace(" ", "").lower() == query_nospace:
                        found_lines.append((page_num, clean_line))

            if found_lines:
                first_page, first_line = found_lines[0]
                self.signals.message.emit(f"✅ Найдено: {query} на стр. {first_page + 1}")
                # Освобождаем поле и возвращаем фокус ДО запуска печати,
                # чтобы сканер сразу принимал следующий QR без потери ввода.
                pass  # поле уже очищено синхронно в обработчике; повторный clear из потока стирал бы следующий пик
                self.signals.message.emit("🖨️ Отправка на печать...")
                try:
                    temp_pdf = tempfile.mktemp("single_package.pdf")
                    writer = fitz.open()
                    writer.insert_pdf(self.single_doc, from_page=first_page, to_page=first_page)
                    writer.save(temp_pdf)
                    writer.close()

                    success = print_pdf(temp_pdf)

                    if success:
                        self.signals.message.emit("✅ Печать выполнена успешно!")
                    else:
                        self.signals.message.emit("⚠️ Принтер не найден или недоступен.")
                except Exception as e:
                    self.signals.message.emit(f"❌ Ошибка при печати: {e}")
                return

            if len(query) > 4:
                saved_suffix = query[-4:]
                short_query = query[:-4]
                potential_labels = []
                for page_num, text in enumerate(self.single_pages_text):
                    for line in text:
                        if short_query.lower() in line.lower():
                            potential_labels.append((page_num, line.strip()))

                if potential_labels:
                    pages_with_short_query = set(page_num for page_num, _ in potential_labels)
                    for page_num in pages_with_short_query:
                        for fragment in " ".join(self.single_pages_text[page_num]).split():
                            frag_clean = fragment.strip()
                            if len(frag_clean) == 4 and frag_clean.lower() == saved_suffix.lower():
                                self.signals.message.emit(
                                    f"✅ Найдено (частичный поиск): {query} на стр. {page_num + 1}"
                                )
                                # Освобождаем поле ДО запуска печати — см. комментарий выше.
                                pass  # поле уже очищено синхронно в обработчике; повторный clear из потока стирал бы следующий пик
                                self.signals.message.emit("🖨️ Отправка на печать...")
                                try:
                                    temp_pdf = tempfile.mktemp("single_package.pdf")
                                    writer = fitz.open()
                                    writer.insert_pdf(self.single_doc, from_page=page_num, to_page=page_num)
                                    writer.save(temp_pdf)
                                    writer.close()

                                    success = print_pdf(temp_pdf)

                                    if success:
                                        self.signals.message.emit("✅ Печать выполнена успешно!")
                                    else:
                                        self.signals.message.emit("⚠️ Принтер не найден или недоступен.")
                                except Exception as e:
                                    self.signals.message.emit(f"❌ Ошибка при печати: {e}")
                                return

            self.signals.message.emit(f"⚠️ Строка {query} не найдена.")
            pass  # поле уже очищено синхронно в обработчике; повторный clear из потока стирал бы следующий пик

        def dispatch():
            # Один скан за раз (см. _SCAN_LOCK) — иначе гонка на общем состоянии.
            with _SCAN_LOCK:
                # Валидация (сетевой запрос) — в потоке, не на UI: быстрые пики не теряются.
                success, message = validate_order(
                    order_number=query, is_employee_prepared_facade=is_facade)
                if not success:
                    self.signals.message.emit(f"❌ Валидация заказа не пройдена: {message}")
                    return
                if not is_download:
                    self.signals.message.emit(f"🔍 Поиск {query} ... (тип операции: {operation_type})")
                    worker()
                else:
                    worker_single_package()

        threading.Thread(target=dispatch, daemon=True).start()