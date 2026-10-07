import os
import sys
import time
import platform
import subprocess
import threading
import fitz
import requests
import winsound

import diag
import settings

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

# Запрет перехвата фокуса на время печати. PDFtoPrinter (AutoIt) на каждую печать дважды
# запускает PDF-XChange Viewer с видимым окном (@SW_SHOW — наш SW_HIDE до внука не доходит),
# и окно на ~2 с забирает фокус: скан в это время уходит в просмотрщик и теряется.
# LockSetForegroundWindow (может вызвать только активный процесс) не даёт чужим окнам стать
# активными; Windows сама снимает блокировку при клике пользователя или нажатии Alt.
# Проверено 07.10.2026: с блокировкой окна просмотрщика фокус не забирали ни разу.
# Снимаем с задержкой: всплывашка драйвера принтера может прийти уже после выхода PDFtoPrinter.
_LSFW_LOCK, _LSFW_UNLOCK = 1, 2
FOCUS_LOCK_TAIL_S = 1.5
_focus_mutex = threading.Lock()
_focus_unlock_timer = None


def _lock_set_foreground_window(code):
    """LockSetForegroundWindow → (ok, код ошибки Windows). Вынесено отдельно ради тестов."""
    import ctypes
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    ctypes.set_last_error(0)
    ok = bool(user32.LockSetForegroundWindow(code))
    return ok, ctypes.get_last_error()


def _focus_lock_begin():
    global _focus_unlock_timer
    try:
        with _focus_mutex:
            if _focus_unlock_timer is not None:
                _focus_unlock_timer.cancel()  # предыдущая печать ещё в «хвосте» — продлеваем
                _focus_unlock_timer = None
            ok, err = _lock_set_foreground_window(_LSFW_LOCK)
        diag.log("FOCUS_LOCK", ok=int(ok), err=err)
    except Exception as e:
        diag.log("FOCUS_LOCK_FAIL", exc=type(e).__name__, err=e)


def _focus_unlock_if_current(timer):
    """Снять блокировку, только если этот таймер всё ещё текущий. Иначе опоздавший таймер прошлой
    печати (cancel() пришёл, когда он уже сработал) снял бы блокировку, поставленную новой."""
    global _focus_unlock_timer
    try:
        with _focus_mutex:
            if _focus_unlock_timer is not timer:
                return False
            _focus_unlock_timer = None
            ok, err = _lock_set_foreground_window(_LSFW_UNLOCK)
        diag.log("FOCUS_UNLOCK", ok=int(ok), err=err)
        return True
    except Exception as e:
        diag.log("FOCUS_UNLOCK_FAIL", exc=type(e).__name__, err=e)
        return False


def _focus_unlock_later(delay_s=None):
    global _focus_unlock_timer
    delay = FOCUS_LOCK_TAIL_S if delay_s is None else delay_s  # константа читается при вызове
    with _focus_mutex:
        timer = threading.Timer(delay, _focus_unlock_if_current)
        timer.args = (timer,)
        timer.daemon = True
        _focus_unlock_timer = timer
        timer.start()


def _kill_process_tree(pid):
    """taskkill /T /F: завершить PDFtoPrinter вместе с PDF-XChange. Иначе зависший просмотрщик
    переживёт родителя, и PDFtoPrinter (у него защита от второго экземпляра) начнёт валить
    каждую следующую печать."""
    try:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True,
                       creationflags=CREATE_NO_WINDOW, timeout=10)
    except Exception as e:
        diag.log("PRINT_KILL_FAIL", pid=pid, exc=type(e).__name__, err=e)


