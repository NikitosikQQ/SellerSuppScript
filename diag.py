"""Отладочная сборка цехового скрипта: подробный лог сети, печати и сканов.

Включается только в отладочной сборке (в exe вшит файл diag_build.flag — см.
auth_gui_diag.spec) или переменной окружения SS_DIAG=1 при запуске из исходников.
В обычной сборке модуль ничего не делает: патчи не ставятся, log() — пустышка.

Лог пишется рядом с exe: ss_diag_<ПК>_<дата>_<время>.log (если папка закрыта на
запись — в %TEMP%). Строка: «2026-10-07 14:10:29.299 +0300 [поток] ТЕГ ключ=значение».
Время локальное с миллисекундами и смещением — для сверки с логами сервера (UTC).

Что попадает в лог:
- старт: версии, ПК, прокси (переменные и реестр), ipconfig /all, синхронизация
  времени (w32tm), маршруты, принтеры, адреса сервера в DNS (A и AAAA);
- каждый HTTP-запрос: поиск системного прокси, DNS, каждая попытка TCP, TLS,
  отправка, ожидание ответа — и итоговая строка HTTP со всеми фазами, кодом ответа,
  размером и часами сервера (заголовок Date) либо ошибкой (connect/read timeout);
- каждый скан пилы по шагам: очередь, проверка заказа, QR+PDF, печать, отметка;
- печать: подгонка 58×40, ожидание очереди печати, PDFtoPrinter (время, код
  выхода, таймаут), состояние принтера по умолчанию (статус, задания в очереди);
- фоновые замеры каждые SS_DIAG_PROBE_SEC секунд (по умолчанию 15): DNS/TCP/TLS
  и HTTP до сервера, DNS/TCP/TLS до ya.ru — чтобы отличить «плохо до нашего
  сервера» от «плохо с интернетом вообще», ping шлюза; Wi-Fi — раз в минуту;
- зависания интерфейса дольше секунды, необработанные исключения.

Пароли и токены в лог не пишутся.
"""
import itertools
import os
import platform
import re
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from contextlib import contextmanager
from datetime import datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

_FLAG_FILE = "diag_build.flag"
REF_HOST = "ya.ru"  # опорный сайт: если тормозит и он — дело в интернете цеха, а не в нашем сервере


def _flag_in_bundle():
    """Флаг ищем только внутри собранного exe: в исходниках он лежит рядом с кодом,
    и запуск из исходников иначе всегда включал бы диагностику (там — SS_DIAG=1)."""
    meipass = getattr(sys, "_MEIPASS", None)
    return bool(meipass) and os.path.exists(os.path.join(meipass, _FLAG_FILE))


ENABLED = os.environ.get("SS_DIAG") == "1" or _flag_in_bundle()

try:
    PROBE_INTERVAL_SEC = int(os.environ.get("SS_DIAG_PROBE_SEC", "15"))
except ValueError:
    PROBE_INTERVAL_SEC = 15

LOG_PATH = None

_CREATE_NO_WINDOW = 0x08000000
# Низкоуровневые строки, которые фоновые замеры не пишут (их итог — в одной строке PROBE/HTTP).
_QUIET_TAGS = {"PROXY", "DNS", "TCP", "TLS", "SEND", "WAIT"}

_write_lock = threading.Lock()
_fh = None
_tls = threading.local()
_req_seq = itertools.count(1)
_scan_seq = itertools.count(1)
_keep = []  # ссылки на QTimer, чтобы их не собрал GC
_server_url = None


# ---------------------------------------------------------------- запись лога

def ms_since(t0):
    return int((time.perf_counter() - t0) * 1000)


def _now_str():
    now = datetime.now().astimezone()
    return f"{now:%Y-%m-%d %H:%M:%S}.{now.microsecond // 1000:03d} {now:%z}"


def _fmt(value):
    if value is None:
        return "-"
    s = str(value).replace("\r", " ").replace("\n", " ⏎ ")
    if len(s) > 1500:
        s = s[:1500] + "…"
    if s == "" or any(c in s for c in ' ="'):
        s = '"' + s.replace('"', "'") + '"'
    return s


def _ctx():
    return getattr(_tls, "ctx", {})


@contextmanager
def op(name=None, **fields):
    """Контекст для всех строк лога из этого потока: op=…, sid=…, order=…"""
    if not ENABLED:
        yield
        return
    prev = _ctx()
    new = dict(prev)
    if name:
        new["op"] = name
    new.update({k: v for k, v in fields.items() if v is not None})
    _tls.ctx = new
    try:
        yield
    finally:
        _tls.ctx = prev


def log(tag, **fields):
    if not ENABLED or _fh is None:
        return
    try:
        ctx = _ctx()
        if ctx.get("quiet") and tag in _QUIET_TAGS:
            return
        items = [(k, v) for k, v in ctx.items() if k != "quiet" and k not in fields]
        items += list(fields.items())
        line = " ".join([_now_str(), f"[{threading.current_thread().name}]", tag]
                        + [f"{k}={_fmt(v)}" for k, v in items])
        with _write_lock:
            _fh.write(line + "\n")
            _fh.flush()
    except Exception:
        pass


