"""Повтор запросов при сетевой ошибке: проверка заказа (пила) и отметка работы.

requests.post подменён — в сеть ничего не уходит. По умолчанию (другие посты) повторов нет
и сообщения прежние.
"""
import http.client
import unittest
from unittest import mock

import requests
import urllib3.exceptions as ue

import seller_supp_api as api


def not_sent_error():
    """Соединение не установилось (отказ/DNS) — так requests оборачивает NewConnectionError."""
    return requests.exceptions.ConnectionError(
        ue.MaxRetryError(None, "/api/v1/employees/work/process", reason=ue.NewConnectionError(None, "refused")))


def reset_after_send_error():
    """Соединение оборвалось после отправки запроса — запрос мог дойти до сервера."""
    return requests.exceptions.ConnectionError(
        ue.ProtocolError("Connection aborted.", http.client.RemoteDisconnected("closed")))


def response(status=200, data=None):
    r = mock.Mock()
    r.status_code = status
    r.text = "ошибка"
    r.json.return_value = data if data is not None else {}
    return r


OK_VALIDATION = response(200, {"message": None, "needAlert": False, "labelCode": None})
MARK_OK = response(200, {"message": None, "orderWasUpdated": True, "needAlert": False})
MARK_NOT_UPDATED = response(200, {"message": "Статус заказа X НЕ БЫЛ обновлен, текущий статус: PILA",
                                  "orderWasUpdated": False, "needAlert": False})


class ApiRetryTest(unittest.TestCase):
    def setUp(self):
        api.USER_CONTEXT.clear()
        api.save_token("tester", "token")
        api.save_workplace("tester", "Пила-мастер")
        self.sound = mock.patch.object(api, "play_notification_sound").start()
        mock.patch.object(api, "NETWORK_RETRY_PAUSE_S", 0).start()
        self.addCleanup(mock.patch.stopall)
        self.addCleanup(api.USER_CONTEXT.clear)

    def post(self, *effects):
        return mock.patch.object(api.requests, "post", side_effect=list(effects))

    def test_validate_order_retries_once_on_read_timeout(self):
        with self.post(requests.exceptions.ReadTimeout("read timed out"), OK_VALIDATION) as post:
            ok, message, _ = api.validate_order("X", True, timeout=(5, 8), network_retries=1)
        self.assertTrue(ok)
        self.assertEqual(post.call_count, 2)
        self.assertEqual(post.call_args.kwargs["timeout"], (5, 8))
        self.sound.assert_not_called()

    def test_validate_order_retries_once_on_connection_error(self):
        with self.post(requests.exceptions.ConnectionError("reset"), OK_VALIDATION) as post:
            ok, _, _ = api.validate_order("X", True, network_retries=1)
        self.assertTrue(ok)
        self.assertEqual(post.call_count, 2)

    def test_validate_order_does_not_retry_on_http_500(self):
        with self.post(response(500), OK_VALIDATION) as post:
            ok, message, _ = api.validate_order("X", True, network_retries=1)
        self.assertFalse(ok)
        self.assertEqual(post.call_count, 1)
        self.assertIn("Ошибка сервера 500", message)

    def test_validate_order_gives_up_after_second_timeout_with_single_sound(self):
        with self.post(requests.exceptions.ReadTimeout("t1"), requests.exceptions.ReadTimeout("t2")) as post:
            ok, message, _ = api.validate_order("X", True, timeout=(5, 8), network_retries=1)
        self.assertFalse(ok)
        self.assertEqual(post.call_count, 2)
        self.assertIn("попыток: 2", message)
        self.sound.assert_called_once()

    def test_validate_order_default_is_single_attempt_15s_old_message(self):
        with self.post(requests.exceptions.ReadTimeout("read timed out")) as post:
            ok, message, _ = api.validate_order("X", True)
        self.assertFalse(ok)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.kwargs["timeout"], 15)
        self.assertEqual(message, "Ошибка запроса: read timed out")

    def test_mark_retries_once_when_connection_not_established(self):
        with self.post(not_sent_error(), MARK_OK) as post:
            ok, message = api.send_work_process("X", "EARNING", network_retries=1)
        self.assertTrue(ok)
        self.assertEqual(post.call_count, 2)

    def test_mark_retries_once_on_connect_timeout(self):
        with self.post(requests.exceptions.ConnectTimeout("connect timed out"), MARK_OK) as post:
            ok, _ = api.send_work_process("X", "EARNING", network_retries=1)
        self.assertTrue(ok)
        self.assertEqual(post.call_count, 2)

    def test_mark_does_not_retry_after_read_timeout(self):
        # Запрос ушёл: первый мог ещё выполняться на сервере — параллельный повтор мог бы начислить дважды.
        with self.post(requests.exceptions.ReadTimeout("read timed out"), MARK_OK) as post:
            ok, message = api.send_work_process("X", "EARNING", network_retries=1)
        self.assertFalse(ok)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(message, "Ошибка запроса: read timed out")

    def test_mark_does_not_retry_on_connection_reset_after_send(self):
        with self.post(reset_after_send_error(), MARK_OK) as post:
            ok, _ = api.send_work_process("X", "EARNING", network_retries=1)
        self.assertFalse(ok)
        self.assertEqual(post.call_count, 1)

    def test_request_not_sent_classification(self):
        self.assertTrue(api._request_not_sent(not_sent_error()))
        self.assertTrue(api._request_not_sent(requests.exceptions.ConnectTimeout("t")))
        self.assertFalse(api._request_not_sent(requests.exceptions.ReadTimeout("t")))
        self.assertFalse(api._request_not_sent(reset_after_send_error()))
        self.assertFalse(api._request_not_sent(requests.exceptions.ConnectionError("непонятно что")))

    def test_validate_retries_even_after_send(self):
        # Проверка заказа идемпотентна (WB-статус сохраняется тот же) — её повторять можно всегда.
        with self.post(reset_after_send_error(), OK_VALIDATION) as post:
            ok, _, _ = api.validate_order("X", True, network_retries=1)
        self.assertTrue(ok)
        self.assertEqual(post.call_count, 2)

    def test_mark_default_has_no_retry(self):
        with self.post(not_sent_error(), MARK_OK) as post:
            ok, message = api.send_work_process("X", "EARNING")
        self.assertFalse(ok)
        self.assertEqual(post.call_count, 1)
        self.assertTrue(message.startswith("Ошибка запроса: "))

    def test_mark_does_not_retry_on_http_error(self):
        with self.post(response(502), MARK_OK) as post:
            ok, _ = api.send_work_process("X", "EARNING", network_retries=1)
        self.assertFalse(ok)
        self.assertEqual(post.call_count, 1)


if __name__ == "__main__":
    unittest.main()