def _run_pdftoprinter(args, startupinfo, timeout_s, kill_tree):
    """Запуск PDFtoPrinter с ожиданием. Код выхода или TimeoutExpired (процесс уже завершён)."""
    proc = subprocess.Popen(args, creationflags=CREATE_NO_WINDOW, startupinfo=startupinfo)
    try:
        return proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        if kill_tree:
            _kill_process_tree(proc.pid)
        try:
            proc.kill()
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
        raise

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
    t_all = time.perf_counter()
    try:
        # 1) Нормализация под этикетку (best-effort — при сбое печатаем оригинал).
        #    Можно делать вне блокировки — это CPU-операция, не трогает принтер.
        to_print = pdf_path
        t_norm = time.perf_counter()
        try:
            to_print = _normalize_to_label(pdf_path)
            diag.log("PRINT_NORMALIZE", ms=diag.ms_since(t_norm), ok=1)
        except Exception as e:
            to_print = pdf_path
            diag.log("PRINT_NORMALIZE", ms=diag.ms_since(t_norm), ok=0, exc=type(e).__name__, err=e)

        diag.printer_snapshot_async("before_print")
        t_lock = time.perf_counter()
        # 2) Печать — строго по одному заданию за раз (см. _PRINT_LOCK выше).
        with _PRINT_LOCK:
            diag.log("PRINT_LOCK", wait_ms=diag.ms_since(t_lock))
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
                    timeout_s = settings.get("print", "timeout_s")
                    focus_lock = settings.get("print", "focus_lock")
                    t_run = time.perf_counter()
                    diag.mark_print(True)
                    if focus_lock:
                        _focus_lock_begin()
                    try:
                        returncode = _run_pdftoprinter([printer_exe, to_print, "/s"], si, timeout_s,
                                                       settings.get("print", "kill_tree"))
                    except subprocess.TimeoutExpired:
                        diag.log("PRINT_RUN", result="timeout", ms=diag.ms_since(t_run), timeout_s=timeout_s)
                        diag.printer_snapshot_async("after_timeout")
                        diag.print_processes_async("after_timeout")
                        raise
                    finally:
                        diag.mark_print(False)
                        if focus_lock:
                            _focus_unlock_later()
                    diag.log("PRINT_RUN", result="ok" if returncode == 0 else "error",
                             returncode=returncode, ms=diag.ms_since(t_run))
                    diag.printer_snapshot_async("after_print")
                    diag.print_processes_async("after_print", delay_sec=2)
                    return returncode == 0
                # Фолбэк на старое поведение, если бинарь не вшит.
                import win32api
                return win32api.ShellExecute(0, "print", to_print, None, ".", 0) > 32
            ret = os.system(f"lp '{to_print}'")
            return ret == 0
    except Exception as e:
        diag.log("PRINT_FAIL", ms=diag.ms_since(t_all), exc=type(e).__name__, err=e)
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
    diag.log("WORKPLACE", user=username, workplace=workplace)


def authorize(username, password):
    auth_url = f"{HOST}/auth"
    payload = {"username": username, "password": password}
    try:
        with diag.op("auth", user=username):
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
        with diag.op("workplaces", user=username):
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


# Сетевые ошибки, при которых имеет смысл повторить запрос: соединение не установилось или ответ
# не пришёл (в цеху трафик идёт через туннель xkeen, ответы там иногда теряются — 07.10.2026
# сервер ответил на проверку за 82 мс, а пила ждала 15 с и получила Read timed out).
_NETWORK_ERRORS = (requests.exceptions.ConnectionError, requests.exceptions.Timeout)
NETWORK_RETRY_PAUSE_S = 0.3


def _any_network_error(e):
    return True


def _request_not_sent(e):
    """Запрос точно не дошёл до сервера: соединение не установилось (таймаут соединения, отказ,
    DNS). Только такую ОТМЕТКУ безопасно повторять: после отправки первый запрос мог ещё
    выполняться на сервере, а два параллельных /work/process могут оба начислить — у OrderEntity
    нет @Version, начисление read-modify-write (ревью 07.10.2026)."""
    import urllib3.exceptions as ue
    if isinstance(e, requests.exceptions.ConnectTimeout):
        return True
    seen, cause = set(), e
    while isinstance(cause, BaseException) and id(cause) not in seen:
        seen.add(id(cause))
        if isinstance(cause, ue.NewConnectionError):  # отказ, DNS (NameResolutionError — подкласс)
            return True
        nxt = getattr(cause, "reason", None)
        if nxt is None and cause.args and isinstance(cause.args[0], BaseException):
            nxt = cause.args[0]
        cause = nxt if nxt is not None else cause.__cause__
    return False