def log_block(tag, text):
    """Многострочный вывод (ipconfig и т.п.) — каждая строка с префиксом «    | »."""
    if not ENABLED or _fh is None:
        return
    try:
        lines = (text or "").splitlines()
        head = f"{_now_str()} [{threading.current_thread().name}]"
        with _write_lock:
            _fh.write(f"{head} {tag}_BEGIN lines={len(lines)}\n")
            for ln in lines:
                if ln.strip():
                    _fh.write(f"    | {ln.rstrip()}\n")
            _fh.write(f"{head} {tag}_END\n")
            _fh.flush()
    except Exception:
        pass


def new_scan(**fields):
    """Новый скан: возвращает его номер (sid) для всех строк по этому скану."""
    if not ENABLED:
        return None
    sid = next(_scan_seq)
    log("SCAN_IN", sid=sid, **fields)
    return sid


# ---------------------------------------------------------------- фазы запроса

def _set_phase(name, value):
    ph = getattr(_tls, "phases", None)
    if ph is not None:
        ph[name] = value


def _inc_phase(name):
    ph = getattr(_tls, "phases", None)
    if ph is not None:
        ph[name] = ph.get(name, 0) + 1


def _phases_fields():
    ph = getattr(_tls, "phases", None) or {}
    return {k: v for k, v in ph.items() if k != "tcp_dns_ms" or "tls_ms" not in ph}


def _addr(address):
    try:
        return f"{address[0]}:{address[1]}"
    except Exception:
        return str(address)


def _short_url(url):
    try:
        p = urlsplit(url)
        return p.netloc + p.path + (("?" + p.query) if p.query else "")
    except Exception:
        return url


def _clock_offset(date_header):
    """Часы сервера минус часы ПК, в секундах (Date — с точностью до секунды)."""
    if not date_header:
        return None
    try:
        return round(parsedate_to_datetime(date_header).timestamp() - time.time())
    except Exception:
        return None


def _install_network_patches():
    import requests
    import requests.sessions
    import urllib3.connection as uc

    # DNS — каждый getaddrinfo (requests ходит через urllib3 → socket.getaddrinfo).
    orig_getaddrinfo = socket.getaddrinfo

    def getaddrinfo(host, port, *args, **kwargs):
        t0 = time.perf_counter()
        try:
            res = orig_getaddrinfo(host, port, *args, **kwargs)
        except Exception as e:
            log("DNS_FAIL", host=host, port=port, ms=ms_since(t0), exc=type(e).__name__, err=e)
            raise
        ms = ms_since(t0)
        _set_phase("dns_ms", ms)
        try:
            addrs = ",".join(sorted({r[4][0] for r in res}))
        except Exception:
            addrs = "?"
        log("DNS", host=host, port=port, ms=ms, addrs=addrs)
        return res

    socket.getaddrinfo = getaddrinfo

    # TCP — каждая попытка соединения (если адресов несколько, видно все попытки).
    orig_connect = socket.socket.connect

    def connect(self, address):
        t0 = time.perf_counter()
        try:
            res = orig_connect(self, address)
        except Exception as e:
            _inc_phase("tcp_fail")
            log("TCP_FAIL", addr=_addr(address), ms=ms_since(t0), exc=type(e).__name__, err=e)
            raise
        ms = ms_since(t0)
        _set_phase("tcp_ms", ms)
        log("TCP", addr=_addr(address), ms=ms)
        return res

    socket.socket.connect = connect

    # DNS+TCP одним куском — чтобы из полного connect() вычесть и получить TLS.
    orig_new_conn = uc.HTTPConnection._new_conn

    def _new_conn(self):
        t0 = time.perf_counter()
        sock = orig_new_conn(self)
        _set_phase("tcp_dns_ms", ms_since(t0))
        _set_phase("conn", "new")
        return sock

    uc.HTTPConnection._new_conn = _new_conn

    orig_https_connect = uc.HTTPSConnection.connect

    def https_connect(self):
        t0 = time.perf_counter()
        _set_phase("tcp_dns_ms", None)
        try:
            orig_https_connect(self)
        except Exception as e:
            ph = getattr(_tls, "phases", None) or {}
            stage = "tls" if ph.get("tcp_dns_ms") is not None else "tcp"
            log("CONNECT_FAIL", host=self.host, stage=stage, ms=ms_since(t0), exc=type(e).__name__, err=e)
            raise
        total = ms_since(t0)
        base = (getattr(_tls, "phases", None) or {}).get("tcp_dns_ms")
        tls_ms = total - base if base is not None else None
        _set_phase("tls_ms", tls_ms)
        try:
            version = self.sock.version()
        except Exception:
            version = None
        log("TLS", host=self.host, ms=tls_ms, connect_total_ms=total, version=version)

    uc.HTTPSConnection.connect = https_connect

    # Отправка запроса и ожидание заголовков ответа — здесь срабатывает «Read timed out».
    orig_request = uc.HTTPConnection.request

    def request(self, method, url, *args, **kwargs):
        t0 = time.perf_counter()
        try:
            res = orig_request(self, method, url, *args, **kwargs)
        except Exception as e:
            log("SEND_FAIL", method=method, path=url, ms=ms_since(t0), exc=type(e).__name__, err=e)
            raise
        ms = ms_since(t0)
        _set_phase("send_ms", ms)
        log("SEND", method=method, path=url, ms=ms)
        return res

    uc.HTTPConnection.request = request

    orig_getresponse = uc.HTTPConnection.getresponse

    def getresponse(self, *args, **kwargs):
        t0 = time.perf_counter()
        try:
            resp = orig_getresponse(self, *args, **kwargs)
        except Exception as e:
            _set_phase("wait_ms", ms_since(t0))
            log("WAIT_FAIL", ms=ms_since(t0), exc=type(e).__name__, err=e)
            raise
        ms = ms_since(t0)
        _set_phase("wait_ms", ms)
        log("WAIT", ms=ms, status=getattr(resp, "status", None))
        return resp

    uc.HTTPConnection.getresponse = getresponse

    # Поиск системного прокси: на Windows при включённом прокси requests на КАЖДЫЙ
    # запрос читает реестр и может делать обратный DNS (getfqdn) — это секунды.
    orig_get_environ_proxies = requests.sessions.get_environ_proxies

    def get_environ_proxies(url, no_proxy=None):
        t0 = time.perf_counter()
        res = orig_get_environ_proxies(url, no_proxy=no_proxy)
        ms = ms_since(t0)
        _set_phase("proxy_ms", ms)
        log("PROXY", ms=ms, proxies=res or "-")
        return res

    requests.sessions.get_environ_proxies = get_environ_proxies

    # Весь запрос целиком: итоговая строка HTTP / HTTP_FAIL со всеми фазами.
    orig_session_request = requests.Session.request

    def session_request(self, method, url, *args, **kwargs):
        rid = next(_req_seq)
        prev_ctx = _ctx()
        prev_phases = getattr(_tls, "phases", None)
        _tls.ctx = dict(prev_ctx, req=rid)
        _tls.phases = {}
        t0 = time.perf_counter()
        try:
            resp = orig_session_request(self, method, url, *args, **kwargs)
        except Exception as e:
            log("HTTP_FAIL", method=method, url=_short_url(url), timeout=kwargs.get("timeout"),
                total_ms=ms_since(t0), exc=type(e).__name__, err=e, **_phases_fields())
            raise
        else:
            fields = dict(method=method, url=_short_url(url), status=resp.status_code,
                          total_ms=ms_since(t0),
                          elapsed_ms=int(resp.elapsed.total_seconds() * 1000))
            if not kwargs.get("stream"):
                fields["bytes"] = len(resp.content)
            date = resp.headers.get("Date")
            fields["server_date"] = date
            offset = _clock_offset(date)
            if offset is not None:
                fields["clock_offset_s"] = offset
            fields.update(_phases_fields())
            log("HTTP", **fields)
            return resp
        finally:
            _tls.ctx = prev_ctx
            _tls.phases = prev_phases

    requests.Session.request = session_request


