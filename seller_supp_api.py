import os
import sys
import time
import platform
import subprocess
import threading
import fitz
import requests
import winsound

HOST = "https://seller-supp.ru"
USER_CONTEXT = []  # [{username, token, token_timestamp, workplace}, ...]
TOKEN_TTL = 12 * 60 * 60  # 12 часов

# Windows: запуск дочернего процесса без мелькающего консольного окна.
CREATE_NO_WINDOW = 0x08000000

# Сериализация печати: пики из разных потоков (Пила/Упаковка) встают в очередь и
# печатают строго по одному. Иначе два PDFtoPrinter стартуют одновременно →
# столкновение на принтере и всплывашка «принтер занят». UI не виснет — каждый
# пик и так в своём daemon-потоке, ждёт лишь своей очереди на сам момент печати.
_PRINT_LOCK = threading.Lock()

# Физическая этикетка цеха (мм). Любой исходный PDF (Ozon 75×120, WB 106×79,
# внутренние любых размеров) ужимается под этот размер. Поменялся сток наклеек —
# правим только эти две константы и пересобираем.
LABEL_WIDTH_MM = 58
LABEL_HEIGHT_MM = 40
_MM_TO_PT = 72.0 / 25.4  # 1 мм в пунктах PDF


def _normalize_to_label(src_path):
    """Приводит каждую страницу PDF к размеру этикетки LABEL_WIDTH×HEIGHT (мм).

    Вписывает исходную страницу в МАКСИМАЛЬНОМ масштабе (впритык к краям, без
    полей), выбирая поворот (0° или 90°), дающий больший масштаб (= эффективнее,
    авто-ориентация как у Adobe — в отличие от Sumatra, которая крутила не в ту
    сторону). По той стороне, где из-за пропорций остаётся место, контент
    центрируется. show_pdf_page копирует векторно, без растеризации — QR и текст
    остаются чёткими при любом DPI принтера. Работает для ЛЮБОГО исходного размера.

    Возвращает путь к новому PDF (страницы ровно = этикетке, печать в actual-size).
    """
    target_w = LABEL_WIDTH_MM * _MM_TO_PT
    target_h = LABEL_HEIGHT_MM * _MM_TO_PT
    src = fitz.open(src_path)
    out = fitz.open()
    try:
        for pno in range(src.page_count):
            rect = src[pno].rect
            sw, sh = rect.width, rect.height
            # Кандидаты: без поворота (sw×sh) и с поворотом 90° (sh×sw).
            # Масштаб — максимальный под весь лист этикетки (впритык, без полей).
            best = None
            for rot, eff_w, eff_h in ((0, sw, sh), (90, sh, sw)):
                if eff_w <= 0 or eff_h <= 0:
                    continue
                scale = min(target_w / eff_w, target_h / eff_h)
                if best is None or scale > best[0]:
                    best = (scale, rot, eff_w, eff_h)
            scale, rot, eff_w, eff_h = best
            placed_w, placed_h = eff_w * scale, eff_h * scale
            # Центрируем — по «упёршейся» стороне поля нет, по другой остаток делится поровну.
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

    1) Нормализует страницы под физическую этикетку 58×40 (см. _normalize_to_label):
       любой исходный размер вписывается с авто-поворотом — заменяет «умную»
       подгонку Adobe, которая после обновления Acrobat DC (26.001.21563 от
       18.05.2026) стала показывать окно ридера и копить процессы
       (wiki/bugs/2026-05-29_packer-print-acrobat-window-opens-after-update).
    2) Печатает результат через вшитый PDFtoPrinter.exe в actual-size — без окон,
       без зависимости от установленного PDF-ридера.

    Фолбэки: если нормализация упала — печатаем исходник как есть; если
    PDFtoPrinter.exe не вшит — старый ShellExecute. Non-Windows: lp.
    """
    try:
        # 1) Нормализация под этикетку (best-effort — при сбое печатаем оригинал).
        #    Можно делать вне блокировки — это CPU-операция, не трогает принтер.
        to_print = pdf_path
        try:
            to_print = _normalize_to_label(pdf_path)
        except Exception:
            to_print = pdf_path

        # 2) Печать — строго по одному заданию за раз (см. _PRINT_LOCK выше).
        with _PRINT_LOCK:
            if platform.system() == "Windows":
                base_path = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
                printer_exe = os.path.join(base_path, "PDFtoPrinter.exe")
                if os.path.exists(printer_exe):
                    # /s — тихий режим PDFtoPrinter (без промптов «принтер занят» и т.п.).
                    # STARTUPINFO + SW_HIDE жёстко прячет любое окно дочернего процесса,
                    # чтобы печать НЕ перехватывала фокус приложения. Иначе на момент
                    # печати фокус уходил из окна (QLineEdit -> None) и следующий пик
                    # сканера летел «в никуда» — терялся (диагностика 2026-05-30, лог фокуса).
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
                # Фолбэк на старое поведение, если бинарь не вшит.
                import win32api
                return win32api.ShellExecute(0, "print", to_print, None, ".", 0) > 32
            ret = os.system(f"lp '{to_print}'")
            return ret == 0
    except Exception:
        return False


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
            "workplace": ""
        })


def save_workplace(username, workplace):
    user = get_user_context(username)
    if user:
        user["workplace"] = workplace


def authorize(username, password):
    auth_url = f"{HOST}/auth"
    payload = {"username": username, "password": password}
    try:
        resp = requests.post(auth_url, json=payload, verify=False, timeout=15)
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


def get_workplaces(username):
    token = get_cached_token(username)
    if not token:
        return False, "❌ Нет токена. Авторизуйтесь заново."
    url = f"{HOST}/api/v1/admin/users/{username}/workplaces"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        resp = requests.get(url, headers=headers, verify=False, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list) and data:
                return True, data
            else:
                return False, "⚠️ Нет доступных рабочих мест."
        else:
            return False, f"❌ Ошибка {resp.status_code}: обратитесь к администратору."
    except Exception as e:
        return False, f"❌ Ошибка при получении рабочих мест: {e}"


def validate_secondary_auth(username, selected_wp):
    """Возвращает (needs_secondary, required_wp, msg)"""
    if selected_wp == "Пила-1":
        return True, "Пила-2", f"Для выбранного рабочего места {selected_wp} требуется вторичная авторизация."
    elif selected_wp == "Пила-2":
        return True, "Пила-1", f"Для выбранного рабочего места {selected_wp} требуется вторичная авторизация."
    return False, None, ""


def is_user_in_context(username):
    return any(u["username"] == username for u in USER_CONTEXT)


def remove_user_from_context(username):
    global USER_CONTEXT
    USER_CONTEXT = [u for u in USER_CONTEXT if u["username"] != username]


def validate_order(order_number: str, is_employee_prepared_facade: bool):
    """
    Выполняет валидацию заказа.
    Возвращает (success: bool, message: Optional[str])
    """
    if not USER_CONTEXT:
        return False, "Нет авторизованных пользователей."

    username = USER_CONTEXT[0]["username"]
    token = get_cached_token(username)
    if not token:
        play_notification_sound()
        return False, f"Нет токена для пользователя {username}."

    url = f"{HOST}/api/v1/orders/validation"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    payload = {
        "orderNumber": order_number,
        "isEmployeePreparedFacade": is_employee_prepared_facade
    }

    try:
        resp = requests.post(url, json=payload, headers=headers, verify=False, timeout=15)

        if resp.status_code == 200:
            data = resp.json()

            message = data.get("message")
            need_alert = data.get("needAlert")

            if need_alert:
                play_notification_sound()
                return False, message

            return True, message if message else None

        else:
            play_notification_sound()
            return False, f"Ошибка сервера {resp.status_code}: {resp.text}"

    except Exception as e:
        play_notification_sound()
        return False, f"Ошибка запроса: {e}"


def send_work_process(order_number: str, operation_type: str):
    """
    Отправка информации о выполненном объеме работы всех авторизованных пользователей.
    """
    if not USER_CONTEXT:
        return False, "Нет авторизованных пользователей для отправки данных."

    employees = []
    for u in USER_CONTEXT:
        if "username" in u and "workplace" in u and u["username"] and u["workplace"]:
            employees.append({
                "username": u["username"],
                "workplace": u["workplace"]
            })

    if not employees:
        return False, "Нет корректных пользователей для отправки."

    token = get_cached_token(employees[0]["username"])
    if not token:
        return False, f"Нет токена для пользователя {employees[0]['username']}."

    url = f"{HOST}/api/v1/employees/work/process"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    payload = {
        "employees": employees,
        "orderNumber": order_number,
        "operationType": operation_type
    }

    try:
        resp = requests.post(url, json=payload, headers=headers, verify=False, timeout=15)

        if resp.status_code != 200:
            play_notification_sound()
            return False, f"Ошибка сервера {resp.status_code}: {resp.text}"

        try:
            data = resp.json()
        except:
            play_notification_sound()
            return False, "Ошибка: сервер вернул некорректный JSON."

        message = data.get("message")
        order_was_updated = data.get("orderWasUpdated")
        need_alert = data.get("needAlert")

        if need_alert:
            play_notification_sound()
            return False, message or "Неизвестная ошибка"

        if order_was_updated:
            return True, "Данные успешно обработаны"

        return False, message or "Операция не выполнена"

    except Exception as e:
        play_notification_sound()
        return False, f"Ошибка запроса: {e}"


def download_packages(username: str, only_packaging_materials: bool):
    token = get_cached_token(username)
    if not token:
        play_notification_sound()
        return False, "❌ Нет токена. Авторизуйтесь заново.", None

    url = f"{HOST}/api/v1/orders/packages?onlyPackagingMaterials={'true' if only_packaging_materials else 'false'}"
    headers = {"Authorization": f"Bearer {token}"}

    try:
        resp = requests.post(url, headers=headers, verify=False, timeout=300)
        if resp.status_code == 200:
            return True, None, resp.content
        elif resp.status_code == 404:
            return False, "⚠️ Готовых к упаковке заказов не найдено", None
        else:
            play_notification_sound()
            return False, f"❌ Ошибка {resp.status_code}: {resp.text}", None
    except Exception as e:
        play_notification_sound()
        return False, f"❌ Ошибка запроса: {e}", None


def download_package_by_order(username: str, order_number: str):
    token = get_cached_token(username)
    if not token:
        play_notification_sound()
        return False, "❌ Нет токена. Авторизуйтесь заново.", None

    url = f"{HOST}/api/v1/orders/{order_number}/package"
    headers = {"Authorization": f"Bearer {token}"}

    try:
        resp = requests.get(url, headers=headers, verify=False, timeout=120)
        if resp.status_code == 200:
            return True, None, resp.content
        elif resp.status_code == 404:
            return False, f"⚠️ Заказ не был найден по номеру {order_number}", None
        else:
            play_notification_sound()
            return False, f"❌ Ошибка {resp.status_code}: {resp.text}", None
    except Exception as e:
        play_notification_sound()
        return False, f"❌ Ошибка запроса: {e}", None


def play_notification_sound():
    """Проигрывает звуковой сигнал (работает и в .exe)"""
    try:
        base_path = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
        sound_path = os.path.join(base_path, "ding.wav")
        winsound.PlaySound(sound_path, winsound.SND_FILENAME | winsound.SND_ASYNC)
    except Exception as e:
        print(f"Не удалось воспроизвести звук: {e}")
