"""Выбор рабочего ЭТАПА (форк workplaces_choice.py под seller-platform).

Вместо enum-рабочих мест (Пила/Кромка/ЧПУ/...) — список этапов конвейера из
`GET /api/v1/stages/my-workstations`. В комбобоксе показывается displayName, сохраняется
code (для work/process). Вторичная авторизация пар (furniture) и роутинг по именам мест
убраны: любой выбранный этап открывает один универсальный StageWidget.
"""
from PyQt5.QtWidgets import QWidget, QVBoxLayout, QLabel, QComboBox, QPushButton, QTextEdit
from PyQt5.QtGui import QFont
from PyQt5.QtCore import QTimer

from api import get_workstations, save_stage
from stage_widget import StageWidget


class WorkstationsChoiceWidget(QWidget):
    def __init__(self, stacked_widget, username):
        super().__init__()
        self.stacked_widget = stacked_widget
        self.username = username
        self.stations = []  # [{'code','displayName'}, ...]
        layout = QVBoxLayout()
        self.setLayout(layout)
        self.font = QFont("Arial", 12)

        title = QLabel("Выберите этап")
        title.setFont(QFont("Arial", 18))
        layout.addWidget(title)

        self.combo = QComboBox()
        self.combo.setFont(self.font)
        self.combo.setMinimumHeight(45)
        self.combo.setStyleSheet("""
                    QComboBox { border: 2px solid #CCCCCC; border-radius: 10px; padding: 8px; }
                    QComboBox:focus { border: 2px solid #0078D7; }
                """)
        self.combo.addItem("Выберите этап")
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

        self.load_workstations()

    def append_console(self, text):
        QTimer.singleShot(0, lambda: self.console.append(text))

    def load_workstations(self):
        success, result = get_workstations(self.username)
        if success:
            self.stations = result
            self.combo.clear()
            for st in result:
                # Текст — displayName; данные пункта — code (для work/process).
                self.combo.addItem(st.get("displayName") or st.get("code"), st.get("code"))
            names = ", ".join(st.get("displayName") or st.get("code") for st in result)
            self.append_console(f"✅ Доступные этапы: {names}")
        else:
            self.append_console(result)

    def confirm_selection(self):
        if not self.combo.count() or self.combo.currentIndex() < 0:
            return
        stage_code = self.combo.currentData()
        stage_name = self.combo.currentText()
        if not stage_code or stage_name == "Выберите этап":
            return
        save_stage(self.username, stage_code, stage_name)
        self.append_console(f"🏭 Выбран этап: {stage_name} ({stage_code})")

        stage_widget = StageWidget(stage_code, stage_name)
        self.stacked_widget.addWidget(stage_widget)
        self.stacked_widget.setCurrentWidget(stage_widget)