# ---------------------------------------------------------------- окружение ПК

def _oem_encoding():
    try:
        import ctypes
        return f"cp{ctypes.windll.kernel32.GetOEMCP()}"
    except Exception:
        return "utf-8"


def _run_cmd(args, timeout=10):
    """Консольная команда без окна (CREATE_NO_WINDOW — не крадёт фокус у сканера)."""
    if os.name != "nt":
        return ""
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = subprocess.SW_HIDE
    p = subprocess.run(args, capture_output=True, timeout=timeout,
                       creationflags=_CREATE_NO_WINDOW, startupinfo=si)
    out = (p.stdout or b"") + (p.stderr or b"")
    # Консольные утилиты пишут то в OEM-кодировке (cp866), то в UTF-8 (зависит от
    # настроек Windows) — сначала строгий UTF-8, при ошибке — OEM.
    try:
        return out.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        return out.decode(_oem_encoding(), errors="replace")
    except LookupError:
        return out.decode("utf-8", errors="replace")


def _log_cmd_block(tag, args, timeout=20):
    try:
        log_block(tag, _run_cmd(args, timeout))
    except Exception as e:
        log(tag + "_FAIL", exc=type(e).__name__, err=e)


def _log_proxy_settings():
    fields = {}
    try:
        import urllib.request
        fields["getproxies"] = urllib.request.getproxies() or "-"
    except Exception as e:
        fields["getproxies"] = f"err:{e}"
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy"):
        if os.environ.get(k):
            fields["env_" + k] = os.environ[k]
    try:
        import winreg
        path = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
            for name in ("ProxyEnable", "ProxyServer", "ProxyOverride", "AutoConfigURL", "AutoDetect"):
                try:
                    fields["reg_" + name] = winreg.QueryValueEx(key, name)[0]
                except OSError:
                    pass
    except Exception as e:
        fields["reg_err"] = e
    log("PROXY_SETTINGS", **fields)


