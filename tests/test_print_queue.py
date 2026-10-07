"""Очередь печати пилы: порядок наклеек, пачки, брак, отметки после печати, сбои, закрытие.

Запуск: .venv/Scripts/python.exe -m unittest discover -s tests -t . -v
Без Qt, сети и принтера: проверка, сборка PDF, склейка, печать и отметка — подставные функции.
«PDF» здесь — текстовый файл с номерами заказов, «печать» читает его и запоминает пачку.
"""
import os
import shutil
import tempfile
import threading
import time
import unittest

from print_queue import EARNING, PENALTY, PrintPipeline


class Fake:
    def __init__(self, tmp_dir):
        self.tmp_dir = tmp_dir
        self.lock = threading.Lock()
        self.printed = []          # пачки: [[заказ, ...], ...]
        self.marks = []            # [(заказ, операция), ...]
        self.messages = []
        self.validation = {}       # заказ -> (ok, message)
        self.gates = {}            # заказ -> Event: проверка ждёт, пока не откроют
        self.validated = threading.Semaphore(0)
        self.print_ok = True
        self.print_raises = None
        self.print_gate = None
        self.mark_result = (True, "Данные успешно обработаны")

    def validate(self, order):
        gate = self.gates.get(order)
        if gate is not None:
            gate.wait(5)
        result = self.validation.get(order, (True, None))
        self.validated.release()
        return result

    def build(self, order, path):
        with open(path, "w", encoding="utf-8") as f:
            f.write(order + "\n")

    def merge(self, paths, out):
        with open(out, "w", encoding="utf-8") as f:
            for p in paths:
                with open(p, encoding="utf-8") as src:
                    f.write(src.read())

    def print_(self, path):
        if self.print_gate is not None:
            self.print_gate.wait(5)
        if self.print_raises is not None:
            raise self.print_raises
        with open(path, encoding="utf-8") as f:
            batch = [line.strip() for line in f if line.strip()]
        with self.lock:
            self.printed.append(batch)
        return self.print_ok

    def mark(self, order, op):
        with self.lock:
            self.marks.append((order, op))
        return self.mark_result

    def message(self, text):
        with self.lock:
            self.messages.append(text)

    def wait_validated(self, n):
        for _ in range(n):
            if not self.validated.acquire(timeout=5):
                raise AssertionError("проверки не завершились")


class PrintQueueTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pq_test_")
        self.f = Fake(self.tmp)
        self.pipelines = []

    def tearDown(self):
        for p in self.pipelines:
            p.shutdown()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make(self, **kwargs):
        p = PrintPipeline(validate_fn=self.f.validate, build_pdf_fn=self.f.build, merge_fn=self.f.merge,
                          print_fn=self.f.print_, mark_fn=self.f.mark, on_message=self.f.message,
                          tmp_dir=self.tmp, **kwargs)
        self.pipelines.append(p)
        return p

    def drain(self, p):
        unfinished = p.drain(5)
        self.assertEqual(unfinished, [], f"не завершились: {unfinished}")

    def flat(self):
        return [order for batch in self.f.printed for order in batch]

    def test_jobs_print_in_scan_order_even_if_later_validation_finishes_first(self):
        self.f.gates["A"] = threading.Event()
        p = self.make()
        for order in ("A", "B", "C"):
            p.submit(order, EARNING)
        self.f.wait_validated(2)          # B и C уже проверены, A ещё нет
        time.sleep(0.2)
        self.assertEqual(self.f.printed, [], "печать не должна обгонять голову очереди")
        self.f.gates["A"].set()
        self.drain(p)
        self.assertEqual(self.flat(), ["A", "B", "C"])
        self.assertEqual(self.f.marks, [("A", EARNING), ("B", EARNING), ("C", EARNING)])

    def test_batch_merges_consecutive_validated_earning_jobs_up_to_max(self):
        self.f.gates["1"] = threading.Event()
        p = self.make(batch_max=5)
        for i in range(1, 8):
            p.submit(str(i), EARNING)
        self.f.wait_validated(6)          # 2..7 проверены, 1 ждёт
        self.f.gates["1"].set()
        self.drain(p)
        self.assertEqual(self.f.printed, [["1", "2", "3", "4", "5"], ["6", "7"]])
        self.assertEqual([m[0] for m in self.f.marks], [str(i) for i in range(1, 8)])

    def test_batch_max_one_prints_each_label_separately(self):
        self.f.gates["1"] = threading.Event()
        p = self.make(batch_max=1)
        for i in range(1, 4):
            p.submit(str(i), EARNING)
        self.f.wait_validated(2)
        self.f.gates["1"].set()
        self.drain(p)
        self.assertEqual(self.f.printed, [["1"], ["2"], ["3"]])

    def test_failed_validation_is_skipped_without_print_or_mark(self):
        self.f.validation["B"] = (False, "Заказ отменён")
        p = self.make()
        for order in ("A", "B", "C"):
            p.submit(order, EARNING)
        self.drain(p)
        self.assertEqual(self.flat(), ["A", "C"])
        self.assertEqual([m[0] for m in self.f.marks], ["A", "C"])
        self.assertTrue(any("B" in m and "проверка не пройдена" in m and "Заказ отменён" in m
                            for m in self.f.messages))

    def test_penalty_marks_without_print_and_keeps_order_relative_to_earning(self):
        self.f.gates["X"] = threading.Event()
        p = self.make()
        p.submit("X", EARNING)
        p.submit("X", PENALTY)
        p.submit("Y", EARNING)
        time.sleep(0.2)
        self.f.gates["X"].set()
        self.drain(p)
        self.assertEqual(self.flat(), ["X", "Y"], "брак не печатается")
        self.assertEqual(self.f.marks, [("X", EARNING), ("X", PENALTY), ("Y", EARNING)],
                         "брак не должен обгонять отметку того же заказа — иначе пропадёт штраф")

    def test_marks_sent_only_after_successful_print(self):
        self.f.print_gate = threading.Event()
        p = self.make()
        p.submit("A", EARNING)
        time.sleep(0.3)
        self.assertEqual(self.f.marks, [], "отметка раньше печати")
        self.f.print_gate.set()
        self.drain(p)
        self.assertEqual(self.f.marks, [("A", EARNING)])

    def test_print_failure_sends_no_marks_and_reports_all_batch_orders(self):
        self.f.gates["A"] = threading.Event()
        self.f.print_ok = False
        p = self.make()
        for order in ("A", "B"):
            p.submit(order, EARNING)
        self.f.wait_validated(1)
        self.f.gates["A"].set()
        self.drain(p)
        self.assertEqual(self.f.marks, [])
        failure = [m for m in self.f.messages if "НЕ напечатано" in m]
        self.assertEqual(len(failure), 1)
        self.assertIn("A", failure[0])
        self.assertIn("B", failure[0])

    def test_print_exception_does_not_kill_worker_next_batch_prints(self):
        self.f.print_raises = RuntimeError("принтер упал")
        p = self.make()
        p.submit("A", EARNING)
        self.drain(p)
        self.assertTrue(any("НЕ напечатано" in m and "принтер упал" in m for m in self.f.messages))
        self.f.print_raises = None
        p2_seq = None
        # Очередь закрыта drain-ом: проверяем на новой — поток печати старой должен быть жив до shutdown.
        p = self.make()
        p2_seq = p.submit("B", EARNING)
        self.drain(p)
        self.assertIsNotNone(p2_seq)
        self.assertEqual(self.flat(), ["B"])

    def test_worker_survives_exception_within_same_pipeline(self):
        calls = {"n": 0}
        original = self.f.print_

        def flaky(path):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("сбой первой печати")
            return original(path)

        self.f.gates["A"] = threading.Event()
        p = PrintPipeline(validate_fn=self.f.validate, build_pdf_fn=self.f.build, merge_fn=self.f.merge,
                          print_fn=flaky, mark_fn=self.f.mark, on_message=self.f.message,
                          tmp_dir=self.tmp, batch_max=1)
        self.pipelines.append(p)
        p.submit("A", EARNING)
        p.submit("B", EARNING)
        self.f.wait_validated(1)
        self.f.gates["A"].set()
        self.drain(p)
        self.assertEqual(self.flat(), ["B"])
        self.assertEqual(self.f.marks, [("B", EARNING)])

    def test_mark_failure_is_reported(self):
        self.f.mark_result = (False, "Ошибка запроса: Read timed out")
        p = self.make()
        p.submit("A", EARNING)
        self.drain(p)
        self.assertTrue(any("A" in m and "❌" in m and "Read timed out" in m for m in self.f.messages))
        self.assertEqual(self.f.marks, [("A", EARNING)], "повторы — забота mark_fn, очередь не повторяет")

    def test_duplicate_pending_scan_is_ignored(self):
        self.f.gates["A"] = threading.Event()
        p = self.make(dedupe=True)
        self.assertIsNotNone(p.submit("A", EARNING))
        self.assertIsNone(p.submit("A", EARNING), "дубль, пока заказ ждёт печати")
        self.assertIsNotNone(p.submit("A", PENALTY), "другая операция — не дубль")
        self.f.gates["A"].set()
        self.drain(p)
        self.assertEqual(self.flat(), ["A"])
        self.assertTrue(any("уже в очереди" in m for m in self.f.messages))

    def test_duplicate_allowed_when_dedupe_off(self):
        self.f.gates["A"] = threading.Event()
        p = self.make(dedupe=False, batch_max=1)
        p.submit("A", EARNING)
        p.submit("A", EARNING)
        self.f.gates["A"].set()
        self.drain(p)
        self.assertEqual(self.flat(), ["A", "A"])

    def test_drain_waits_for_inflight_print_and_marks(self):
        self.f.print_gate = threading.Event()
        p = self.make()
        p.submit("A", EARNING)
        threading.Timer(0.3, self.f.print_gate.set).start()
        self.assertEqual(p.drain(5), [])
        self.assertEqual(self.f.marks, [("A", EARNING)])
        self.assertIsNone(p.submit("B", EARNING), "после drain сканы не принимаются")

    def test_drain_timeout_returns_unfinished_jobs(self):
        self.f.gates["A"] = threading.Event()
        p = self.make()
        p.submit("A", EARNING)
        p.submit("B", EARNING)
        unfinished = p.drain(0.3)
        self.assertEqual([(seq, order) for seq, order, _ in unfinished], [(1, "A"), (2, "B")])
        self.f.gates["A"].set()

    def test_counts_and_busy(self):
        self.f.print_gate = threading.Event()
        p = self.make()
        self.assertFalse(p.is_busy())
        p.submit("A", EARNING)
        self.assertTrue(p.is_busy())
        self.f.print_gate.set()
        self.drain(p)
        self.assertFalse(p.is_busy())
        self.assertEqual(p.counts(), {"queued": 0, "printing": 0, "marks": 0})

    def test_penalty_is_busy_while_being_handled(self):
        busy_during = []
        p = None

        def on_message(text):
            self.f.message(text)
            if "брак — печать не нужна" in text:
                busy_during.append(p.is_busy())

        p = PrintPipeline(validate_fn=self.f.validate, build_pdf_fn=self.f.build, merge_fn=self.f.merge,
                          print_fn=self.f.print_, mark_fn=self.f.mark, on_message=on_message, tmp_dir=self.tmp)
        self.pipelines.append(p)
        p.submit("X", PENALTY)
        self.drain(p)
        self.assertEqual(busy_during, [True], "брак, забранный из очереди, должен быть виден как работа")
        self.assertEqual(self.f.marks, [("X", PENALTY)])

    def test_pending_includes_mark_in_flight_and_failures_are_kept(self):
        gate = threading.Event()
        original = self.f.mark

        def slow_mark(order, op):
            gate.wait(5)
            original(order, op)
            return False, "Ошибка запроса: reset"

        p = PrintPipeline(validate_fn=self.f.validate, build_pdf_fn=self.f.build, merge_fn=self.f.merge,
                          print_fn=self.f.print_, mark_fn=slow_mark, on_message=self.f.message, tmp_dir=self.tmp)
        self.pipelines.append(p)
        p.submit("A", EARNING)
        unfinished = p.drain(0.5)
        self.assertEqual(unfinished, [(1, "A", "отправляется отметка")])
        gate.set()
        self.drain(p)
        self.assertEqual(p.mark_failures(), [(1, "A", EARNING, "Ошибка запроса: reset")])

    def test_failing_callbacks_do_not_break_queue(self):
        def boom(*_args):
            raise RuntimeError("колбэк упал")

        p = PrintPipeline(validate_fn=self.f.validate, build_pdf_fn=self.f.build, merge_fn=self.f.merge,
                          print_fn=self.f.print_, mark_fn=self.f.mark, on_message=boom, on_state=boom,
                          tmp_dir=self.tmp)
        self.pipelines.append(p)
        p.submit("A", EARNING)
        p.submit("B", PENALTY)
        self.drain(p)
        self.assertEqual(self.flat(), ["A"])
        self.assertEqual(self.f.marks, [("A", EARNING), ("B", PENALTY)])

    def test_temp_files_removed_after_batch_success_and_failure(self):
        cleaned = []
        p = self.make(cleanup_fn=cleaned.append)
        p.submit("A", EARNING)
        p.submit("B", EARNING)
        self.drain(p)
        self.f.print_ok = False
        p = self.make(cleanup_fn=cleaned.append)
        p.submit("C", EARNING)
        self.drain(p)
        self.assertEqual(os.listdir(self.tmp), [], "временные PDF должны удаляться")
        self.assertTrue(cleaned, "cleanup_fn вызывается для побочных файлов печати")


if __name__ == "__main__":
    unittest.main()
