"""Очередь печати наклеек пилы: скан не ждёт печати.

Скан → проверка заказа (параллельно, до validate_workers одновременно) → печать СТРОГО в порядке
сканов: подряд идущие проверенные наклейки склеиваются в одну печать (до batch_max) → отметка
работы после успешной печати, отдельной очередью в том же порядке.

Почему так:
- Порядок наклеек важнее скорости: наклейка, вышедшая не по порядку, окажется на чужой детали.
  Поэтому печать ждёт проверки «головы» очереди, даже если следующие уже проверены.
- Брак (PENALTY) печати не требует, но идёт через ту же очередь и те же отметки: если «Брак»
  обгонит отметку того же заказа, на сервере пропадёт штраф (EmployeeWorkProcessService).
- Отметка только после успешной печати: начисление — за напечатанную наклейку.
- Отметки в отдельном потоке: долгий ответ сервера на отметку не держит печать.

Модуль без Qt и сети: проверка, сборка PDF, склейка, печать и отметка передаются функциями —
так очередь проверяется юнит-тестами. on_message/on_state вызываются из рабочих потоков:
в Qt передавайте сигнал (emit), а не методы виджетов.
"""
import os
import queue
import tempfile
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

EARNING = "EARNING"
PENALTY = "PENALTY"


@dataclass
class PrintJob:
    seq: int
    order: str
    op: str
    sid: object = None      # номер скана в отладочном логе (diag)
    future: object = None   # Future[(ok, message)] — результат проверки заказа


def _noop(*args, **kwargs):
    return None