_PRINTER_STATUS = [
    (0x1, "PAUSED"), (0x2, "ERROR"), (0x4, "PENDING_DELETION"), (0x8, "PAPER_JAM"),
    (0x10, "PAPER_OUT"), (0x20, "MANUAL_FEED"), (0x40, "PAPER_PROBLEM"), (0x80, "OFFLINE"),
    (0x100, "IO_ACTIVE"), (0x200, "BUSY"), (0x400, "PRINTING"), (0x800, "OUTPUT_BIN_FULL"),
    (0x1000, "NOT_AVAILABLE"), (0x2000, "WAITING"), (0x4000, "PROCESSING"),
    (0x8000, "INITIALIZING"), (0x10000, "WARMING_UP"), (0x20000, "TONER_LOW"),
    (0x40000, "NO_TONER"), (0x80000, "PAGE_PUNT"), (0x100000, "USER_INTERVENTION"),
    (0x200000, "OUT_OF_MEMORY"), (0x400000, "DOOR_OPEN"), (0x800000, "SERVER_UNKNOWN"),
    (0x1000000, "POWER_SAVE"),
]
_ATTR_WORK_OFFLINE = 0x400


def printer_snapshot(where):
    """Принтер по умолчанию: статус, задания в очереди Windows."""
    if not ENABLED:
        return
    t0 = time.perf_counter()
    try:
        import win32print
        name = win32print.GetDefaultPrinter()
        handle = win32print.OpenPrinter(name)
        try:
            info = win32print.GetPrinter(handle, 2)
            jobs = win32print.EnumJobs(handle, 0, 50, 1)
        finally:
            win32print.ClosePrinter(handle)
        status = info.get("Status", 0) or 0
        attrs = info.get("Attributes", 0) or 0
        flags = [n for bit, n in _PRINTER_STATUS if status & bit]
        if attrs & _ATTR_WORK_OFFLINE:
            flags.append("WORK_OFFLINE")
        queue = ";".join(f"{j.get('JobId')}:{j.get('pDocument')}:0x{(j.get('Status') or 0):x}"
                         for j in jobs) or "-"
        log("PRINTER", where=where, name=name, port=info.get("pPortName"),
            driver=info.get("pDriverName"), status=",".join(flags) or "OK",
            jobs=info.get("cJobs"), queue=queue, ms=ms_since(t0))
    except Exception as e:
        log("PRINTER_FAIL", where=where, ms=ms_since(t0), exc=type(e).__name__, err=e)


def printer_snapshot_async(where):
    """Снимок принтера в фоне — не задерживает печать и отметку."""
    if ENABLED:
        threading.Thread(target=printer_snapshot, args=(where,), name="diag-printer",
                         daemon=True).start()


def print_processes(where):
    """Живые процессы печати: PDFtoPrinter (AutoIt) и его PDF-XChange Viewer. Зависший
    процесс держит принтер и файлы во временной папке exe (_MEI)."""
    if not ENABLED:
        return
    try:
        out = _run_cmd(["tasklist", "/FO", "CSV", "/NH"], 10)
        procs = re.findall(r'^"((?:PDFtoPrinter|PDFXC[^"]*)\.exe)","(\d+)"', out, re.I | re.M)
        log("PRINT_PROCS", where=where, count=len(procs),
            procs=",".join(f"{name}:{pid}" for name, pid in procs) or "-")
    except Exception as e:
        log("PRINT_PROCS_FAIL", where=where, exc=type(e).__name__, err=e)


# Состояние печати — чтобы понять, совпадает ли закрытие окна с работой PDFtoPrinter.
_print_state = {"active": 0, "last_end": 0.0}
# Признаки, что окно закрывает человек: клик по рамке окна (крестик и т.п.) или Alt+F4.
# ВАЖНО: на Windows 11 крестик Qt-окна приходит голым WM_CLOSE (без SC_CLOSE, проверено
# 07.10.2026), поэтому только по типу сообщения человека от чужого процесса не отличить.
_close_state = {"last_user_input": 0.0, "last_block": 0.0}
_USER_INPUT_WINDOW_SEC = 3.0   # клик по рамке/Alt+F4 не раньше чем за 3 с до закрытия
_PRINT_WINDOW_SEC = 3.0        # блокируем только закрытие в первые 3 с после печати
_SECOND_TRY_SEC = 10.0         # повторная попытка в течение 10 с всегда проходит


def mark_print(active):
    if not ENABLED:
        return
    if active:
        _print_state["active"] += 1
    else:
        _print_state["active"] = max(0, _print_state["active"] - 1)
        _print_state["last_end"] = time.perf_counter()


def _since_print_ms():
    last = _print_state["last_end"]
    return int((time.perf_counter() - last) * 1000) if last else None


def foreground_info():
    """Чьё окно сейчас активно: «процесс.exe:pid:заголовок»."""
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.WinDLL("user32")
        kernel32 = ctypes.WinDLL("kernel32")
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                        ctypes.POINTER(wintypes.DWORD)]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        hwnd = user32.GetForegroundWindow()
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        title = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, title, 256)
        name = "?"
        handle = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
        if handle:
            buf = ctypes.create_unicode_buffer(520)
            size = wintypes.DWORD(520)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                name = os.path.basename(buf.value)
            kernel32.CloseHandle(handle)
        return f"{name}:{pid.value}:{title.value}"
    except Exception as e:
        return f"err:{type(e).__name__}"


