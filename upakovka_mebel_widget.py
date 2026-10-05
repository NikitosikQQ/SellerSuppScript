import sys
import fitz
import tempfile
import os
import threading
import platform
from PyQt5.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QPushButton, QLineEdit, QTextEdit,
    QMessageBox, QLabel, QCheckBox, QHBoxLayout
)
from PyQt5.QtGui import QFont
from PyQt5.QtCore import pyqtSignal, QObject
from seller_supp_api import download_packages, send_work_process, USER_CONTEXT, \
    validate_order, download_package_by_order, print_pdf, play_notification_sound  # методы и контекст
from label_lookup import AMBIGUOUS_CODE, build_code_index, resolve_page, resolve_single_order_page, \
    result_message

if platform.system() == "Windows":
    import win32api

# Сериализация обработки сканов: пики принимаются мгновенно (поле чистится сразу,
# UI не виснет), но обрабатываются СТРОГО ПО ОДНОМУ. Иначе два потока топчут общее
# состояние (self.single_doc, фикс. temp-файлы) → можно напечатать чужую этикетку.
_SCAN_LOCK = threading.Lock()


class WorkerSignals(QObject):
    """Сигналы, используемые для безопасного обновления GUI из потоков"""
    message = pyqtSignal(str)          # вывод в консоль
    clear = pyqtSignal()               # очистка поля и чекбокса
    set_path = pyqtSignal(str)         # установка пути к PDF
    show_warning = pyqtSignal(str, str)  # заголовок, сообщение для QMessageBox


