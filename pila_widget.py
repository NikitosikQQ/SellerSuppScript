import sys, os, tempfile, threading, time, qrcode, fitz, win32api
from PyQt5.QtWidgets import QWidget, QVBoxLayout, QPushButton, QLineEdit, QTextEdit, QLabel, QMessageBox, QCheckBox
from PyQt5.QtGui import QFont
from PyQt5.QtCore import QTimer, pyqtSignal, QObject
import diag
import settings
from print_queue import PrintPipeline
from qr_label import build_label_pdf, merge_pdfs
from seller_supp_api import send_work_process, validate_order, print_pdf  # метод для POST-запроса с USER_CONTEXT

# ПРЕЖНИЙ режим ([pila] print_queue = off): сканы обрабатываются СТРОГО ПО ОДНОМУ, каждый ждёт
# проверки и печати предыдущего. Иначе два потока топчут общий self.temp_pdf → можно напечатать
# QR чужого заказа. В режиме очереди не используется (там у каждой наклейки свой файл).
_SCAN_LOCK = threading.Lock()


def _remove_label_copy(pdf_path):
    """print_pdf пишет рядом копию, подогнанную под этикетку (<имя>_label.pdf) — убрать её."""
    copy = (pdf_path[:-4] if pdf_path.lower().endswith(".pdf") else pdf_path) + "_label.pdf"
    try:
        os.remove(copy)
    except OSError:
        pass