def _ms_ago(t):
    return int((time.perf_counter() - t) * 1000) if t else None


def should_block_close(spontaneous):
    """Вызывать из closeEvent главного окна. Пишет, кто и когда закрывает окно.

    В отладочной сборке не даёт закрыть окно, только если ВСЁ сразу: закрытие пришло снаружи,
    в первые 3 с после печати, без клика по рамке окна и Alt+F4 перед ним, и это первая
    попытка. Повторная попытка в течение 10 с проходит всегда — человек закроет окно вторым
    нажатием, а чужой процесс (AutoIt внутри PDFtoPrinter) будет пойман строкой CLOSE_BLOCKED."""
    if not ENABLED:
        return False
    now = time.perf_counter()
    since_input = _ms_ago(_close_state["last_user_input"])
    since_block = _ms_ago(_close_state["last_block"])
    since_print = _since_print_ms()
    print_active = _print_state["active"] > 0
    user_input = since_input is not None and since_input < _USER_INPUT_WINDOW_SEC * 1000
    recent_print = print_active or (since_print is not None and since_print < _PRINT_WINDOW_SEC * 1000)
    second_try = since_block is not None and since_block < _SECOND_TRY_SEC * 1000
    block = bool(spontaneous) and recent_print and not user_input and not second_try
    log("CLOSE_EVENT", spontaneous=int(bool(spontaneous)), user_input=int(user_input),
        since_user_input_ms=since_input, print_active=int(print_active), since_print_ms=since_print,
        second_try=int(second_try), blocked=int(block), fg=foreground_info())
    if not spontaneous:
        log_block("CLOSE_STACK", "".join(traceback.format_stack(limit=25)))
    if block:
        _close_state["last_block"] = now
        log("CLOSE_BLOCKED", note="окно закрывают снаружи сразу после печати без клика и Alt+F4 — оставлено открытым")
        print_processes_async("close_blocked")
    return block


_WM_NAMES = {0x0010: "WM_CLOSE", 0x0011: "WM_QUERYENDSESSION", 0x0016: "WM_ENDSESSION",
             0x0012: "WM_QUIT", 0x0002: "WM_DESTROY"}


def install_window_watch(app, main_window):
    """Кто закрывает окно: системные сообщения закрытия, Alt/F4/Esc, смена активности окна."""
    if not ENABLED:
        return
    try:
        import ctypes
        from ctypes import wintypes
        from PyQt5.QtCore import QAbstractNativeEventFilter, QEvent, QObject, Qt

        main_hwnd = int(main_window.winId())

        class NativeFilter(QAbstractNativeEventFilter):
            last_key = None

            def nativeEventFilter(self, event_type, message):
                try:
                    msg = wintypes.MSG.from_address(int(message))
                    code = msg.message
                    # Ввод человека по окну (приходит через диспетчер Qt): клик по рамке —
                    # WM_NCLBUTTONDOWN/UP с кодом зоны (20 = крестик), Alt+F4 — WM_SYSKEYDOWN F4.
                    if code in (0x00A1, 0x00A2, 0x0242, 0x0243) or (code == 0x0104 and msg.wParam == 0x73):
                        if msg.hWnd == main_hwnd:
                            _close_state["last_user_input"] = time.perf_counter()
                            log("NATIVE", msg={0x00A1: "WM_NCLBUTTONDOWN", 0x00A2: "WM_NCLBUTTONUP",
                                               0x0242: "WM_NCPOINTERDOWN", 0x0243: "WM_NCPOINTERUP",
                                               0x0104: "WM_SYSKEYDOWN_F4"}[code],
                                hit=msg.wParam if code != 0x0104 else "-")
                        return False, 0
                    name = _WM_NAMES.get(code)
                    if name is None and code != 0x0112:
                        return False, 0
                    # Посланное (Post) сообщение Qt пропускает через фильтр дважды — диспетчер и
                    # оконная процедура; одинаковые окно+код+wParam+время считаем одним.
                    key = (msg.hWnd, code, msg.wParam, msg.time)
                    if key == self.last_key:
                        return False, 0
                    self.last_key = key
                    if code == 0x0112 and (msg.wParam & 0xFFF0) == 0xF060:
                        name = "WM_SYSCOMMAND_SC_CLOSE"  # Alt+F4 или крестик (если система его шлёт)
                        if msg.hWnd == main_hwnd:
                            _close_state["last_user_input"] = time.perf_counter()
                    if name and code != 0x0002 or (code == 0x0002 and msg.hWnd == main_hwnd):
                        log("NATIVE", msg=name, to_main=int(msg.hWnd == main_hwnd),
                            print_active=int(_print_state["active"] > 0),
                            since_print_ms=_since_print_ms(), fg=foreground_info())
                except Exception:
                    pass
                return False, 0

        class KeyFilter(QObject):
            def eventFilter(self, obj, event):
                try:
                    if event.type() == QEvent.KeyPress and event.spontaneous():
                        key = event.key()
                        if key in (Qt.Key_F4, Qt.Key_Escape, Qt.Key_Alt) \
                                or event.modifiers() & Qt.AltModifier:
                            log("KEY", key=key, mods=int(event.modifiers()), obj=type(obj).__name__,
                                since_print_ms=_since_print_ms(), fg=foreground_info())
                except Exception:
                    pass
                return False

        native = NativeFilter()
        keys = KeyFilter()
        app.installNativeEventFilter(native)
        app.installEventFilter(keys)
        _keep.extend([native, keys])

        state_names = {0: "suspended", 1: "hidden", 2: "inactive", 4: "active"}
        app.applicationStateChanged.connect(
            lambda st: log("APP_STATE", state=state_names.get(int(st), int(st)),
                           print_active=int(_print_state["active"] > 0),
                           since_print_ms=_since_print_ms(), fg=foreground_info()))
        log("WINDOW_WATCH", main_hwnd=hex(main_hwnd))
    except Exception as e:
        log("WINDOW_WATCH_FAIL", exc=type(e).__name__, err=e)