def _post_json(url, payload, headers, timeout, network_retries, op, retry_if=_any_network_error, **op_fields):
    """POST с повтором при сетевой ошибке, если retry_if(исключение) разрешает. Ответ сервера с
    ошибкой (4xx/5xx) не повторяется. Возвращает (resp, None, попыток) или (None, исключение, попыток)."""
    attempts = 1 + max(0, int(network_retries))
    for attempt in range(1, attempts + 1):
        try:
            with diag.op(op, attempt=attempt if attempts > 1 else None, **op_fields):
                resp = requests.post(url, json=payload, headers=headers, verify=False, timeout=timeout)
            return resp, None, attempt
        except _NETWORK_ERRORS as e:
            if attempt < attempts and retry_if(e):
                diag.log("NET_RETRY", op=op, attempt=attempt, exc=type(e).__name__)
                time.sleep(NETWORK_RETRY_PAUSE_S)
                continue
            return None, e, attempt


def validate_order(order_number: str, is_employee_prepared_facade: bool, timeout=15, network_retries=0):
    """
    Выполняет валидацию заказа.
    Возвращает (success: bool, message: Optional[str], label_code: Optional[str]).

    timeout / network_retries — для пилы: (5, 8) и один повтор при сетевой ошибке (ss_settings.ini,
    [pila] fast_validate). По умолчанию — как раньше: 15 с без повтора. Повторять проверку безопасно:
    у WB-заказов сервер опрашивает WB и сохраняет тот же статус — повтор ничего не меняет.

    label_code — код с этикетки маркетплейса (у OZON — scanit, «ii…»), по которому ищется
    страница в PDF: с 05.10.2026 на новой этикетке OZON нет номера отправления. None — кода
    нет (WB, ЯМ, старый макет, сервер старой версии), страница ищется по номеру.
    """
    if not USER_CONTEXT:
        return False, "Нет авторизованных пользователей.", None

    username = USER_CONTEXT[0]["username"]
    token = get_cached_token(username)
    if not token:
        play_notification_sound()
        return False, f"Нет токена для пользователя {username}.", None

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
        resp, net_error, attempts = _post_json(url, payload, headers, timeout, network_retries,
                                               "validate", order=order_number)
        if net_error is not None:
            play_notification_sound()
            tries = f" (попыток: {attempts})" if attempts > 1 else ""
            return False, f"Ошибка запроса{tries}: {net_error}", None

        if resp.status_code == 200:
            data = resp.json()

            message = data.get("message")
            need_alert = data.get("needAlert")
            label_code = data.get("labelCode")

            if need_alert:
                play_notification_sound()
                return False, message, None

            return True, (message if message else None), label_code

        else:
            play_notification_sound()
            return False, f"Ошибка сервера {resp.status_code}: {resp.text}", None

    except Exception as e:
        play_notification_sound()
        return False, f"Ошибка запроса: {e}", None


def send_work_process(order_number: str, operation_type: str, network_retries=0):
    """
    Отправка информации о выполненном объеме работы всех авторизованных пользователей.

    network_retries — для пилы один повтор ([pila] mark_retry), но ТОЛЬКО если запрос точно не ушёл
    (_request_not_sent: соединение не установилось). После отправки не повторяем: первый запрос мог
    ещё выполняться, и два параллельных могли бы оба начислить. По умолчанию — без повтора, как раньше.
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
        resp, net_error, attempts = _post_json(url, payload, headers, 15, network_retries, "mark",
                                               retry_if=_request_not_sent,
                                               order=order_number, operation=operation_type)
        if net_error is not None:
            play_notification_sound()
            tries = f" (попыток: {attempts})" if attempts > 1 else ""
            return False, f"Ошибка запроса{tries}: {net_error}"

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
        # С 05.10.2026 этикетки OZON формируются асинхронно (задание → опрос → файл): сервер
        # укладывается в ~270 с, здесь запас сверху. Обрыв раньше ответа терял весь PDF.
        with diag.op("download_packages"):
            resp = requests.post(url, headers=headers, verify=False, timeout=420)
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
        # Этикетка одного заказа тоже через асинхронное задание OZON — запас сверху.
        with diag.op("download_one", order=order_number):
            resp = requests.get(url, headers=headers, verify=False, timeout=180)
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