class PrintPipeline:
    def __init__(self, validate_fn, build_pdf_fn, merge_fn, print_fn, mark_fn, on_message,
                 on_state=None, batch_max=5, validate_workers=3, dedupe=True,
                 cleanup_fn=None, tmp_dir=None, log=None):
        """
        validate_fn(order) -> (ok, message)        проверка заказа на сервере
        build_pdf_fn(order, path)                  PDF одной наклейки
        merge_fn(paths, out_path)                  склейка PDF в одну пачку
        print_fn(pdf_path) -> bool                 печать (True — задание принято)
        mark_fn(order, op) -> (ok, message)        отметка работы
        on_message(text), on_state(dict)           сообщения и счётчики для окна
        cleanup_fn(pdf_path)                       удалить побочные файлы печати (копия 58×40)
        log(tag, **fields)                         отладочный лог (diag.log)
        """
        self._validate_fn = validate_fn
        self._build_pdf_fn = build_pdf_fn
        self._merge_fn = merge_fn
        self._print_fn = print_fn
        self._mark_fn = mark_fn
        self._on_message = on_message or _noop
        self._on_state = on_state or _noop
        self._batch_max = max(1, int(batch_max))
        self._dedupe = dedupe
        self._cleanup_fn = cleanup_fn or _noop
        self._tmp_dir = tmp_dir
        raw_log = log or _noop

        def safe_log(*args, **kwargs):  # отладочный лог не должен ронять очередь
            try:
                raw_log(*args, **kwargs)
            except Exception:
                pass

        self._log = safe_log

        self._cv = threading.Condition()
        self._jobs = deque()        # ждут печати (проверяются или проверены), по порядку seq
        self._printing = []         # в работе у потока печати: пачка, брак или проваленная проверка
        self._marks = queue.Queue()
        self._marks_pending = 0     # в очереди отметок + отправляется сейчас
        self._mark_inflight = None  # отметка, которая отправляется сейчас
        self._mark_failures = []    # [(seq, заказ, операция, сообщение)] — для итога при закрытии
        self._seq = 0
        self._accepting = True
        self._stopped = False

        self._executor = ThreadPoolExecutor(max_workers=validate_workers, thread_name_prefix="pila-validate")
        self._print_thread = threading.Thread(target=self._print_loop, name="pila-print", daemon=True)
        self._mark_thread = threading.Thread(target=self._mark_loop, name="pila-mark", daemon=True)
        self._print_thread.start()
        self._mark_thread.start()

    # ------------------------------------------------------------------ приём сканов

    def submit(self, order, op, sid=None):
        """Скан. Возвращает номер в очереди или None, если скан не принят."""
        with self._cv:
            if not self._accepting:
                self._say(f"⛔ {order}: программа закрывается — скан не принят")
                return None
            if self._dedupe and any(j.order == order and j.op == op
                                    for j in list(self._jobs) + self._printing):
                self._say(f"↩️ {order}: уже в очереди — повторный скан пропущен")
                self._log("Q_DEDUPE", sid=sid, order=order, op=op)
                return None
            self._seq += 1
            job = PrintJob(self._seq, order, op, sid=sid)
            self._jobs.append(job)
            ahead = len(self._jobs) + len(self._printing) - 1
        job.future = self._executor.submit(self._validate, job)
        job.future.add_done_callback(lambda _f: self._wake())
        what = "брак" if op == PENALTY else "печать"
        self._message(job, f"принят ({what})" + (f", впереди {ahead}" if ahead else ""))
        self._log("Q_SUBMIT", sid=sid, seq=job.seq, order=order, op=op, ahead=ahead)
        self._state()
        return job.seq

    def _validate(self, job):
        t0 = time.perf_counter()
        try:
            ok, message = self._validate_fn(job.order)
        except Exception as e:
            ok, message = False, f"Ошибка проверки: {e}"
        self._log("Q_VALIDATED", sid=job.sid, seq=job.seq, order=job.order, ok=int(bool(ok)),
                  ms=int((time.perf_counter() - t0) * 1000), msg=message)
        return bool(ok), message

    def _wake(self):
        with self._cv:
            self._cv.notify_all()

    # ------------------------------------------------------------------ печать

    def _take_next(self):
        """Ждёт, пока проверится голова очереди, и забирает работу: (вид, задания)."""
        with self._cv:
            while True:
                if self._stopped:
                    return None, None
                if self._jobs:
                    head = self._jobs[0]
                    if head.future is not None and head.future.done():
                        break
                self._cv.wait(timeout=0.5)
            ok, _ = head.future.result()
            # Всё, что поток печати забрал из очереди, до конца обработки лежит в _printing:
            # иначе в этом окне is_busy()/pending() пусты, и закрытие окна потеряло бы отметку брака.
            if not ok or head.op == PENALTY:
                self._printing = [self._jobs.popleft()]
                return ("failed" if not ok else "penalty"), list(self._printing)
            batch = []
            while self._jobs and len(batch) < self._batch_max:
                job = self._jobs[0]
                if job.op != EARNING or job.future is None or not job.future.done():
                    break
                if not job.future.result()[0]:
                    break  # проваленная проверка — своей итерацией, со своим сообщением
                batch.append(self._jobs.popleft())
            self._printing = batch
            return "print", batch

    def _print_loop(self):
        while True:
            kind, jobs = self._take_next()
            if kind is None:
                return
            self._state()  # пачка ушла из очереди в печать — обновить счётчики в окне
            try:
                if kind == "failed":
                    job = jobs[0]
                    self._message(job, f"❌ проверка не пройдена: {job.future.result()[1]}")
                elif kind == "penalty":
                    job = jobs[0]
                    self._enqueue_mark(job)  # отметку — до сообщения: сообщение может упасть
                    self._message(job, "⚠️ брак — печать не нужна, отметка в очереди")
                else:
                    self._print_batch(jobs)
            except Exception as e:  # поток печати не должен умирать
                self._say(f"❌ Ошибка очереди печати: {e}")
                self._log("Q_ERROR", exc=type(e).__name__, err=e)
            finally:
                with self._cv:
                    self._printing = []
                    self._cv.notify_all()
                self._state()

    def _new_tmp(self, prefix):
        fd, path = tempfile.mkstemp(prefix=prefix, suffix=".pdf", dir=self._tmp_dir)
        os.close(fd)
        return path

    def _print_batch(self, batch):
        seqs = ", ".join(str(j.seq) for j in batch)
        orders = ", ".join(j.order for j in batch)
        paths, merged, ok, error = [], None, False, None
        t0 = time.perf_counter()
        try:
            for job in batch:
                path = self._new_tmp(f"pila_{job.seq}_")
                paths.append(path)
                self._build_pdf_fn(job.order, path)
            if len(paths) == 1:
                merged = paths[0]
            else:
                merged = self._new_tmp("pila_batch_")
                self._merge_fn(paths, merged)
            self._log("Q_PRINT", seqs=seqs, orders=orders, count=len(batch))
            ok = bool(self._print_fn(merged))
        except Exception as e:
            ok, error = False, e
        finally:
            for path in set(paths + ([merged] if merged else [])):
                try:
                    self._cleanup_fn(path)
                except Exception:
                    pass
                try:
                    os.remove(path)
                except OSError:
                    pass
        self._log("Q_PRINTED", seqs=seqs, ok=int(ok), ms=int((time.perf_counter() - t0) * 1000),
                  err=error)
        if ok:
            for job in batch:  # отметки — до сообщений: сообщение может упасть, отметка — нет
                self._enqueue_mark(job)
            if len(batch) == 1:
                self._message(batch[0], "🖨️ напечатано")
            else:
                self._say(f"🖨️ [{seqs}] напечатано одной пачкой: {orders}")
        else:
            reason = f"ошибка: {error}" if error else "принтер не найден или недоступен"
            self._say(f"⚠️ [{seqs}] НЕ напечатано ({reason}): {orders} — отсканируйте ещё раз")

    # ------------------------------------------------------------------ отметки

    def _enqueue_mark(self, job):
        with self._cv:
            self._marks_pending += 1
        self._marks.put(job)

    def _mark_loop(self):
        while True:
            job = self._marks.get()
            if job is None:
                return
            with self._cv:
                self._mark_inflight = job
            t0 = time.perf_counter()
            try:
                ok, message = self._mark_fn(job.order, job.op)
            except Exception as e:
                ok, message = False, f"Ошибка отправки данных: {e}"
            try:
                self._log("Q_MARK", sid=job.sid, seq=job.seq, order=job.order, op=job.op, ok=int(bool(ok)),
                          ms=int((time.perf_counter() - t0) * 1000), msg=message)
            except Exception:
                pass
            with self._cv:
                if not ok:
                    self._mark_failures.append((job.seq, job.order, job.op, str(message)))
                self._mark_inflight = None
                self._marks_pending -= 1
                self._cv.notify_all()
            self._message(job, ("✅ " if ok else "❌ ") + str(message))
            self._state()

    # ------------------------------------------------------------------ состояние и закрытие

    def _say(self, text):
        """Сообщение в окно. Сбой колбэка не должен ронять потоки очереди."""
        try:
            self._on_message(text)
        except Exception:
            pass

    def _message(self, job, text):
        self._say(f"[{job.seq}] {job.order}: {text}")

    def counts(self):
        with self._cv:
            return {"queued": len(self._jobs), "printing": len(self._printing),
                    "marks": self._marks_pending}

    def _state(self):
        try:
            self._on_state(self.counts())
        except Exception:
            pass

    def mark_failures(self):
        """Отметки, которые не прошли: [(seq, заказ, операция, сообщение)]."""
        with self._cv:
            return list(self._mark_failures)

    def is_busy(self):
        c = self.counts()
        return bool(c["queued"] or c["printing"] or c["marks"])

    def stop_accepting(self):
        with self._cv:
            self._accepting = False

    def pending(self):
        """Незавершённые: [(seq, order, где)]."""
        with self._cv:
            result = [(j.seq, j.order, "ждёт печати") for j in self._jobs]
            result += [(j.seq, j.order, "печатается") for j in self._printing]
            if self._mark_inflight is not None:
                job = self._mark_inflight
                result.append((job.seq, job.order, "отправляется отметка"))
            result += [(j.seq, j.order, "ждёт отметки") for j in list(self._marks.queue) if j is not None]
            return result

    def drain(self, timeout):
        """Не принимать новые сканы и дождаться печати и отметок. Возвращает незавершённые."""
        self.stop_accepting()
        deadline = time.monotonic() + timeout
        with self._cv:
            while (self._jobs or self._printing or self._marks_pending) and time.monotonic() < deadline:
                self._cv.wait(timeout=min(0.2, max(0.0, deadline - time.monotonic())))
        return self.pending()

    def shutdown(self):
        """Остановить потоки (после drain). Незавершённые задания отбрасываются."""
        with self._cv:
            self._accepting = False
            self._stopped = True
            self._cv.notify_all()
        self._marks.put(None)
        self._executor.shutdown(wait=False, cancel_futures=True)
