import sys, os, tempfile, threading, platform
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QPushButton, QLineEdit, QTextEdit,
    QLabel, QMessageBox, QCheckBox
)
from PyQt5.QtGui import QFont
from PyQt5.QtCore import pyqtSignal, QObject
from seller_supp_api import send_work_process, validate_order  # метод для POST-запроса с USER_CONTEXT

if platform.system() == "Windows":
    import win32api


class WorkerSignals(QObject):
    """Сигналы для безопасного обновления интерфейса из потоков"""
    console = pyqtSignal(str)
    clear = pyqtSignal()


class KromkaWidget(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Кромка")
        self.setGeometry(100, 100, 800, 600)
        font = QFont("Arial", 12)
        layout = QVBoxLayout()

        # === Ввод строки (orderNumber) ===
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Введите номер заказа...")
        self.search_input.setFont(font)
        self.search_input.setMinimumHeight(45)
        self.search_input.setStyleSheet("""
            QLineEdit { border: 2px solid #CCCCCC; border-radius: 10px; padding: 8px; }
            QLineEdit:focus { border: 2px solid #0078D7; }
        """)
        layout.addWidget(self.search_input)

        # === Чекбокс "Брак" ===
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
        layout.addWidget(self.penalty_checkbox)

        # === Кнопка выполнения ===
        self.print_button = QPushButton("Выполнить")
        self.print_button.setFont(font)
        self.print_button.setMinimumHeight(45)
        self.print_button.clicked.connect(self.send_request)
        self.print_button.setStyleSheet("""
            QPushButton { background-color: #0078D7; color: white; border: none; border-radius: 10px; }
            QPushButton:hover { background-color: #005A9E; }
        """)
        layout.addWidget(self.print_button)

        # === Консоль ===
        self.console_label = QLabel("Консоль:")
        self.console_label.setFont(font)
        layout.addWidget(self.console_label)

        self.results = QTextEdit()
        self.results.setFont(font)
        self.results.setReadOnly(True)
        self.results.setStyleSheet("""
            QTextEdit { border: 1px solid #CCCCCC; border-radius: 8px; padding: 6px; background-color: #FAFAFA; }
        """)
        layout.addWidget(self.results)

        self.setLayout(layout)

        # === Сигналы для работы с интерфейсом ===
        self.signals = WorkerSignals()
        self.signals.console.connect(self.append_console)
        self.signals.clear.connect(self.clear_search_input)

        # Обработка Enter
        self.search_input.returnPressed.connect(self.send_request)

    # ---------------------------
    # Методы UI
    # ---------------------------

    def append_console(self, text):
        """Добавляет сообщение в консоль"""
        self.results.append(text)
        self.results.verticalScrollBar().setValue(self.results.verticalScrollBar().maximum())

    def clear_search_input(self):
        """Очищает поле ввода и чекбокс"""
        self.search_input.clear()
        self.search_input.setFocus()
        self.penalty_checkbox.setChecked(False)

    # ---------------------------
    # Основная логика
    # ---------------------------

    def send_request(self):
        """Инициирует отправку данных"""
        text = self.search_input.text().strip()
        if not text:
            QMessageBox.warning(self, "Ошибка", "Введите номер заказа!")
            return
        success, message = validate_order(order_number=text, is_employee_prepared_facade=True)
        if not success:
            # Валидация не пройдена → выводим сообщение в консоль и прекращаем выполнение
            self.signals.clear.emit()
            self.append_console(f"❌ Валидация заказа не пройдена: {message}")
            return

        operation_type = "PENALTY" if self.penalty_checkbox.isChecked() else "EARNING"
        self.signals.console.emit(f"Отправка данных: {text}, операция: {operation_type}")
        self.worker_send_request(text, operation_type)

    def worker_send_request(self, text, operation_type):
        """Запускает фоновый поток для запроса"""
        try:
            self.signals.console.emit("⏳ Отправка данных на сервер...")

            thread = threading.Thread(
                target=self.send_work_process_request,
                args=(text, operation_type),
                daemon=True
            )
            thread.start()

            # очищаем поля в главном потоке
            self.signals.clear.emit()

        except Exception as e:
            self.signals.console.emit(f"❌ Ошибка при обработке: {e}")

    def send_work_process_request(self, order_number, operation_type):
        """Фоновая функция для запроса к серверу"""
        try:
            success, msg = send_work_process(order_number, operation_type)
            if success:
                self.signals.console.emit(f"✅ {msg}")
            else:
                self.signals.console.emit(f"❌ Ошибка при обработке: {msg}")
        except Exception as e:
            self.signals.console.emit(f"❌ Ошибка при обработке: {e}")
