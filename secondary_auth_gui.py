from PyQt5.QtWidgets import QWidget, QVBoxLayout, QLabel, QLineEdit, QPushButton, QTextEdit
from PyQt5.QtGui import QFont
from PyQt5.QtCore import QTimer, Qt
from seller_supp_api import authorize, get_workplaces, save_workplace, is_user_in_context, remove_user_from_context
from pila_widget import PilaWidget

class SecondaryAuthWidget(QWidget):
    """Вторичная авторизация для специальных рабочих мест"""

    def __init__(self, stacked_widget, username, required_workplace, back_widget=None):
        super().__init__()
        self.stacked_widget = stacked_widget
        self.username = username
        self.required_workplace = required_workplace
        self.back_widget = back_widget
        self.font = QFont("Arial", 12)

        layout = QVBoxLayout()
        layout.setAlignment(Qt.AlignTop)
        self.setLayout(layout)

        # Заголовок
        title = QLabel("Авторизация напарника")
        title.setFont(QFont("Arial", 18))
        layout.addWidget(title)

        # Поля логина и пароля
        self.username_input = QLineEdit()
        self.username_input.setPlaceholderText("Имя пользователя")
        self.username_input.setFont(self.font)
        self.username_input.setMinimumHeight(45)
        self.username_input.setStyleSheet("""
            QLineEdit { border: 2px solid #CCCCCC; border-radius: 10px; padding: 8px; }
            QLineEdit:focus { border: 2px solid #0078D7; }
        """)
        layout.addWidget(self.username_input)

        self.password_input = QLineEdit()
        self.password_input.setPlaceholderText("Пароль")
        self.password_input.setEchoMode(QLineEdit.Password)
        self.password_input.setFont(self.font)
        self.password_input.setMinimumHeight(45)
        self.password_input.setStyleSheet("""
            QLineEdit { border: 2px solid #CCCCCC; border-radius: 10px; padding: 8px; }
            QLineEdit:focus { border: 2px solid #0078D7; }
        """)
        layout.addWidget(self.password_input)

        # Кнопки
        self.login_button = QPushButton("Войти")
        self.login_button.setFont(self.font)
        self.login_button.setMinimumHeight(45)
        self.login_button.setStyleSheet("""
            QPushButton { background-color: #0078D7; color: white; border: none; border-radius: 10px; }
            QPushButton:hover { background-color: #005A9E; }
        """)
        self.login_button.clicked.connect(self.handle_login)
        layout.addWidget(self.login_button)

        self.back_button = QPushButton("Назад")
        self.back_button.setFont(self.font)
        self.back_button.setMinimumHeight(45)
        self.back_button.setStyleSheet("""
            QPushButton { background-color: #CCCCCC; color: black; border: none; border-radius: 10px; }
            QPushButton:hover { background-color: #AAAAAA; }
        """)
        self.back_button.clicked.connect(self.go_back)
        layout.addWidget(self.back_button)

        # Консоль
        self.console = QTextEdit()
        self.console.setFont(self.font)
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

        if is_user_in_context(username):
            self.append_console(f"⚠️ Пользователь '{username}' уже авторизован. Используйте другого.")
            return

        self.login_button.setEnabled(False)
        success, token_or_error = authorize(username, password)

        if success:
            self.append_console(f"✅ Пользователь '{username}' авторизован")
            wp_success, workplaces = get_workplaces(username)

            if wp_success:
                if self.required_workplace not in workplaces:
                    self.append_console(
                        f"❌ Работник '{username}' не имеет доступа к рабочему месту {self.required_workplace}")
                    remove_user_from_context(username)
                else:
                    save_workplace(username, self.required_workplace)
                    self.append_console(f"🏭 Выбранное рабочее место: {self.required_workplace}")

                    # Запуск PilaWidget, если рабочее место Пила-1 или Пила-2
                    if self.required_workplace in ["Пила-1", "Пила-2"]:
                        pila_widget = PilaWidget()
                        self.stacked_widget.addWidget(pila_widget)
                        self.stacked_widget.setCurrentWidget(pila_widget)
                    elif self.back_widget:
                        self.back_widget.load_workplaces()
                        self.stacked_widget.setCurrentWidget(self.back_widget)
            else:
                self.append_console(workplaces)
                remove_user_from_context(username)
        else:
            if token_or_error == "401":
                self.append_console("❌ Неверное имя пользователя или пароль")
            else:
                self.append_console(f"❌ Ошибка авторизации: {token_or_error}")

        self.login_button.setEnabled(True)

    def go_back(self):
        if self.back_widget:
            self.stacked_widget.setCurrentWidget(self.back_widget)