class UpakovkaMebelWidget(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Упаковка мебели")
        self.setGeometry(100, 100, 800, 600)

        font = QFont("Arial", 12)
        self.layout = QVBoxLayout()

        # Поле пути к PDF
        self.path_input = QLineEdit()
        self.path_input.setPlaceholderText("Путь к загруженному PDF появится здесь")
        self.path_input.setFont(font)
        self.path_input.setReadOnly(True)
        self.path_input.setStyleSheet("""
            QLineEdit { border: 2px solid #CCCCCC; border-radius: 10px; padding: 8px; }
            QLineEdit:focus { border: 2px solid #0078D7; }
        """)
        self.layout.addWidget(self.path_input)

        # Кнопка загрузки PDF
        self.load_button = QPushButton("Получить актуальные этикетки")
        self.load_button.setFont(font)
        self.load_button.setMinimumHeight(45)
        self.load_button.clicked.connect(self.fetch_labels_from_server)
        self.load_button.setStyleSheet("""
            QPushButton { background-color: #0078D7; color: white; border: none; border-radius: 10px; }
            QPushButton:hover { background-color: #005A9E; }
        """)
        self.layout.addWidget(self.load_button)

        # Поле поиска
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Введите номер заказа...")
        self.search_input.setFont(font)
        self.search_input.setStyleSheet("""
            QLineEdit { border: 2px solid #CCCCCC; border-radius: 10px; padding: 8px; }
            QLineEdit:focus { border: 2px solid #0078D7; }
        """)
        self.layout.addWidget(self.search_input)

        # Чекбокс "Брак"
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
        checkbox_row.addWidget(self.download_and_print_checkbox)

        # Можно добавить растяжку, чтобы они не прилипали к левому краю
        checkbox_row.addStretch()

        # Добавляем горизонтальный layout в основной вертикальный layout
        self.layout.addLayout(checkbox_row)

        # Поиск по Enter
        self.search_input.returnPressed.connect(self.search_text)

        # Кнопка поиска и печати
        self.search_button = QPushButton("Найти и распечатать")
        self.search_button.setFont(font)
        self.search_button.setMinimumHeight(45)
        self.search_button.setStyleSheet("""
            QPushButton { background-color: #0078D7; color: white; border: none; border-radius: 10px; }
            QPushButton:hover { background-color: #005A9E; }
        """)
        self.search_button.clicked.connect(self.search_text)
        self.layout.addWidget(self.search_button)

        # Консоль
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
        # Код этикетки (OZON scanit «ii…») → страницы PDF; строится при загрузке PDF.
        self.code_index = {}

        self.single_doc = None
        self.single_pages_text = None

        # Инициализация сигналов и подключение к слотам GUI
        self.signals = WorkerSignals()
        self.signals.message.connect(self.append_console)
        self.signals.clear.connect(self.clear_search_input)
        self.signals.set_path.connect(self.path_input.setText)
        # show_warning -> вызываем QMessageBox.warning в GUI-потоке
        self.signals.show_warning.connect(lambda title, msg: QMessageBox.warning(self, title, msg))

    # === GUI-методы (слоты) ===
    def append_console(self, text):
        """Добавление строки в консоль (GUI-поток)"""
        self.results.append(text)
        self.results.verticalScrollBar().setValue(self.results.verticalScrollBar().maximum())

    def clear_search_input(self):
        """Очищает строку поиска и сбрасывает чекбокс 'Брак' (GUI-поток)"""
        self.search_input.clear()
        self.search_input.setFocus()
        self.penalty_checkbox.setChecked(False)
        self.download_and_print_checkbox.setChecked(False)

    # === Логика ===
    def fetch_labels_from_server(self):
        """Загружает PDF с сервера"""
        if not USER_CONTEXT:
            # Сигнал вызовет QMessageBox.warning в GUI-потоке
            self.signals.show_warning.emit("Ошибка", "Нет авторизованных пользователей!")
            return

        username = USER_CONTEXT[0]["username"]
        self.signals.message.emit("📦 Получение актуальных этикеток...")

        def worker():
            success, msg, pdf_bytes = download_packages(username, True)
            self.signals.message.emit(msg)
            if not success or not pdf_bytes:
                return
            try:
                temp_pdf_path = os.path.join(tempfile.gettempdir(), "packages_mebel.pdf")
                with open(temp_pdf_path, "wb") as f:
                    f.write(pdf_bytes)

                doc = fitz.open(temp_pdf_path)
                pages_text = [
                    doc[page].get_text("text").splitlines()
                    for page in range(len(doc))
                ]
                code_index = build_code_index(pages_text)

                # Подмена документа — под тем же замком, что и обработка скана: иначе скан
                # мог найти страницу в одном PDF, а напечатать её из другого (чужая этикетка).
                with _SCAN_LOCK:
                    self.doc, self.pages_text, self.code_index = doc, pages_text, code_index
                # Установка пути и сообщение — через сигналы в GUI-поток
                self.signals.set_path.emit(temp_pdf_path)
                self.signals.message.emit(
                    f"✅ PDF загружен ({len(doc)} страниц, кодов этикеток OZON: {len(code_index)}).")
            except Exception as e:
                self.signals.message.emit(f"❌ Ошибка обработки PDF: {e}")

        threading.Thread(target=worker, daemon=True).start()

    def send_to_server(self, query, operation_type):
        """Асинхронная отправка данных на сервер"""
        def sender():
            try:
                success, msg = send_work_process(query, operation_type)
                if success:
                    self.signals.message.emit(f"✅ {msg}")
                else:
                    self.signals.message.emit(f"❌ Ошибка при обработке: {msg}")
            except Exception as e:
                self.signals.message.emit(f"❌ Ошибка при обработке: {e}")

        threading.Thread(target=sender, daemon=True).start()

    def print_page(self, page_num, query, operation_type, doc=None):
        """Печатает страницу и по завершении запускает отправку данных.

        doc — документ, в котором искали страницу: печать идёт в своём потоке, и за это время
        «Получить актуальные этикетки» может подменить self.doc.
        """
        source = doc if doc is not None else self.doc

        def worker():
            try:
                temp_pdf = tempfile.mktemp(".pdf")
                writer = fitz.open()
                writer.insert_pdf(source, from_page=page_num, to_page=page_num)
                writer.save(temp_pdf)
                writer.close()

                success = print_pdf(temp_pdf)

                if success:
                    self.signals.message.emit("✅ Печать выполнена успешно!")
                    # После успешной печати — отправляем данные
                    self.send_to_server(query, operation_type)
                    # Очистка полей — через сигнал
                    pass  # поле уже очищено синхронно в обработчике; повторный clear из потока стирал бы следующий пик
                else:
                    pass  # поле уже очищено синхронно в обработчике; повторный clear из потока стирал бы следующий пик
                    self.signals.message.emit("⚠️ Принтер не найден или недоступен.")
            except Exception as e:
                pass  # поле уже очищено синхронно в обработчике; повторный clear из потока стирал бы следующий пик
                self.signals.message.emit(f"❌ Ошибка при печати: {e}")

        threading.Thread(target=worker, daemon=True).start()

    def search_text(self):
        """Поиск заказа и выполнение нужного действия"""
        is_download = self.download_and_print_checkbox.isChecked()
        if not is_download:
            if not self.doc:
                self.signals.show_warning.emit("Ошибка", "Сначала получите PDF с сервера!")
                return

        query = self.search_input.text().strip()
        if not query:
            self.signals.show_warning.emit("Ошибка", "Введите строку для поиска!")
            return

        # Читаем чекбокс СЕЙЧАС (на UI): clear_search_input ниже его сбрасывает.
        operation_type = "PENALTY" if self.penalty_checkbox.isChecked() else "EARNING"

        # СРАЗУ освобождаем поле и фокус (синхронно — мы на UI-потоке). Валидация
        # (сеть) + поиск + печать уходят в поток dispatch(), чтобы быстрые пики
        # подряд не терялись (раньше validate_order висел на UI и замораживал ввод).
        self.clear_search_input()

        def worker(label_code):
            # Снимок под _SCAN_LOCK (dispatch держит замок): поиск и печать — по одному PDF.
            doc, pages_text, code_index = self.doc, self.pages_text, self.code_index
            page, how = resolve_page(pages_text, code_index, query, label_code)
            self.signals.message.emit(result_message(how, query, label_code, page))
            if page is None:
                if how == AMBIGUOUS_CODE:
                    play_notification_sound()
                return

            if operation_type == "PENALTY":
                self.signals.message.emit("⚠️ Брак — печать пропущена.")
                self.send_to_server(query, operation_type)
            else:
                self.signals.message.emit("🖨️ Отправка на печать...")
                self.print_page(page, query, operation_type, doc)

        def worker_single_package(label_code):
            if not USER_CONTEXT:
                self.signals.show_warning.emit("Ошибка", "Нет авторизованных пользователей!")
                return

            username = USER_CONTEXT[0]["username"]
            self.signals.message.emit(f"📦 Получение этикетки по номеру заказа {query} ...")

            success_download, msg, pdf_bytes = download_package_by_order(username, query)
            self.signals.message.emit(msg)
            if not success_download or not pdf_bytes:
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

            page, how = resolve_single_order_page(self.single_pages_text, query, label_code)
            self.signals.message.emit(result_message(how, query, label_code, page))
            if page is None:
                if how == AMBIGUOUS_CODE:
                    play_notification_sound()
                return

            # Печать без отправки выработки — как и прежде в этом режиме.
            self.signals.message.emit("🖨️ Отправка на печать...")
            try:
                temp_pdf = tempfile.mktemp("single_package.pdf")
                writer = fitz.open()
                writer.insert_pdf(self.single_doc, from_page=page, to_page=page)
                writer.save(temp_pdf)
                writer.close()

                success = print_pdf(temp_pdf)

                if success:
                    self.signals.message.emit("✅ Печать выполнена успешно!")
                else:
                    self.signals.message.emit("⚠️ Принтер не найден или недоступен.")
            except Exception as e:
                self.signals.message.emit(f"❌ Ошибка при печати: {e}")

        def dispatch():
            # Один скан за раз (см. _SCAN_LOCK) — иначе гонка на общем состоянии.
            with _SCAN_LOCK:
                # Валидация (сетевой запрос) — в потоке, не на UI: быстрые пики не теряются.
                # label_code — код с этикетки (OZON scanit «ii…»): по нему ищется страница.
                success, message, label_code = validate_order(
                    order_number=query, is_employee_prepared_facade=True)
                if not success:
                    self.signals.message.emit(f"❌ Валидация заказа не пройдена: {message}")
                    return
                if not is_download:
                    self.signals.message.emit(f"🔍 Поиск {query} ... (тип операции: {operation_type})")
                    worker(label_code)
                else:
                    worker_single_package(label_code)

        threading.Thread(target=dispatch, daemon=True).start()
