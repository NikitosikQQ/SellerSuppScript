"""print_pdf: блокировка фокуса на время печати, таймаут с завершением дерева процессов, коды выхода.

PDFtoPrinter не запускается: subprocess.Popen подменён. Реальной печати нет.
"""
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from contextlib import contextmanager
from unittest import mock

import fitz

import seller_supp_api as api
import settings


@contextmanager
def override_settings(**values):
    """override_settings(print__focus_lock=False) — временно меняет настройку."""
    saved = {}
    for name, value in values.items():
        section, key = name.split("__")
        saved[(section, key)] = settings._values[(section, key)]
        settings._values[(section, key)] = value
    try:
        yield
    finally:
        settings._values.update(saved)


class FakeProc:
    def __init__(self, returncode=0, hang=False):
        self.returncode = returncode
        self.hang = hang
        self.pid = 4242
        self.killed = False

    def wait(self, timeout=None):
        if self.hang and not self.killed:
            raise subprocess.TimeoutExpired("PDFtoPrinter.exe", timeout)
        return self.returncode

    def kill(self):
        self.killed = True


class PrintPdfTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pp_test_")
        self.pdf = os.path.join(self.tmp, "label.pdf")
        doc = fitz.open()
        doc.new_page(width=100, height=100)
        doc.save(self.pdf)
        doc.close()
        self.lock_calls = []
        self.unlocked = threading.Event()

        def fake_lock(code):
            self.lock_calls.append(code)
            if code == api._LSFW_UNLOCK:
                self.unlocked.set()
            return True, 0

        self._drop_pending_unlock()  # таймер прошлого теста не должен сработать в этом
        patcher = mock.patch.object(api, "_lock_set_foreground_window", side_effect=fake_lock)
        patcher.start()
        self.addCleanup(patcher.stop)
        tail = mock.patch.object(api, "FOCUS_LOCK_TAIL_S", 0.05)
        tail.start()
        self.addCleanup(tail.stop)
        # platform.system() в первый раз может запустить «ver» через subprocess — под подменённым
        # Popen это сломало бы тест. Подменяем ответ: печать идёт по ветке Windows.
        system = mock.patch.object(api.platform, "system", return_value="Windows")
        system.start()
        self.addCleanup(system.stop)

    def tearDown(self):
        self._drop_pending_unlock()
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _drop_pending_unlock():
        with api._focus_mutex:
            timer = api._focus_unlock_timer
            api._focus_unlock_timer = None
        if timer is not None:
            timer.cancel()

    def run_print(self, proc):
        order = []

        def fake_popen(args, **kwargs):
            order.append(("popen", list(self.lock_calls)))
            self.assertIn("/s", args)
            return proc

        with mock.patch.object(api.subprocess, "Popen", side_effect=fake_popen):
            result = api.print_pdf(self.pdf)
        return result, order

    def test_print_pdf_locks_foreground_before_run_and_unlocks_after_tail(self):
        with override_settings(print__focus_lock=True):
            ok, order = self.run_print(FakeProc(0))
        self.assertTrue(ok)
        self.assertEqual(order[0], ("popen", [api._LSFW_LOCK]), "блокировка ставится до запуска PDFtoPrinter")
        self.assertTrue(self.unlocked.wait(2), "блокировка снимается после хвоста")
        self.assertEqual(self.lock_calls, [api._LSFW_LOCK, api._LSFW_UNLOCK])

    def test_print_pdf_unlocks_when_run_raises(self):
        with override_settings(print__focus_lock=True):
            with mock.patch.object(api.subprocess, "Popen", side_effect=OSError("нет файла")):
                ok = api.print_pdf(self.pdf)
        self.assertFalse(ok)
        self.assertTrue(self.unlocked.wait(2), "блокировка снимается и при ошибке запуска")

    def test_print_pdf_timeout_kills_process_tree_and_returns_false(self):
        proc = FakeProc(hang=True)
        with override_settings(print__timeout_s=7, print__kill_tree=True), \
                mock.patch.object(api, "_kill_process_tree") as kill_tree:
            ok, _ = self.run_print(proc)
        self.assertFalse(ok)
        kill_tree.assert_called_once_with(proc.pid)
        self.assertTrue(proc.killed)

    def test_print_pdf_timeout_without_kill_tree_kills_only_parent(self):
        proc = FakeProc(hang=True)
        with override_settings(print__kill_tree=False), \
                mock.patch.object(api, "_kill_process_tree") as kill_tree:
            ok, _ = self.run_print(proc)
        self.assertFalse(ok)
        kill_tree.assert_not_called()
        self.assertTrue(proc.killed)

    def test_print_pdf_uses_timeout_from_settings(self):
        seen = {}

        class Proc(FakeProc):
            def wait(self, timeout=None):
                seen["timeout"] = timeout
                return 0

        with override_settings(print__timeout_s=17):
            ok, _ = self.run_print(Proc())
        self.assertTrue(ok)
        self.assertEqual(seen["timeout"], 17)

    def test_print_pdf_nonzero_returncode_returns_false(self):
        ok, _ = self.run_print(FakeProc(returncode=1))
        self.assertFalse(ok)

    def test_stale_unlock_timer_does_not_remove_new_lock(self):
        current = object()
        with mock.patch.object(api, "_focus_unlock_timer", current):
            self.assertFalse(api._focus_unlock_if_current(object()), "чужой (опоздавший) таймер")
            self.assertEqual(self.lock_calls, [])
            self.assertTrue(api._focus_unlock_if_current(current))
        self.assertEqual(self.lock_calls, [api._LSFW_UNLOCK])

    def test_print_pdf_skips_lock_when_disabled(self):
        with override_settings(print__focus_lock=False):
            ok, _ = self.run_print(FakeProc(0))
        self.assertTrue(ok)
        self.assertEqual(self.lock_calls, [])

    def test_legacy_settings_match_26_10_5(self):
        values, _, errors = settings.load(path=os.path.join(self.tmp, "нет_такого.ini"))
        self.assertEqual(errors, [])
        self.assertEqual(values[("print", "timeout_s")], 30)
        legacy_ini = os.path.join(self.tmp, "ss_settings.ini")
        with open(legacy_ini, "w", encoding="utf-8") as f:
            f.write("[general]\nmode = legacy\n")
        values, _, errors = settings.load(path=legacy_ini)
        self.assertEqual(errors, [])
        self.assertEqual((values[("print", "timeout_s")], values[("print", "focus_lock")],
                          values[("print", "kill_tree")]), (60, False, False))


if __name__ == "__main__":
    unittest.main()
