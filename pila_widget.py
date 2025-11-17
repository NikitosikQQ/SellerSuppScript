import sys, os, tempfile, threading, qrcode, fitz, win32api
from PyQt5.QtWidgets import QWidget, QVBoxLayout, QPushButton, QLineEdit, QTextEdit, QLabel, QMessageBox, QCheckBox
from PyQt5.QtGui import QFont
from PyQt5.QtCore import QTimer, pyqtSignal, QObject
from seller_supp_api import send_work_process  # метод для POST-запроса с USER_CONTEXT


class WorkerSignals(QObject):
    message = pyqtSignal(str)
    clear = pyqtSignal()


class PilaWidget(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Пила")
        self.setGeometry(100, 100, 800, 600)
        font = QFont("Arial", 12)
        layout = QVBoxLayout()

        # Ввод строки (orderNumber)
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Введите номер заказа...")
        self.search_input.setFont(font)
        self.search_input.setMinimumHeight(45)
        self.search_input.setStyleSheet("""
            QLineEdit {
                border: 2px solid #CCCCCC;
                border-radius: 10px;
                padding: 8px;
            }
            QLineEdit:focus {
                border: 2px solid #0078D7;
            }
        """)
        layout.addWidget(self.search_input)

        # Чекбокс Брак (красный, большой)
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

        # Кнопка печати
        self.print_button = QPushButton("Выполнить")
        self.print_button.setFont(font)
        self.print_button.setMinimumHeight(45)
        self.print_button.clicked.connect(self.generate_and_print_qr)
        self.print_button.setStyleSheet("""
            QPushButton {
                background-color: #0078D7;
                color: white;
                border: none;
                border-radius: 10px;
            }
            QPushButton:hover {
                background-color: #005A9E;
            }
        """)
        layout.addWidget(self.print_button)

        # Консоль
        self.console_label = QLabel("Консоль:")
        self.console_label.setFont(font)
        layout.addWidget(self.console_label)

        self.results = QTextEdit()
        self.results.setFont(font)
        self.results.setReadOnly(True)
        self.results.setStyleSheet("""
            QTextEdit {
                border: 1px solid #CCCCCC;
                border-radius: 8px;
                padding: 6px;
                background-color: #FAFAFA;
            }
        """)
        layout.addWidget(self.results)

        self.setLayout(layout)
        self.temp_pdf = os.path.join(tempfile.gettempdir(), "qr_print.pdf")

        # Подключение сигналов
        self.signals = WorkerSignals()
        self.signals.message.connect(self.append_console)
        self.signals.clear.connect(self.clear_search_input)

        self.search_input.returnPressed.connect(self.generate_and_print_qr)

    def append_console(self, text):
        """Вывод сообщений в консоль"""
        self.results.append(text)
        self.results.verticalScrollBar().setValue(self.results.verticalScrollBar().maximum())

    def clear_search_input(self):
        """Очистка поля ввода и чекбокса"""
        self.search_input.clear()
        self.search_input.setFocus()
        self.penalty_checkbox.setChecked(False)

    def generate_and_print_qr(self):
        """Обработка нажатия кнопки"""
        text = self.search_input.text().strip()
        if not text:
            QMessageBox.warning(self, "Ошибка", "Введите строку для генерации QR-кода!")
            return

        operation_type = "PENALTY" if self.penalty_checkbox.isChecked() else "EARNING"
        self.append_console(f"Генерация QR для: {text}, операция: {operation_type}")
        threading.Thread(target=self.worker_generate_and_print, args=(text, operation_type), daemon=True).start()

    def worker_generate_and_print(self, text, operation_type):
        """Фоновая задача по генерации и печати QR"""
        try:
            if operation_type == "PENALTY":
                self.signals.message.emit("⚠️ БРАК — печать QR пропущена.")
                threading.Thread(target=self.send_work_process_request, args=(text, operation_type), daemon=True).start()
                self.signals.clear.emit()
                return

            # Генерация PDF с QR
            qr_img = qrcode.make(text)
            width = 38 * 72 / 25.4
            height = 38 * 72 / 25.4
            doc = fitz.open()
            page = doc.new_page(width=width, height=height)
            img_temp = os.path.join(tempfile.gettempdir(), "qr_temp.png")
            qr_img.convert("RGB").save(img_temp)
            margin = 5
            qr_size = min(width, height) - 2 * margin
            x = (width - qr_size) / 2
            y = (height - qr_size) / 2
            page.insert_image(fitz.Rect(x, y, x + qr_size, y + qr_size), filename=img_temp)
            doc.save(self.temp_pdf)
            doc.close()
            os.remove(img_temp)

            self.signals.message.emit(f"PDF с QR создан: {self.temp_pdf}")
            self.signals.message.emit("Отправка на печать...")

            success = False
            try:
                result = win32api.ShellExecute(0, "print", self.temp_pdf, None, ".", 0)
                if result > 32:
                    success = True
            except Exception as e:
                self.signals.message.emit(f"Ошибка при печати: {e}")

            if success:
                self.signals.message.emit("✅ Печать выполнена успешно!")
                threading.Thread(target=self.send_work_process_request, args=(text, operation_type), daemon=True).start()
            else:
                self.signals.message.emit("⚠️ Принтер не найден или недоступен.")

            self.signals.clear.emit()

        except Exception as e:
            self.signals.message.emit(f"❌ Ошибка генерации QR/PDF: {e}")

    def send_work_process_request(self, order_number, operation_type):
        """Отправка данных о работе в API"""
        try:
            success, message = send_work_process(order_number, operation_type)
            if success:
                self.signals.message.emit(f"✅ {message}")
            else:
                self.signals.message.emit(f"❌ Ошибка отправки данных о работе: {message}")
        except Exception as e:
            self.signals.message.emit(f"❌ Ошибка отправки данных: {e}")

