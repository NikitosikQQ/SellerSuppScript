import re
import sys
import fitz
import tempfile
import os
import threading
import platform
from PyQt5.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QPushButton, QLineEdit, QTextEdit,
    QMessageBox, QLabel, QCheckBox
)
from PyQt5.QtGui import QFont
from PyQt5.QtCore import pyqtSignal, QObject
from seller_supp_api import download_packages, send_work_process, USER_CONTEXT  # импортируем методы и контекст

if platform.system() == "Windows":
    import win32api


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
        self.layout.addWidget(self.penalty_checkbox)

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

                self.doc = doc
                self.pages_text = pages_text
                # Установка пути и сообщение — через сигналы в GUI-поток
                self.signals.set_path.emit(temp_pdf_path)
                self.signals.message.emit(f"✅ PDF загружен ({len(doc)} страниц).")
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
                    self.signals.message.emit(f"❌ Ошибка при отправке: {msg}")
            except Exception as e:
                self.signals.message.emit(f"❌ Ошибка при отправке: {e}")

        threading.Thread(target=sender, daemon=True).start()

    def print_page(self, page_num, query, operation_type):
        """Печатает страницу и по завершении запускает отправку данных"""
        def worker():
            try:
                temp_pdf = tempfile.mktemp(".pdf")
                writer = fitz.open()
                writer.insert_pdf(self.doc, from_page=page_num, to_page=page_num)
                writer.save(temp_pdf)
                writer.close()

                success = False
                if platform.system() == "Windows":
                    result = win32api.ShellExecute(0, "print", temp_pdf, None, ".", 0)
                    if result > 32:
                        success = True
                else:
                    ret = os.system(f"lp '{temp_pdf}'")
                    if ret == 0:
                        success = True

                if success:
                    self.signals.message.emit("✅ Печать выполнена успешно!")
                    # После успешной печати — отправляем данные
                    self.send_to_server(query, operation_type)
                    # Очистка полей — через сигнал
                    self.signals.clear.emit()
                else:
                    self.signals.clear.emit()
                    self.signals.message.emit("⚠️ Принтер не найден или недоступен.")
            except Exception as e:
                self.signals.clear.emit()
                self.signals.message.emit(f"❌ Ошибка при печати: {e}")

        threading.Thread(target=worker, daemon=True).start()

    def search_text(self):
        """Поиск заказа и выполнение нужного действия"""
        if not self.doc:
            self.signals.show_warning.emit("Ошибка", "Сначала получите PDF с сервера!")
            return

        query = self.search_input.text().strip()
        if not query:
            self.signals.show_warning.emit("Ошибка", "Введите строку для поиска!")
            return

        operation_type = "PENALTY" if self.penalty_checkbox.isChecked() else "EARNING"
        self.signals.message.emit(f"🔍 Поиск... (тип операции: {operation_type})")

        def worker():
            found_lines = []

            # === Полный поиск ===
            for page_num, text in enumerate(self.pages_text):
                filtered_text = [line.strip() for line in text if re.fullmatch(r"[0-9\- ]+", line.strip())]
                for line in filtered_text:
                    clean_line = line.split(" ", 1)[0]
                    if clean_line.lower() == query.lower():
                        found_lines.append((page_num, clean_line))

            if found_lines:
                first_page, first_line = found_lines[0]
                self.signals.message.emit(f"✅ Найдено: {first_line} на стр. {first_page + 1}")

                if operation_type == "PENALTY":
                    self.signals.message.emit("⚠️ Брак — печать пропущена.")
                    self.send_to_server(query, operation_type)
                    self.signals.clear.emit()
                else:
                    self.signals.message.emit("🖨️ Отправка на печать...")
                    self.print_page(first_page, query, operation_type)
                return

            # === Частичный поиск ===
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
                                if operation_type == "PENALTY":
                                    self.signals.message.emit("⚠️ Брак — печать пропущена.")
                                    self.send_to_server(query, operation_type)
                                    self.signals.clear.emit()
                                else:
                                    self.signals.message.emit("🖨️ Отправка на печать...")
                                    self.print_page(page_num, query, operation_type)
                                return

            self.signals.message.emit("⚠️ Строка не найдена.")
            self.signals.clear.emit()

        threading.Thread(target=worker, daemon=True).start()
