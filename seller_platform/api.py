"""HTTP-клиент + печать этикеток для форка упаковщика под Seller Platform.

Форк `seller_supp_api.py` (seller-support / мебельный цех). Отличия:
- хост из `config.ini` рядом с .exe (не хардкод seller-supp.ru);
- `verify_ssl` из конфига (на проде true);
- фиксация работы (ЗП) идёт по `stageCode` (этап конвейера), а не по enum `workplace`
  (де-мебелизация бэкенда seller-platform: EmployeeDto.stageCode);
- выбор рабочего места заменён выбором ЭТАПА (`GET /api/v1/stages/my-workstations`);
- вырезаны furniture-специфики (вторичная авторизация пар Пила, чекбокс «Фасад»).

Механизм печати (`print_pdf`/`_normalize_to_label`/`PDFtoPrinter.exe`/`_PRINT_LOCK`/
`ding.wav`/фикс кражи фокуса) перенесён 1:1 — это проверенный код.
"""
import configparser
import os
import platform
import subprocess
import sys
import threading
import time

import fitz
import requests
import winsound

# ---------------------------------------------------------------------------
# Конфигурация (host / verify_ssl) из config.ini рядом с .exe или main.py.
# ---------------------------------------------------------------------------

def _external_dir():
    """Каталог, где лежит редактируемый config.ini: рядом с .exe (frozen) или с модулем (dev)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _bundle_dir():
    """Каталог вшитых ресурсов (PDFtoPrinter.exe, ding.wav): _MEIPASS в .exe, иначе рядом с модулем."""
    return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


def _load_config():
    cfg = configparser.ConfigParser()
    path = os.path.join(_external_dir(), "config.ini")
    host = "http://localhost:8082"
    verify_ssl = False
    try:
        if cfg.read(path, encoding="utf-8") and cfg.has_section("api"):
            host = cfg.get("api", "host", fallback=host).strip().rstrip("/")
            verify_ssl = cfg.getboolean("api", "verify_ssl", fallback=verify_ssl)
    except Exception:
        pass
    return host, verify_ssl


HOST, VERIFY_SSL = _load_config()

USER_CONTEXT = []  # [{username, token, token_timestamp, stage_code, stage_name}, ...]
TOKEN_TTL = 12 * 60 * 60  # 12 часов

# Windows: запуск дочернего процесса без мелькающего консольного окна.
CREATE_NO_WINDOW = 0x08000000

# Сериализация печати: пики из разных потоков встают в очередь и печатают строго по одному.
_PRINT_LOCK = threading.Lock()

# Физическая этикетка цеха (мм). Любой исходный PDF ужимается под этот размер.
LABEL_WIDTH_MM = 58
LABEL_HEIGHT_MM = 40
_MM_TO_PT = 72.0 / 25.4  # 1 мм в пунктах PDF


def _normalize_to_label(src_path):
    """Приводит каждую страницу PDF к размеру этикетки LABEL_WIDTH×HEIGHT (мм).

    Вписывает страницу в максимальном масштабе (впритык, без полей), выбирая поворот
    (0° или 90°), дающий больший масштаб. Векторное копирование (show_pdf_page) — QR и
    текст чёткие при любом DPI. Возвращает путь к нормализованному PDF.
    """
    target_w = LABEL_WIDTH_MM * _MM_TO_PT
    target_h = LABEL_HEIGHT_MM * _MM_TO_PT
    src = fitz.open(src_path)
    out = fitz.open()
    try:
        for pno in range(src.page_count):
            rect = src[pno].rect
            sw, sh = rect.width, rect.height
            best = None
            for rot, eff_w, eff_h in ((0, sw, sh), (90, sh, sw)):
                if eff_w <= 0 or eff_h <= 0:
                    continue
                scale = min(target_w / eff_w, target_h / eff_h)
                if best is None or scale > best[0]:
                    best = (scale, rot, eff_w, eff_h)
            scale, rot, eff_w, eff_h = best
            placed_w, placed_h = eff_w * scale, eff_h * scale
            x0 = (target_w - placed_w) / 2.0
            y0 = (target_h - placed_h) / 2.0
            dst = out.new_page(width=target_w, height=target_h)
            dst.show_pdf_page(
                fitz.Rect(x0, y0, x0 + placed_w, y0 + placed_h),
                src, pno, rotate=rot,
            )
        norm_path = (src_path[:-4] if src_path.lower().endswith(".pdf") else src_path) + "_label.pdf"
        out.save(norm_path)
        return norm_path
    finally:
        out.close()
        src.close()


def print_pdf(pdf_path):
    """Тихая печать PDF этикетки на принтер по умолчанию. True при успехе.

    Нормализует под 58×40 мм (best-effort), печатает через вшитый PDFtoPrinter.exe в
    actual-size без окон. STARTUPINFO+SW_HIDE прячет окно дочернего процесса, чтобы печать
    не перехватывала фокус (иначе следующий пик сканера терялся). Фолбэк: ShellExecute / lp.
    """
    try:
        to_print = pdf_path
        try:
            to_print = _normalize_to_label(pdf_path)
        except Exception:
            to_print = pdf_path

        with _PRINT_LOCK:
            if platform.system() == "Windows":
                printer_exe = os.path.join(_bundle_dir(), "PDFtoPrinter.exe")
                if os.path.exists(printer_exe):
                    si = subprocess.STARTUPINFO()
                    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                    si.wShowWindow = subprocess.SW_HIDE
                    completed = subprocess.run(
                        [printer_exe, to_print, "/s"],
                        creationflags=CREATE_NO_WINDOW,
                        startupinfo=si,
                        timeout=60,
                    )
                    return completed.returncode == 0
                import win32api
                return win32api.ShellExecute(0, "print", to_print, None, ".", 0) > 32
            ret = os.system(f"lp '{to_print}'")
            return ret == 0
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Контекст пользователя / токен (12 ч).
# ---------------------------------------------------------------------------

def get_user_context(username):
    for user in USER_CONTEXT:
        if user["username"] == username:
            return user
    return None


def get_cached_token(username):
    user = get_user_context(username)
    if user and (time.time() - user["token_timestamp"] < TOKEN_TTL):
        return user["token"]
    return None


def save_token(username, token):
    user = get_user_context(username)
    if user:
        user["token"] = token
        user["token_timestamp"] = time.time()
    else:
        USER_CONTEXT.append({
            "username": username,
            "token": token,
            "token_timestamp": time.time(),
            "stage_code": "",
            "stage_name": "",
        })


def save_stage(username, stage_code, stage_name=""):
    """Сохраняет выбранный этап (код для work/process + отображаемое имя для UI)."""
    user = get_user_context(username)
    if user:
        user["stage_code"] = stage_code
        user["stage_name"] = stage_name


def is_user_in_context(username):
    return any(u["username"] == username for u in USER_CONTEXT)


def remove_user_from_context(username):
    global USER_CONTEXT
    USER_CONTEXT = [u for u in USER_CONTEXT if u["username"] != username]


# ---------------------------------------------------------------------------
# HTTP-вызовы seller-platform.
# ---------------------------------------------------------------------------

def authorize(username, password):
    auth_url = f"{HOST}/auth"
    payload = {"username": username, "password": password}
    try:
        resp = requests.post(auth_url, json=payload, verify=VERIFY_SSL, timeout=15)
        if resp.status_code == 200:
            token = resp.json().get("token") or resp.json().get("access_token")
            if token:
                save_token(username, token)
                return True, token
        elif resp.status_code == 401:
            play_notification_sound()
            return False, "401"
        else:
            play_notification_sound()
            return False, f"server_error:{resp.status_code}"
    except Exception as e:
        play_notification_sound()
        return False, f"exception:{e}"


def get_workstations(username):
    """Список рабочих этапов текущего пользователя (GET /api/v1/stages/my-workstations).

    Возвращает (True, [{'code','displayName'}, ...]) либо (False, текст ошибки).
    Бэкенд отдаёт этапы с has_workstation_tab, отфильтрованные по назначениям users.stage_codes.
    """
    token = get_cached_token(username)
    if not token:
        return False, "❌ Нет токена. Авторизуйтесь заново."
    url = f"{HOST}/api/v1/stages/my-workstations"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        resp = requests.get(url, headers=headers, verify=VERIFY_SSL, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list) and data:
                return True, data
            return False, "⚠️ Нет доступных рабочих этапов (проверьте назначения в учётке)."
        return False, f"❌ Ошибка {resp.status_code}: обратитесь к администратору."
    except Exception as e:
        return False, f"❌ Ошибка при получении этапов: {e}"


def validate_order(order_number):
    """Валидация заказа перед скан-флоу (POST /api/v1/orders/validation).

    Furniture-параметр isEmployeePreparedFacade убран. Возвращает (success, message).
    """
    if not USER_CONTEXT:
        return False, "Нет авторизованных пользователей."

    username = USER_CONTEXT[0]["username"]
    token = get_cached_token(username)
    if not token:
        play_notification_sound()
        return False, f"Нет токена для пользователя {username}."

    url = f"{HOST}/api/v1/orders/validation"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {"orderNumber": order_number}

    try:
        resp = requests.post(url, json=payload, headers=headers, verify=VERIFY_SSL, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            message = data.get("message")
            if data.get("needAlert"):
                play_notification_sound()
                return False, message
            return True, message if message else None
        play_notification_sound()
        return False, f"Ошибка сервера {resp.status_code}: {resp.text}"
    except Exception as e:
        play_notification_sound()
        return False, f"Ошибка запроса: {e}"


def send_work_process(order_number, operation_type):
    """Фиксация работы (ЗП). Шлёт {username, stageCode} (де-мебелизация: stageCode вместо workplace).

    POST /api/v1/employees/work/process. operation_type: EARNING | PENALTY.
    """
    if not USER_CONTEXT:
        return False, "Нет авторизованных пользователей для отправки данных."

    employees = []
    for u in USER_CONTEXT:
        if u.get("username") and u.get("stage_code"):
            employees.append({"username": u["username"], "stageCode": u["stage_code"]})

    if not employees:
        return False, "Нет корректных пользователей (не выбран этап)."

    token = get_cached_token(employees[0]["username"])
    if not token:
        return False, f"Нет токена для пользователя {employees[0]['username']}."

    url = f"{HOST}/api/v1/employees/work/process"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {
        "employees": employees,
        "orderNumber": order_number,
        "operationType": operation_type,
    }

    try:
        resp = requests.post(url, json=payload, headers=headers, verify=VERIFY_SSL, timeout=15)
        if resp.status_code != 200:
            play_notification_sound()
            return False, f"Ошибка сервера {resp.status_code}: {resp.text}"
        try:
            data = resp.json()
        except Exception:
            play_notification_sound()
            return False, "Ошибка: сервер вернул некорректный JSON."

        message = data.get("message")
        if data.get("needAlert"):
            play_notification_sound()
            return False, message or "Неизвестная ошибка"
        if data.get("orderWasUpdated"):
            return True, "Данные успешно обработаны"
        return False, message or "Операция не выполнена"
    except Exception as e:
        play_notification_sound()
        return False, f"Ошибка запроса: {e}"


def download_package_by_order(username, order_number):
    """Этикетка одного заказа (GET /api/v1/orders/{n}/package). (success, message, pdf_bytes)."""
    token = get_cached_token(username)
    if not token:
        play_notification_sound()
        return False, "❌ Нет токена. Авторизуйтесь заново.", None

    url = f"{HOST}/api/v1/orders/{order_number}/package"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        resp = requests.get(url, headers=headers, verify=VERIFY_SSL, timeout=120)
        if resp.status_code == 200:
            return True, None, resp.content
        elif resp.status_code == 404:
            return False, f"⚠️ Заказ не найден по номеру {order_number}", None
        else:
            play_notification_sound()
            return False, f"❌ Ошибка {resp.status_code}: {resp.text}", None
    except Exception as e:
        play_notification_sound()
        return False, f"❌ Ошибка запроса: {e}", None


def play_notification_sound():
    """Проигрывает звуковой сигнал (работает и в .exe)."""
    try:
        sound_path = os.path.join(_bundle_dir(), "ding.wav")
        winsound.PlaySound(sound_path, winsound.SND_FILENAME | winsound.SND_ASYNC)
    except Exception as e:
        print(f"Не удалось воспроизвести звук: {e}")
