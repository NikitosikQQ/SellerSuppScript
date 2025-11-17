from PyQt5.QtWidgets import QWidget, QVBoxLayout, QLabel, QComboBox, QPushButton, QTextEdit
from PyQt5.QtGui import QFont
from PyQt5.QtCore import QTimer

from chpu_widget import ChpuWidget
from kromka_widget import KromkaWidget
from seller_supp_api import get_workplaces, save_workplace, validate_secondary_auth
import secondary_auth_gui
from pila_widget import PilaWidget
from upakovka_mebel_widget import UpakovkaMebelWidget
from upakovka_widget import UpakovkaWidget


class WorkplacesChoiceWidget(QWidget):
    def __init__(self, stacked_widget, username):
        super().__init__()
        self.stacked_widget = stacked_widget
        self.username = username
        layout = QVBoxLayout()
        self.setLayout(layout)
        self.font = QFont("Arial", 12)

        title = QLabel("Выберите рабочее место")
        title.setFont(QFont("Arial", 18))
        layout.addWidget(title)

        self.combo = QComboBox()
        self.combo.setFont(self.font)
        self.combo.setMinimumHeight(45)
        self.combo.setStyleSheet("""
                    QComboBox { border: 2px solid #CCCCCC; border-radius: 10px; padding: 8px; }
                    QComboBox:focus { border: 2px solid #0078D7; }
                """)
        self.combo.addItem("Выберите рабочее место")
        layout.addWidget(self.combo)

        self.confirm_button = QPushButton("Выбрать")
        self.confirm_button.setFont(self.font)
        self.confirm_button.setMinimumHeight(45)
        self.confirm_button.setStyleSheet("""
                    QPushButton { background-color: #0078D7; color: white; border: none; border-radius: 10px; }
                    QPushButton:hover { background-color: #005A9E; }
                """)
        self.confirm_button.clicked.connect(self.confirm_selection)
        layout.addWidget(self.confirm_button)

        self.console = QTextEdit()
        self.console.setFont(self.font)
        self.console.setReadOnly(True)
        self.console.setStyleSheet("""
                                    QTextEdit { border: 1px solid #CCCCCC; border-radius: 8px; padding: 6px; background-color: #FAFAFA; }
                                """)
        layout.addWidget(self.console)

        self.load_workplaces()

    def append_console(self, text):
        QTimer.singleShot(0, lambda: self.console.append(text))

    def load_workplaces(self):
        success, result = get_workplaces(self.username)
        if success:
            self.combo.clear()
            self.combo.addItems(result)
            self.append_console(f"✅ Рабочие места: {', '.join(result)}")
        else:
            self.append_console(result)

    def confirm_selection(self):
        if not self.combo.count() or self.combo.currentText() == "Выберите рабочее место":
            return
        selected_wp = self.combo.currentText()
        save_workplace(self.username, selected_wp)
        self.append_console(f"🏭 Выбрано рабочее место: {selected_wp}")

        needs_secondary, required_wp, msg = validate_secondary_auth(self.username, selected_wp)
        if needs_secondary:
            self.append_console(msg)
            sec_auth_widget = secondary_auth_gui.SecondaryAuthWidget(
                self.stacked_widget, self.username, required_wp, back_widget=self
            )
            self.stacked_widget.addWidget(sec_auth_widget)
            self.stacked_widget.setCurrentWidget(sec_auth_widget)
        elif selected_wp == "Пила-мастер":
            pila_widget = PilaWidget()
            self.stacked_widget.addWidget(pila_widget)
            self.stacked_widget.setCurrentWidget(pila_widget)
        elif selected_wp == "Кромщик":
            kromka_widget = KromkaWidget()
            self.stacked_widget.addWidget(kromka_widget)
            self.stacked_widget.setCurrentWidget(kromka_widget)
        elif selected_wp == "ЧПУ":
            chpu_widget = ChpuWidget()
            self.stacked_widget.addWidget(chpu_widget)
            self.stacked_widget.setCurrentWidget(chpu_widget)
        elif selected_wp == "Упаковщик":
            upakovka_widget = UpakovkaWidget()
            self.stacked_widget.addWidget(upakovka_widget)
            self.stacked_widget.setCurrentWidget(upakovka_widget)
        elif selected_wp == "Упаковщик мебели":
            upakovka_mebel_widget = UpakovkaMebelWidget()
            self.stacked_widget.addWidget(upakovka_mebel_widget)
            self.stacked_widget.setCurrentWidget(upakovka_mebel_widget)