def print_processes_async(where, delay_sec=0):
    if not ENABLED:
        return

    def run():
        if delay_sec:
            time.sleep(delay_sec)
        print_processes(where)

    threading.Thread(target=run, name="diag-procs", daemon=True).start()


def log_exit():
    """Закрытие программы: что ещё живо из процессов печати (причина окна «Failed to remove
    temporary directory _MEI…» — занятый файл во временной папке exe)."""
    global _ui_last_tick
    if not ENABLED:
        return
    _ui_last_tick = None  # цикл событий кончился — тиков больше не будет, это не зависание
    log("EXIT", mei=getattr(sys, "_MEIPASS", "-"))
    print_processes("exit")


def _log_printers():
    try:
        import win32print
        flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
        default = win32print.GetDefaultPrinter()
        for p in win32print.EnumPrinters(flags, None, 2):
            log("PRINTER_LIST", name=p.get("pPrinterName"), port=p.get("pPortName"),
                driver=p.get("pDriverName"), default=int(p.get("pPrinterName") == default))
    except Exception as e:
        log("PRINTER_LIST_FAIL", exc=type(e).__name__, err=e)


_WIFI_KEYS = re.compile(r"SSID|BSSID|Сигнал|Signal|Скорость|rate|Канал|Channel|Состояние|State"
                        r"|Тип радио|Radio type", re.I)


def _log_wifi():
    try:
        out = _run_cmd(["netsh", "wlan", "show", "interfaces"], 10)
    except Exception as e:
        log("WIFI_FAIL", exc=type(e).__name__, err=e)
        return
    pairs = []
    for line in out.splitlines():
        if ":" in line and _WIFI_KEYS.search(line.split(":", 1)[0]):
            key, value = line.split(":", 1)
            pairs.append(f"{key.strip()}={value.strip()}")
    if pairs:
        log("WIFI", info="; ".join(pairs))
    else:
        log("WIFI", info="нет данных Wi-Fi (кабель или служба WLAN выключена)", raw=out.strip()[:200])


def _default_gateway():
    try:
        out = _run_cmd(["route", "print", "-4", "0.0.0.0"], 10)
    except Exception:
        return None
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0] == "0.0.0.0" and parts[1] == "0.0.0.0" \
                and re.match(r"^\d+\.\d+\.\d+\.\d+$", parts[2]):
            return parts[2]
    return None


_PING_RE = re.compile(r"[=<]\s*(\d+)\s*(?:мс|ms)", re.I)


def _ping(host):
    out = _run_cmd(["ping", "-n", "1", "-w", "1000", host], 5)
    m = _PING_RE.search(out)
    if m:
        return int(m.group(1)), None
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    return None, (lines[1] if len(lines) > 1 else out.strip()[:120])


def _detect_interception():
    """TCP до заведомо недоступных адресов (TEST-NET). Если соединение «устанавливается» —
    трафик перехватывает прозрачный прокси/VPN (TUN, xkeen/Xray на роутере): он принимает
    любое соединение сам, и сбой дальше выглядит как Read timed out, а не как ошибка связи."""
    for ip in ("192.0.2.1", "198.51.100.1"):
        t0 = time.perf_counter()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(3)
        try:
            sock.connect((ip, 80))
            log("INTERCEPT", ip=ip, result="connected", ms=ms_since(t0),
                note="соединение с недоступным адресом установилось — трафик перехватывается")
        except Exception as e:
            log("INTERCEPT", ip=ip, result=type(e).__name__, ms=ms_since(t0))
        finally:
            sock.close()


def _startup_dump():
    _log_proxy_settings()
    host = urlsplit(_server_url).hostname
    try:
        socket.getaddrinfo(host, 443, 0, socket.SOCK_STREAM)  # строка DNS: есть ли IPv6 (AAAA)
    except Exception:
        pass
    _detect_interception()
    _log_cmd_block("IPCONFIG", ["ipconfig", "/all"])
    _log_cmd_block("W32TM", ["w32tm", "/query", "/status"])
    _log_cmd_block("ROUTE", ["route", "print", "-4"])
    _log_printers()
    printer_snapshot("startup")
    _log_wifi()
    # Маршрут до сервера (до ~25 с): видно, идёт ли трафик напрямую к провайдеру или в туннель.
    _log_cmd_block("TRACERT", ["tracert", "-d", "-h", "15", "-w", "700", host], timeout=60)


