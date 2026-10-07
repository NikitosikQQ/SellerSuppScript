"""Настройки поста из файла ss_settings.ini рядом с exe — откат нововведений без замены exe.

Файла нет — всё по умолчанию (новое поведение). Пример с пояснениями — ss_settings.example.ini.

Откат целиком на поведение 26.10.5 — одна строка:
    [general]
    mode = legacy
Откат отдельных нововведений — их ключи в [pila] / [print] (on/off, числа).

Читается один раз при старте программы. Ошибка в файле не роняет программу: неверное значение
заменяется умолчанием, а причина пишется в отладочный лог (если это отладочная сборка).
"""
import configparser
import os
import sys

import diag

SETTINGS_FILE = "ss_settings.ini"

# Новое поведение (по умолчанию) и поведение 26.10.5 (mode = legacy).
_DEFAULTS = {
    # Очередь печати пилы: скан не ждёт печати. off — скан ждёт проверки и печати, как в 26.10.5.
    ("pila", "print_queue"): (True, False),
    # Сколько наклеек склеивать в одну печать PDFtoPrinter (1 — без склейки).
    ("pila", "batch_max"): (5, 1),
    # Номер заказа текстом под QR. off — только QR, как в 26.10.5.
    ("pila", "label_text"): (True, False),
    # Проверка заказа на пиле: таймаут (5, 8) с и один повтор при сетевой ошибке. off — 15 с без повтора.
    # Если у WB проблемы и сервер долго опрашивает WB (3–13 с) — выключить: 8 с ему может не хватить.
    ("pila", "fast_validate"): (True, False),
    # Один повтор отметки работы, если соединение с сервером не установилось (запрос точно не ушёл).
    # После отправки не повторяем: два параллельных запроса могли бы начислить дважды. off — без повтора.
    ("pila", "mark_retry"): (True, False),
    # Не ставить в очередь дубль: тот же заказ и та же операция ещё ждут печати.
    ("pila", "dedupe"): (True, False),
    # Не давать окнам PDFtoPrinter / PDF-XChange забирать фокус на время печати (все посты).
    ("print", "focus_lock"): (True, False),
    # Таймаут PDFtoPrinter, секунд (все посты). В 26.10.5 — 60.
    ("print", "timeout_s"): (30, 60),
    # По таймауту завершать всё дерево процессов печати, а не только PDFtoPrinter (все посты).
    ("print", "kill_tree"): (True, False),
}


def _settings_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _parse_bool(raw):
    value = raw.strip().lower()
    if value in ("1", "on", "yes", "true", "да", "вкл"):
        return True
    if value in ("0", "off", "no", "false", "нет", "выкл"):
        return False
    raise ValueError(f"ожидается on/off, получено {raw!r}")


def _read_text(path):
    """Текст файла в любой кодировке, в которой его мог сохранить Блокнот: UTF-8 (с BOM и без),
    «Юникод» (UTF-16 с BOM) или «ANSI» (cp1251 — декодирует любые байты)."""
    with open(path, "rb") as f:
        data = f.read()
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1251")


def _known_sections():
    return {"general"} | {section for section, _ in _DEFAULTS}


def load(path=None):
    """Возвращает (настройки, путь к файлу или None, список ошибок разбора).

    Никогда не бросает исключений: файл настроек — средство отката, и ошибка в нём не должна
    мешать программе запуститься. Всё непонятное — в список ошибок (он виден в окне пилы)."""
    path = path or os.path.join(_settings_dir(), SETTINGS_FILE)
    # interpolation=None: «%» в значении — обычный символ, а не подстановка ConfigParser.
    # strict=False: повтор ключа или раздела — не ошибка, берётся последнее значение.
    def new_parser():
        return configparser.ConfigParser(inline_comment_prefixes=(";", "#"), interpolation=None, strict=False)

    parser = new_parser()
    errors = []
    found = None
    if os.path.exists(path):
        try:
            text = _read_text(path)
        except Exception as e:
            text = None
            errors.append(f"файл не прочитан ({type(e).__name__}: {e}) — работаю по умолчанию")
        if text is not None:
            found = path
            try:
                parser.read_string(text)
            except configparser.Error as e:
                # ConfigParser дочитывает файл до конца и сохраняет правильные строки — их и берём:
                # одна кривая строка не должна отменять «mode = legacy» ниже.
                errors.append(f"в файле есть ошибки, правильные строки применены: {e}")
            except Exception as e:
                parser = new_parser()
                errors.append(f"файл не разобран ({type(e).__name__}: {e}) — работаю по умолчанию")

    known = _known_sections()
    for section in parser.sections():
        if section not in known:
            errors.append(f"неизвестный раздел [{section}] — пропущен (разделы: {', '.join(sorted(known))})")
            continue
        allowed = {key for s, key in _DEFAULTS if s == section} | ({"mode"} if section == "general" else set())
        for key in parser.options(section):
            if key not in allowed:
                errors.append(f"[{section}] неизвестный ключ {key!r} — пропущен")

    mode = "new"
    try:
        mode = parser.get("general", "mode", fallback="new").strip().lower()
    except Exception as e:
        errors.append(f"[general] mode не прочитан: {e}")
    if mode not in ("new", "legacy"):
        errors.append(f"[general] mode: ожидается new или legacy, получено {mode!r} — взято new")
        mode = "new"
    legacy = mode == "legacy"

    values = {}
    for (section, key), (new_value, legacy_value) in _DEFAULTS.items():
        value = legacy_value if legacy else new_value
        try:
            if parser.has_option(section, key):
                raw = parser.get(section, key)
                parsed = _parse_bool(raw) if isinstance(new_value, bool) else int(raw)
                if not isinstance(parsed, bool) and parsed < 1:
                    raise ValueError(f"должно быть не меньше 1, получено {parsed}")
                value = parsed
        except Exception as e:
            errors.append(f"[{section}] {key}: {e} — взято {value}")
        values[(section, key)] = value
    values[("general", "mode")] = mode
    return values, found, errors


_values, SETTINGS_PATH, SETTINGS_ERRORS = load()


def get(section, key):
    return _values[(section, key)]


def summary():
    """Короткая строка для лога и консоли: только то, что отличается от нового поведения."""
    changed = [f"{s}.{k}={_values[(s, k)]}" for (s, k), (new_value, _) in _DEFAULTS.items()
               if _values[(s, k)] != new_value]
    mode = _values[("general", "mode")]
    head = f"mode={mode}"
    return head + ("; " + ", ".join(changed) if changed else "")


def log_effective():
    """В отладочный лог — откуда взяты настройки и что отличается от умолчаний."""
    diag.log("SETTINGS", file=SETTINGS_PATH or "-", effective=summary(),
             errors="; ".join(SETTINGS_ERRORS) or "-")
