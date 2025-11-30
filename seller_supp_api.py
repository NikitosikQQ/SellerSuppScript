import os
import sys
import time
import requests
import winsound

HOST = "http://localhost:8080"
USER_CONTEXT = []  # [{username, token, token_timestamp, workplace}, ...]
TOKEN_TTL = 12 * 60 * 60  # 12 часов

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
        resp = requests.post(auth_url, json=payload, verify=False, timeout=5)
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
        resp = requests.get(url, headers=headers, verify=False, timeout=5)
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

    # Берем токен первого авторизованного пользователя (как send_work_process)
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
        resp = requests.post(url, json=payload, headers=headers, verify=False, timeout=5)

        # Если сервер прислал 200 OK → стандартный json ответ ResultInformationResponse
        if resp.status_code == 200:
            data = resp.json()

            message = data.get("message")
            need_alert = data.get("needAlert")

            # 🔔 Воспроизводим звук, если сервер сказал
            if need_alert:
                play_notification_sound()
                return False, message

            # возвращаем message, если есть, иначе None
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
    Обрабатывает ResultInformationResponse:
      message: str
      orderWasUpdated: bool
      needAlert: bool
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
        resp = requests.post(url, json=payload, headers=headers, verify=False, timeout=5)

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

        # 1) Если needAlert = true → звук + ошибка
        if need_alert:
            play_notification_sound()
            return False, message or "Неизвестная ошибка"

        # 2) Если needAlert = false и orderWasUpdated = true → успех
        if order_was_updated:
            return True, "Данные успешно обработаны"

        # 3) Если needAlert = false и orderWasUpdated = false → ошибка + message
        return False, message or "Операция не выполнена"

    except Exception as e:
        play_notification_sound()
        return False, f"Ошибка запроса: {e}"

def download_packages(username: str, only_packaging_materials: bool):
    """
    Загружает PDF с этикетками для указанного пользователя.
    Возвращает (success: bool, message: str, pdf_bytes: Optional[bytes])

    :param username: имя пользователя
    :param only_packaging_materials: если True — загружает только упаковочные материалы
    """
    token = get_cached_token(username)
    if not token:
        play_notification_sound()
        return False, "❌ Нет токена. Авторизуйтесь заново.", None

    # ✅ передаём параметр onlyPackagingMaterials в запрос
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
    """
    Загружает PDF с этикетками для указанного пользователя.
    Возвращает (success: bool, message: str, pdf_bytes: Optional[bytes])

    :param username: имя пользователя
    :param order_number: номер заказа, с которого требуется скачать этикетку
    """
    token = get_cached_token(username)
    if not token:
        play_notification_sound()
        return False, "❌ Нет токена. Авторизуйтесь заново.", None

    # ✅ передаём параметр order_number в запрос
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
        # Путь до папки, где лежит .exe или исходник
        base_path = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
        sound_path = os.path.join(base_path, "ding.wav")

        # Проигрывание асинхронно (не блокирует UI)
        winsound.PlaySound(sound_path, winsound.SND_FILENAME | winsound.SND_ASYNC)
    except Exception as e:
        print(f"Не удалось воспроизвести звук: {e}")