# ---------------------------------------------------------------- фоновые замеры

def _probe_tls(host, port, target):
    """DNS → TCP → TLS до хоста, без HTTP. Проверка сертификата выключена — меряем время."""
    t0 = time.perf_counter()
    stage = "dns"
    try:
        infos = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
        dns_ms = ms_since(t0)
        ip = infos[0][4][0]
        stage = "tcp"
        t1 = time.perf_counter()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(10)
        try:
            sock.connect((ip, port))
            tcp_ms = ms_since(t1)
            stage = "tls"
            t2 = time.perf_counter()
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            with ctx.wrap_socket(sock, server_hostname=host) as ss:
                tls_ms = ms_since(t2)
                version = ss.version()
        finally:
            try:
                sock.close()
            except Exception:
                pass
        log("PROBE", target=target, host=host, ip=ip, ok=1, dns_ms=dns_ms, tcp_ms=tcp_ms,
            tls_ms=tls_ms, total_ms=ms_since(t0), tls=version)
    except Exception as e:
        log("PROBE", target=target, host=host, ok=0, stage=stage, total_ms=ms_since(t0),
            exc=type(e).__name__, err=e)


def _probe_http(url):
    """Настоящий HTTP-запрос к серверу тем же путём, что и сканы (строка HTTP op=probe)."""
    try:
        import requests
        requests.get(url, timeout=15, verify=False, allow_redirects=False)
    except Exception:
        pass  # HTTP_FAIL уже записан обёрткой


