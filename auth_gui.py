import sys
import time
import urllib3
from PyQt5.QtWidgets import QWidget, QVBoxLayout, QLineEdit, QPushButton, QLabel, QTextEdit, QStackedWidget, \
    QApplication, QMessageBox
from PyQt5.QtGui import QFont
from PyQt5.QtCore import QTimer
import diag
import settings
from seller_supp_api import HOST, authorize
from workplaces_choice import WorkplacesChoiceWidget
from version import APP_TITLE, APP_VERSION


class AuthWidget(QWidget):
    def __init__(self, stacked_widget):
        super().__init__()
        self.stacked_widget = stacked_widget
        layout = QVBoxLayout()
        self.setLayout(layout)
        font = QFont("Arial", 12)

        self.title_label = QLabel("Авторизация в системе")
        self.title_label.setFont(QFont("Arial", 18))
        layout.addWidget(self.title_label)

        self.username_input = QLineEdit()
        self.username_input.setPlaceholderText("Имя пользователя")
        self.username_input.setFont(font)
        self.username_input.setMinimumHeight(45)
        self.username_input.setStyleSheet("""
                    QLineEdit { border: 2px solid #CCCCCC; border-radius: 10px; padding: 8px; }
                    QLineEdit:focus { border: 2px solid #0078D7; }
                """)
        layout.addWidget(self.username_input)

        self.password_input = QLineEdit()
        self.password_input.setPlaceholderText("Пароль")
        self.password_input.setEchoMode(QLineEdit.Password)
        self.password_input.setFont(font)
        self.password_input.setMinimumHeight(45)
        self.password_input.setStyleSheet("""
                    QLineEdit { border: 2px solid #CCCCCC; border-radius: 10px; padding: 8px; }
                    QLineEdit:focus { border: 2px solid #0078D7; }
                """)
        layout.addWidget(self.password_input)

        self.login_button = QPushButton("Войти")
        self.login_button.setFont(font)
        self.login_button.setMinimumHeight(45)
        self.login_button.setStyleSheet("""
                    QPushButton { background-color: #0078D7; color: white; border: none; border-radius: 10px; }
                    QPushButton:hover { background-color: #005A9E; }
                """)
        self.login_button.clicked.connect(self.handle_login)
        layout.addWidget(self.login_button)

        self.console = QTextEdit()
        self.console.setFont(font)
        self.console.setReadOnly(True)
        self.console.setStyleSheet("""
                    QTextEdit { border: 1px solid #CCCCCC; border-radius: 8px; padding: 6px; background-color: #FAFAFA; }
                """)
        layout.addWidget(self.console)

    def append_console(self, text):
        QTimer.singleShot(0, lambda: self.console.append(text))

    def handle_login(self):
        username = self.username_input.text().strip()
        password = self.password_input.text().strip()
        if not username or not password:
            self.append_console("⚠️ Введите имя пользователя и пароль")
            return
        self.login_button.setEnabled(False)
        success, token_or_error = authorize(username, password)
        if success:
            self.append_console(f"✅ Пользователь '{username}' авторизован")
            wp_widget = WorkplacesChoiceWidget(self.stacked_widget, username)
            self.stacked_widget.addWidget(wp_widget)
            self.stacked_widget.setCurrentWidget(wp_widget)
        else:
            if token_or_error == "401":
                self.append_console("❌ Неверное имя пользователя или пароль")
            else:
                self.append_console(f"❌ Ошибка авторизации: {token_or_error}")
        self.login_button.setEnabled(True)


class AuthGUI(QWidget):
    def __init__(self):
        super().__init__()
        # Версия в заголовке: цех может назвать сборку, не открывая свойства файла.
        self.setWindowTitle(APP_TITLE + (" [DIAG]" if diag.ENABLED else ""))
        self.setGeometry(200, 200, 550, 500)
        layout = QVBoxLayout()
        self.setLayout(layout)
        self.stack = QStackedWidget()
        layout.addWidget(self.stack)
        self.auth_page = AuthWidget(self.stack)
        self.stack.addWidget(self.auth_page)
        # Настройки из ss_settings.ini и ошибки в нём — видно на любом посту, не только на пиле.
        if settings.SETTINGS_PATH:
            self.auth_page.append_console(f"⚙️ Настройки из {settings.SETTINGS_FILE}: {settings.summary()}")
        for error in settings.SETTINGS_ERRORS:
            self.auth_page.append_console(f"⚠️ Настройки: {error}")
        if diag.ENABLED:
            self.auth_page.append_console(f"🔎 Отладочная сборка. Лог: {diag.LOG_PATH}")

    # Сколько ждать очередь печати пилы при закрытии окна.
    CLOSE_DRAIN_MAX_S = 45

    def closeEvent(self, event):
        # Отладочная сборка пишет, кто закрывает окно, и не даёт чужому процессу закрыть
        # его сразу после печати. В обычной сборке should_block_close всегда False.
        if diag.should_block_close(event.spontaneous()):
            event.ignore()
            page = self.stack.currentWidget()
            if hasattr(page, "append_console"):
                page.append_console("🔎 Окно закрыли сразу после печати — оставлено открытым (отладка). "
                                    "Если закрываете вы — нажмите крестик ещё раз.")
            return
        if self._delay_close_for_print_queue(event):
            return
        super().closeEvent(event)

    def _delay_close_for_print_queue(self, event):
        """Очередь печати пилы ещё работает: не закрываться сразу, иначе напечатанное останется
        без отметки (без начисления), а ждущее — без следа. Новые сканы больше не принимаются,
        окно закроется само, когда очередь опустеет (не дольше CLOSE_DRAIN_MAX_S)."""
        if getattr(self, "_close_forced", False):
            return False
        page = self.stack.currentWidget()
        pipeline = getattr(page, "pipeline", None)
        if pipeline is None or not pipeline.is_busy():
            return False
        event.ignore()
        if getattr(self, "_drain_timer", None) is not None:
            return True  # уже ждём
        pipeline.stop_accepting()
        page.append_console("⏳ Завершаю печать и отметки — окно закроется само. Новые сканы не принимаются.")
        diag.log("CLOSE_DRAIN", pending=len(pipeline.pending()))
        started = time.monotonic()
        failures_before = len(pipeline.mark_failures())
        self._drain_timer = QTimer(self)

        def check():
            if pipeline.is_busy() and time.monotonic() - started < self.CLOSE_DRAIN_MAX_S:
                return
            self._drain_timer.stop()
            lines = [f"[{seq}] {order} — {where}" for seq, order, where in pipeline.pending()]
            lines += [f"[{seq}] {order} — отметка не прошла: {message}"
                      for seq, order, _op, message in pipeline.mark_failures()[failures_before:]]
            if lines:
                diag.log("CLOSE_UNFINISHED", jobs="; ".join(lines))
                QMessageBox.warning(self, "Не всё завершено",
                                    "Не напечатано или не отмечено:\n" + "\n".join(lines)
                                    + "\n\nПосле запуска отсканируйте эти заказы заново.")
            self._close_forced = True
            self.close()

        self._drain_timer.timeout.connect(check)
        self._drain_timer.start(500)
        return True


if __name__ == "__main__":
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    diag.init(APP_VERSION, HOST)
    settings.log_effective()
    app = QApplication(sys.argv)
    diag.install_ui_watchdog()
    app.aboutToQuit.connect(diag.log_exit)
    window = AuthGUI()
    window.show()
    diag.install_window_watch(app, window)
    sys.exit(app.exec_())