class WorkerSignals(QObject):
    message = pyqtSignal(str)
    clear = pyqtSignal()
    state = pyqtSignal(str)


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

        # Строка состояния очереди печати: видно, что ещё печатается и можно ли закрывать окно.
        self.queue_label = QLabel("")
        self.queue_label.setFont(QFont("Arial", 11))
        layout.addWidget(self.queue_label)

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
        self.signals.state.connect(self.queue_label.setText)

        self.search_input.returnPressed.connect(self.generate_and_print_qr)

        # Очередь печати ([pila] print_queue): скан не ждёт проверки и печати. Выключена —
        # прежний режим 26.10.5 (generate_and_print_qr → _legacy_*).
        self.pipeline = None
        if settings.get("pila", "print_queue"):
            self.pipeline = PrintPipeline(
                validate_fn=self._queue_validate,
                build_pdf_fn=lambda order, path: build_label_pdf(order, path,
                                                                 with_text=settings.get("pila", "label_text")),
                merge_fn=merge_pdfs,
                print_fn=self._queue_print,
                mark_fn=self._queue_mark,
                on_message=self.signals.message.emit,
                on_state=self._queue_state,
                batch_max=settings.get("pila", "batch_max"),
                dedupe=settings.get("pila", "dedupe"),
                cleanup_fn=_remove_label_copy,
                log=diag.log,
            )
            self._queue_state(self.pipeline.counts())

        if settings.SETTINGS_PATH:
            self.results.append(f"⚙️ Настройки из {os.path.basename(settings.SETTINGS_PATH)}: {settings.summary()}")
        for error in settings.SETTINGS_ERRORS:
            self.results.append(f"⚠️ Настройки: {error}")
        if diag.ENABLED:
            diag.log("PILA_OPEN", queue=int(self.pipeline is not None), settings=settings.summary())
            self.results.append(f"🔎 Отладочная сборка. Лог: {diag.LOG_PATH}")

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

        # Чекбокс читаем СЕЙЧАС (на UI): clear_search_input ниже его сбрасывает.
        operation_type = "PENALTY" if self.penalty_checkbox.isChecked() else "EARNING"
        sid = diag.new_scan(order=text, operation=operation_type)
        t_queued = time.perf_counter()

        # СРАЗУ освобождаем поле и фокус (синхронно — мы на UI-потоке), чтобы сканер
        # принял следующий QR без потери. Валидация (сеть) + генерация + печать —
        # в фоне. Раньше validate_order висел тут, на UI, и при двух пиках подряд
        # второй ввод терялся.
        self.clear_search_input()

        if self.pipeline is not None:
            self.pipeline.submit(text, operation_type, sid=sid)
            return

        self.signals.message.emit(f"Генерация QR для: {text}, операция: {operation_type}")

        def _run():
            # Один скан за раз (см. _SCAN_LOCK) — иначе гонка на общем self.temp_pdf.
            with _SCAN_LOCK:
                diag.log("SCAN_START", sid=sid, order=text, queue_ms=diag.ms_since(t_queued))
                self.worker_generate_and_print(text, operation_type, sid)

        threading.Thread(target=_run, daemon=True).start()

    def worker_generate_and_print(self, text, operation_type, sid=None):
        """Фоновая задача по генерации и печати QR"""
        t_scan = time.perf_counter()
        result = "error"
        try:
            # Валидация (сетевой запрос) — в потоке, не на UI: иначе быстрые пики теряются.
            t_step = time.perf_counter()
            with diag.op("validate", sid=sid):
                success, message, _label_code = validate_order(order_number=text, is_employee_prepared_facade=True)
            diag.log("SCAN_VALIDATE", sid=sid, order=text, ok=int(success), ms=diag.ms_since(t_step), msg=message)
            if not success:
                result = "validation_failed"
                self.signals.message.emit(f"❌ Валидация заказа не пройдена: {message}")
                return

            if operation_type == "PENALTY":
                result = "penalty"
                self.signals.message.emit("⚠️ БРАК — печать QR пропущена.")
                threading.Thread(target=self.send_work_process_request, args=(text, operation_type, sid), daemon=True).start()
                pass  # поле уже очищено синхронно в обработчике; повторный clear из потока стирал бы следующий пик
                return

            # Генерация PDF с QR (label_text = off — прежняя наклейка 26.10.5: только QR 38×38 мм)
            t_step = time.perf_counter()
            build_label_pdf(text, self.temp_pdf, with_text=settings.get("pila", "label_text"))
            diag.log("SCAN_QR_PDF", sid=sid, ms=diag.ms_since(t_step))

            self.signals.message.emit(f"PDF с QR создан: {self.temp_pdf}")
            self.signals.message.emit("Отправка на печать...")

            t_step = time.perf_counter()
            with diag.op("print", sid=sid, order=text):
                success = print_pdf(self.temp_pdf)
            diag.log("SCAN_PRINT", sid=sid, ok=int(success), ms=diag.ms_since(t_step))

            if success:
                result = "printed"
                self.signals.message.emit("✅ Печать выполнена успешно!")
                threading.Thread(target=self.send_work_process_request, args=(text, operation_type, sid), daemon=True).start()
            else:
                result = "print_failed"
                self.signals.message.emit("⚠️ Принтер не найден или недоступен.")

            pass  # поле уже очищено синхронно в обработчике; повторный clear из потока стирал бы следующий пик

        except Exception as e:
            diag.log("SCAN_ERROR", sid=sid, exc=type(e).__name__, err=e)
            self.signals.message.emit(f"❌ Ошибка генерации QR/PDF: {e}")
        finally:
            diag.log("SCAN_DONE", sid=sid, order=text, result=result, total_ms=diag.ms_since(t_scan))

    def send_work_process_request(self, order_number, operation_type, sid=None):
        """Отправка данных о работе в API"""
        t_mark = time.perf_counter()
        try:
            with diag.op("mark", sid=sid):
                success, message = send_work_process(order_number, operation_type)
            diag.log("SCAN_MARK", sid=sid, order=order_number, ok=int(success), ms=diag.ms_since(t_mark), msg=message)
            if success:
                self.signals.message.emit(f"✅ {message}")
            else:
                self.signals.message.emit(f"❌ {message}")
        except Exception as e:
            diag.log("SCAN_MARK", sid=sid, order=order_number, ok=0, ms=diag.ms_since(t_mark), exc=type(e).__name__, err=e)
            self.signals.message.emit(f"❌ Ошибка отправки данных: {e}")

    # ------------------------------------------------------------------ очередь печати
    # Вызываются из потоков очереди (print_queue.PrintPipeline) — виджеты здесь не трогаем.

    def _queue_validate(self, order):
        if settings.get("pila", "fast_validate"):
            ok, message, _label_code = validate_order(order, True, timeout=(5, 8), network_retries=1)
        else:
            ok, message, _label_code = validate_order(order, True)
        return ok, message

    def _queue_print(self, pdf_path):
        with diag.op("print"):
            return print_pdf(pdf_path)

    def _queue_mark(self, order, operation_type):
        retries = 1 if settings.get("pila", "mark_retry") else 0
        return send_work_process(order, operation_type, network_retries=retries)

    def _queue_state(self, counts):
        queued, printing, marks = counts["queued"], counts["printing"], counts["marks"]
        if not (queued or printing or marks):
            text = "Очередь печати пуста"
        else:
            parts = []
            if queued:
                parts.append(f"в очереди: {queued}")
            if printing:
                parts.append(f"печатается: {printing}")
            if marks:
                parts.append(f"ждут отметки: {marks}")
            text = "⏳ " + " · ".join(parts)
        self.signals.state.emit(text)