def _probe_loop():
    time.sleep(3)
    interval = max(1, PROBE_INTERVAL_SEC)
    host = urlsplit(_server_url).hostname
    gateway = None
    n = 0
    while True:
        try:
            if n % max(1, 3600 // interval) == 0:
                gateway = _default_gateway()
                log("GATEWAY", ip=gateway or "-")
            with op("probe", quiet=True):
                _probe_tls(host, 443, "server")
                _probe_http(_server_url + "/login")
                _probe_tls(REF_HOST, 443, "ref")
            if gateway:
                rtt, raw = _ping(gateway)
                log("PING", target="gateway", ip=gateway, rtt_ms=rtt, lost=int(rtt is None), raw=raw)
            if n % max(1, 60 // interval) == 0:
                _log_wifi()
                _resources()
            if n % max(1, 300 // interval) == 0:
                printer_snapshot("probe")
        except Exception as e:
            log("PROBE_ERR", exc=type(e).__name__, err=e)
        n += 1
        time.sleep(interval)


# ---------------------------------------------------------------- исключения, интерфейс

def _install_excepthooks():
    prev_hook = sys.excepthook

    def hook(exc_type, exc, tb):
        log("UNCAUGHT", exc=exc_type.__name__, err=exc,
            tb="".join(traceback.format_exception(exc_type, exc, tb))[-3000:])
        prev_hook(exc_type, exc, tb)

    sys.excepthook = hook

    prev_thread_hook = threading.excepthook

    def thread_hook(args):
        log("THREAD_EXC", thread=getattr(args.thread, "name", None),
            exc=getattr(args.exc_type, "__name__", args.exc_type), err=args.exc_value,
            tb="".join(traceback.format_exception(args.exc_type, args.exc_value,
                                                  args.exc_traceback))[-3000:])
        prev_thread_hook(args)

    threading.excepthook = thread_hook


_ui_last_tick = None
_UI_HANG_DUMP_SEC = 3.0


def _all_stacks():
    names = {t.ident: t.name for t in threading.enumerate()}
    parts = []
    for ident, frame in sys._current_frames().items():
        stack = "".join(traceback.format_stack(frame, limit=12))
        parts.append(f"--- поток {names.get(ident, ident)} ---\n{stack}")
    return "\n".join(parts)


def _ui_hang_watch():
    """Фоновая проверка: UI-поток не тикал > 3 с — пишем стеки всех потоков (где висит)."""
    dumped = False
    while True:
        time.sleep(1)
        last = _ui_last_tick
        if last is None:
            continue
        stalled = time.perf_counter() - last
        if stalled > _UI_HANG_DUMP_SEC and not dumped:
            log("UI_HANG", stalled_ms=int(stalled * 1000))
            log_block("UI_HANG_STACKS", _all_stacks())
            dumped = True
        elif stalled <= _UI_HANG_DUMP_SEC:
            dumped = False


def _install_qt_message_handler():
    """Предупреждения Qt (в том числе «из чужого потока») — частая причина падений GUI."""
    try:
        from PyQt5.QtCore import qInstallMessageHandler

        def handler(mode, context, message):
            log("QT_MSG", mode=int(mode), msg=message,
                where=f"{getattr(context, 'file', None)}:{getattr(context, 'line', None)}")

        qInstallMessageHandler(handler)
        _keep.append(handler)
    except Exception as e:
        log("QT_MSG_HANDLER_FAIL", exc=type(e).__name__, err=e)


def install_ui_watchdog():
    """Тикер на UI-потоке раз в 250 мс: пауза между тиками > 1 с = интерфейс висел.
    Вызывать после создания QApplication, из главного потока."""
    global _ui_last_tick
    if not ENABLED:
        return
    from PyQt5.QtCore import QTimer
    _install_qt_message_handler()
    _ui_last_tick = time.perf_counter()

    def tick():
        global _ui_last_tick
        now = time.perf_counter()
        gap_ms = int((now - _ui_last_tick) * 1000)
        _ui_last_tick = now
        if gap_ms > 1000:
            log("UI_STALL", ms=gap_ms)

    timer = QTimer()
    timer.timeout.connect(tick)
    timer.start(250)
    _keep.append(timer)
    threading.Thread(target=_ui_hang_watch, name="diag-uiwatch", daemon=True).start()


def _resources():
    """Память процесса, потоки, дескрипторы, объекты GDI/USER — утечки перед падением."""
    fields = {"threads": threading.active_count()}
    try:
        import ctypes
        from ctypes import wintypes

        class PMC(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]

        kernel32 = ctypes.WinDLL("kernel32")
        user32 = ctypes.WinDLL("user32")
        # argtypes обязательны: псевдо-дескриптор процесса (-1) иначе не влезает в int.
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]
        kernel32.GetProcessHandleCount.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        user32.GetGuiResources.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        proc = kernel32.GetCurrentProcess()
        pmc = PMC()
        pmc.cb = ctypes.sizeof(PMC)
        if kernel32.K32GetProcessMemoryInfo(proc, ctypes.byref(pmc), pmc.cb):
            fields["rss_mb"] = round(pmc.WorkingSetSize / 1048576, 1)
            fields["peak_mb"] = round(pmc.PeakWorkingSetSize / 1048576, 1)
            fields["private_mb"] = round(pmc.PagefileUsage / 1048576, 1)
        handles = wintypes.DWORD()
        if kernel32.GetProcessHandleCount(proc, ctypes.byref(handles)):
            fields["handles"] = handles.value
        fields["gdi"] = user32.GetGuiResources(proc, 0)
        fields["user"] = user32.GetGuiResources(proc, 1)
    except Exception as e:
        fields["err"] = f"{type(e).__name__}: {e}"
    log("RES", **fields)


# ---------------------------------------------------------------- старт

def _open_log():
    global _fh, LOG_PATH
    name = f"ss_diag_{socket.gethostname()}_{datetime.now():%Y-%m-%d_%H-%M-%S}.log"
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    else:
        exe_dir = os.path.dirname(os.path.abspath(__file__))
    for folder in (exe_dir, tempfile.gettempdir()):
        try:
            path = os.path.join(folder, name)
            _fh = open(path, "a", encoding="utf-8", buffering=1)
            LOG_PATH = path
            return
        except OSError:
            continue


def _log_versions():
    versions = {"openssl": ssl.OPENSSL_VERSION}
    for mod, attr in (("requests", "__version__"), ("urllib3", "__version__"),
                      ("fitz", "VersionBind"), ("PIL", "__version__"),
                      ("PyQt5.QtCore", "QT_VERSION_STR")):
        try:
            versions[mod.replace(".", "_")] = getattr(__import__(mod, fromlist=["_"]), attr, None)
        except Exception as e:
            versions[mod.replace(".", "_")] = f"err:{type(e).__name__}"
    log("VERSIONS", **versions)


def init(app_version, server_url):
    """Открыть лог, поставить перехватчики, запустить сбор окружения и фоновые замеры."""
    global _server_url
    if not ENABLED or _fh is not None:
        return
    _server_url = server_url.rstrip("/")
    _open_log()
    if _fh is None:
        return
    log("START", version=app_version, exe=sys.executable,
        frozen=int(bool(getattr(sys, "frozen", False))), python=platform.python_version(),
        os=platform.platform(), pc=socket.gethostname(), user=os.environ.get("USERNAME"),
        pid=os.getpid(), server=_server_url, probe_sec=PROBE_INTERVAL_SEC)
    _log_versions()
    # Аварийное завершение (access violation, abort) — стеки всех потоков в этот же файл.
    try:
        import faulthandler
        faulthandler.enable(file=_fh, all_threads=True)
    except Exception as e:
        log("FAULTHANDLER_FAIL", exc=type(e).__name__, err=e)
    # Важно: свой sys.excepthook отключает у PyQt5 аварийный выход (qFatal → abort) при
    # необработанном исключении в обработчике Qt — в отладочной сборке программа не
    # закроется, а исключение с трассировкой попадёт в лог (строка UNCAUGHT).
    _install_excepthooks()
    _install_network_patches()
    threading.Thread(target=_startup_dump, name="diag-env", daemon=True).start()
    if PROBE_INTERVAL_SEC > 0:
        threading.Thread(target=_probe_loop, name="diag-probe", daemon=True).start()
