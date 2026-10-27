"""
engine.py — движок печати CoolPrint.

Берёт первое готовое задание из очереди и печатает его по пачкам. Между пачками
действует ОБЩИЙ для всех заданий интервал охлаждения (cooldown): он защищает
фьюзер принтера, поэтому не сбрасывается при смене задания и переживает
перезапуск службы.

Решение «что печатать дальше» принимается на границе каждой пачки. Благодаря
этому перестановка очереди, пауза и отмена работают и во время печати.
"""
import asyncio
import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Optional

import config
import pdf_processor
from jobs import EventHub, Job, JobQueue, JobStatus, QueueError
from printer_backend import PrinterBackend
from snmp_manager import PrinterState, PrinterStatus
from state_manager import AuditLog

log = logging.getLogger(__name__)

PROBLEM = (PrinterStatus.ERROR, PrinterStatus.WARNING, PrinterStatus.OFFLINE)
READY   = (PrinterStatus.IDLE, PrinterStatus.SLEEP)
CANON_PPM = 25                       # паспортная скорость iR2425, для оценки времени


class Engine:
    def __init__(self, queue: JobQueue, backend: PrinterBackend, *,
                 poll_sec: float = 3.0, idle_poll_sec: float = 15.0,
                 lost_job_sec: Optional[float] = None, watchdog_sec: Optional[float] = None,
                 minute: float = 60.0, work_dir: Optional[Path] = None,
                 state_file: Optional[Path] = None) -> None:
        self.queue, self.hub, self.backend = queue, queue.hub, backend
        self.poll_sec = poll_sec
        self.idle_poll_sec = idle_poll_sec
        self.lost_job_sec = lost_job_sec if lost_job_sec is not None else config.LOST_JOB_MIN * 60
        self.watchdog_sec = watchdog_sec if watchdog_sec is not None else config.WATCHDOG_MIN * 60
        self.minute = minute                         # секунд в «минуте» cooldown (в тестах << 60)
        self.work_dir = Path(work_dir or config.WORK_DIR)
        self.state_file = Path(state_file or config.DATA_DIR / "engine_state.json")

        self.phase = "idle"
        self.current: Optional[Job] = None
        self.printer = PrinterState(PrinterStatus.UNKNOWN, 0)
        self.printer_ts = 0.0
        self.batch = {"index": 0, "total": 0, "printed": 0, "expected": 0}
        self.cooldown_until = 0.0                    # wall-time, переживает рестарт
        self.cooldown_total = 0.0
        self._batches: dict[str, list[list[Path]]] = {}
        self._wake = asyncio.Event()
        self._stopping = False
        self._tasks: list[asyncio.Task] = []
        self._avg_batch_sec: dict[str, float] = {}
        self._load_state()

    # ── Состояние, переживающее рестарт ─────────────────────────────────────
    def _load_state(self) -> None:
        try:
            d = json.loads(self.state_file.read_text(encoding="utf-8"))
            self.cooldown_until = float(d.get("cooldown_until", 0))
            self.cooldown_total = float(d.get("cooldown_total", 0))
        except (OSError, ValueError):
            pass

    def _save_state(self) -> None:
        try:
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps({"cooldown_until": self.cooldown_until,
                                       "cooldown_total": self.cooldown_total}), encoding="utf-8")
            os.replace(tmp, self.state_file)
        except OSError as e:
            log.warning(f"Не удалось сохранить состояние движка: {e}")

    # ── Жизненный цикл ──────────────────────────────────────────────────────
    async def start(self) -> None:
        self._tasks = [asyncio.create_task(self._main(), name="engine-main"),
                       asyncio.create_task(self._listen(), name="engine-listen"),
                       asyncio.create_task(self._monitor(), name="engine-monitor")]

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []

    def kick(self) -> None:
        self._wake.set()

    async def _nap(self, sec: float) -> None:
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=max(0.01, sec))
        except asyncio.TimeoutError:
            pass
        self._wake.clear()

    async def _listen(self) -> None:
        """Любое изменение очереди будит движок, чтобы он не ждал конца sleep."""
        q = self.hub.subscribe()
        try:
            while True:
                ev = await q.get()
                if ev["kind"] in ("queue", "paused"):
                    self._wake.set()
        finally:
            self.hub.unsubscribe(q)

    async def _monitor(self) -> None:
        """Опрашивает принтер в фоне, чтобы панель показывала статус и когда ничего не печатается."""
        while True:
            if self.phase in ("idle", "paused", "cooling", "preparing"):
                await self._poll_printer()
            await asyncio.sleep(self.idle_poll_sec)

    async def _poll_printer(self) -> PrinterState:
        try:
            st = await self.backend.state()
        except Exception as e:
            st = PrinterState(PrinterStatus.OFFLINE, self.printer.page_count, str(e))
        if st.status != self.printer.status:
            self.hub.publish("printer", status=st.status.value, error=st.error_msg,
                             page_count=st.page_count)
        self.printer, self.printer_ts = st, time.time()
        return st

    # ── Главный цикл ────────────────────────────────────────────────────────
    async def _main(self) -> None:
        last_prune = 0.0
        while not self._stopping:
            if time.time() - last_prune > 3600:
                last_prune = time.time()
                self._prune()
            job = self.queue.next_runnable()
            if job is None:
                self.phase = "paused" if (self.queue.paused and self.queue.active()) else "idle"
                self.current = None
                await self._nap(5.0)
                continue
            try:
                await self._run_job(job)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("Сбой при печати задания")
                self._fail(job, f"Внутренняя ошибка: {e}")
            finally:
                self.current = None
                self.batch = {"index": 0, "total": 0, "printed": 0, "expected": 0}

    def _prune(self) -> None:
        for j in self.queue.prune_history():
            shutil.rmtree(self.work_dir / j.id, ignore_errors=True)
            if Path(j.source).parent == config.UPLOADS_DIR:
                Path(j.source).unlink(missing_ok=True)

    # ── Задание ─────────────────────────────────────────────────────────────
    async def _run_job(self, job: Job) -> None:
        self.current = job
        first_start = job.status == JobStatus.QUEUED and not job.started
        self.queue.set_status(job.id, JobStatus.RUNNING)
        if first_start:
            self.hub.publish("job_started", job=job.to_dict())

        self.phase = "preparing"
        self.hub.publish("phase", phase=self.phase, job_id=job.id)
        try:
            batches = await self._prepare(job)
        except Exception as e:
            self._fail(job, f"Ошибка подготовки файла: {e}")
            return
        if job.batches_total != len(batches):
            job.batches_total = len(batches)
            self.queue.touch(job)

        while job.batches_done < job.batches_total:
            if await self._gate(job) != "go":
                if job.status == JobStatus.RUNNING:
                    self.queue.set_status(job.id, JobStatus.QUEUED)
                return
            if not await self._run_batch(job, batches[job.batches_done]):
                return

        self.queue.set_status(job.id, JobStatus.DONE)
        AuditLog.append(job.owner, job.name, job.pages_selected(), job.copies, job.batches_done,
                        job.batches_total, "ALL_DONE", 0, self.printer.page_count,
                        duration_min=int((time.time() - job.started) / 60),
                        notes=f"{job.paper}, duplex={job.duplex}")
        self.hub.publish("job_done", job=job.to_dict())
        self._forget(job)

    def _forget(self, job: Job) -> None:
        self._batches.pop(job.id, None)
        shutil.rmtree(self.work_dir / job.id, ignore_errors=True)

    def _fail(self, job: Job, msg: str) -> None:
        log.error(f"Задание {job.id} ({job.name}): {msg}")
        job.in_flight = False
        self.queue.set_status(job.id, JobStatus.FAILED, error=msg)
        AuditLog.append(job.owner, job.name, job.pages_selected(), job.copies, job.batches_done,
                        job.batches_total, "FAILED", 0, self.printer.page_count, notes=msg)
        self.hub.publish("job_failed", job=job.to_dict(), error=msg)
        if config.ON_FAILURE == "halt":
            self.queue.set_paused(True)
            self.hub.publish("halted", reason=f"Очередь остановлена после сбоя: {job.name}")

    # ── Подготовка PDF ──────────────────────────────────────────────────────
    def _signature(self, job: Job) -> dict:
        return {"batch_size": job.batch_size, "copies": job.copies, "duplex": job.duplex,
                "paper": job.paper, "booklet": job.booklet, "page_from": job.page_from,
                "page_to": job.page_to, "fit": config.FIT_TO_PAPER, "src": job.source}

    async def _prepare(self, job: Job) -> list[list[Path]]:
        cached = self._batches.get(job.id)
        if cached:
            return cached
        wdir = self.work_dir / job.id
        sig_file = wdir / "sig.json"
        sig = self._signature(job)
        try:
            same = json.loads(sig_file.read_text(encoding="utf-8")) == sig
        except (OSError, ValueError):
            same = False
        if not same:
            if job.batches_done:
                raise RuntimeError("Параметры задания изменились после начала печати")
            shutil.rmtree(wdir, ignore_errors=True)
        wdir.mkdir(parents=True, exist_ok=True)

        source = job.source
        if job.page_from != 1 or job.pages_selected() != job.total_pages:
            source = str(wdir / "range.pdf")
            if not Path(source).exists():
                await asyncio.to_thread(self._slice_range, job, source)

        chunk = max(8, job.batch_size // max(1, config.CHUNKS_PER_BATCH))
        batches = await pdf_processor.split_pdf_into_batches(
            pdf_path=source, batch_size=job.batch_size, chunk_size=chunk,
            duplex=job.duplex, copies=job.copies, batches_dir=wdir / "batches",
            booklet_mode=job.booklet, paper=job.paper, fit=config.FIT_TO_PAPER)
        if not batches:
            raise RuntimeError("В файле нет страниц для печати")
        sig_file.write_text(json.dumps(sig), encoding="utf-8")
        self._batches[job.id] = batches
        return batches

    @staticmethod
    def _slice_range(job: Job, dest: str) -> None:
        from pypdf import PdfReader, PdfWriter
        reader, writer = PdfReader(job.source), PdfWriter()
        for i in range(job.page_from - 1, job.last_page()):
            writer.add_page(reader.pages[i])
        with open(dest, "wb") as f:
            writer.write(f)

    # ── Ворота: охлаждение + готовность принтера ────────────────────────────
    def _should_yield(self, job: Job) -> bool:
        return job.status != JobStatus.RUNNING or self.queue.next_runnable() is not job

    async def _gate(self, job: Job) -> str:
        last_pub = 0.0
        while True:
            if self._stopping or self._should_yield(job):
                return "yield"
            remaining = self.cooldown_until - time.time()
            if remaining <= 0:
                break
            self.phase = "cooling"
            if time.time() - last_pub >= (2.0 if self.minute >= 1 else 0.2):
                last_pub = time.time()
                self.hub.publish("cooldown", remaining=remaining, total=self.cooldown_total,
                                 until=self.cooldown_until, job_id=job.id)
            await self._nap(min(remaining, 1.0))

        self.phase = "waiting_printer"
        self.hub.publish("phase", phase=self.phase, job_id=job.id)
        while True:
            if self._stopping or self._should_yield(job):
                return "yield"
            st = await self._poll_printer()
            if st.status in READY:
                return "go"
            await self._nap(min(5.0, max(self.poll_sec, 0.05)))

    # ── Одна пачка ──────────────────────────────────────────────────────────
    async def _run_batch(self, job: Job, chunks: list[Path]) -> bool:
        idx = job.batches_done
        weight = config.paper_counter_weight(job.paper) if job.paper == "A3" else 1
        full_expected = sum(pdf_processor.expected_pages_for_batch(c) for c in chunks)

        skip = job.recovery_skip
        if skip:
            chunks = await asyncio.to_thread(pdf_processor.skip_pages_in_chunks, chunks, skip)
        expected_sides = sum(pdf_processor.expected_pages_for_batch(c) for c in chunks)
        expected = expected_sides * weight
        self.batch = {"index": idx + 1, "total": job.batches_total, "printed": 0,
                      "expected": expected_sides}

        if not chunks:                                   # пачка уже полностью напечатана
            return self._batch_ok(job, 0, time.time())

        t0 = time.time()
        start = await self._poll_printer()
        start_count = start.page_count

        self.phase = "sending"
        self.hub.publish("phase", phase=self.phase, job_id=job.id)
        job.in_flight = True
        self.queue.touch(job)
        for n, chunk in enumerate(chunks, 1):
            ok = await self.backend.send(chunk, job.duplex, job.paper)
            if not ok:
                await self.backend.purge()
                job.in_flight = False
                self._fail(job, f"Пачка {idx + 1}: не удалось отправить часть {n}/{len(chunks)} в спулер")
                return False
            if job.status == JobStatus.CANCELLED:
                break

        self.phase = "printing"
        self.hub.publish("phase", phase=self.phase, job_id=job.id)
        deadline = time.monotonic() + self.watchdog_sec
        idle_since: Optional[float] = None
        last_printed = -1
        last_status = None
        last_pub = 0.0

        while True:
            if self._stopping:
                return False
            st = await self._poll_printer()
            printed_raw = st.page_count - start_count
            printed = max(0, printed_raw) // weight
            self.batch["printed"] = printed

            if job.status == JobStatus.CANCELLED:        # отмена: чистим спулер, запоминаем прогресс
                await self.backend.purge()
                self._remember_progress(job, printed, full_expected, skip)
                job.in_flight = False
                self.queue.touch(job)
                self.hub.publish("job_cancelled", job=job.to_dict())
                return False

            if printed != last_printed or time.time() - last_pub > 10:
                last_printed, last_pub = printed, time.time()
                self.hub.publish("progress", job_id=job.id, batch=idx + 1,
                                 batches=job.batches_total, printed=printed,
                                 expected=expected_sides, status=st.status.value)

            if st.status in PROBLEM:                      # заморозка watchdog: ждём человека/сеть
                deadline = time.monotonic() + self.watchdog_sec
                idle_since = None
                if last_status != st.status:
                    self.hub.publish("printer_alert", job_id=job.id, status=st.status.value,
                                     error=st.error_msg, batch=idx + 1)
            elif st.status in READY:
                if printed_raw >= expected:
                    break
                idle_since = idle_since or time.monotonic()
                if time.monotonic() - idle_since > self.lost_job_sec:
                    self._remember_progress(job, printed, full_expected, skip)
                    self._fail(job, f"Пачка {idx + 1}: счётчик +{printed_raw}, ожидалось ≥{expected}. "
                                    f"Задание потеряно в сети/спулере.")
                    return False
            else:
                idle_since = None
                if last_status in PROBLEM:
                    self.hub.publish("printer_recovered", job_id=job.id, batch=idx + 1)
            last_status = st.status

            if time.monotonic() > deadline:
                self._remember_progress(job, printed, full_expected, skip)
                self._fail(job, f"Пачка {idx + 1}: watchdog — печать не завершилась за "
                                f"{self.watchdog_sec / 60:.0f} мин")
                return False
            await self._nap(self.poll_sec)

        return self._batch_ok(job, printed_raw, t0, start_count=start_count)

    def _remember_progress(self, job: Job, printed: int, full_expected: int, skip: int) -> None:
        """Сохраняет, сколько страниц текущей пачки уже вышло, чтобы повтор не печатал их заново."""
        done = skip + printed
        if job.duplex != "none" or job.booklet != "none":
            done -= done % 2                              # неполный лист допечатываем целиком
        job.recovery_skip = max(0, min(done, max(0, full_expected - 1)))

    def _batch_ok(self, job: Job, printed_raw: int, t0: float, start_count: int = 0) -> bool:
        weight = config.paper_counter_weight(job.paper) if job.paper == "A3" else 1
        job.in_flight = False
        job.batches_done += 1
        job.pages_printed += printed_raw // weight
        job.recovery_skip = 0
        self._avg_batch_sec[job.id] = time.time() - t0
        cd = job.cooldown * self.minute
        self.cooldown_total = cd
        self.cooldown_until = time.time() + cd if cd > 0 else 0.0
        self._save_state()
        self.queue.touch(job)
        AuditLog.append(job.owner, job.name, job.pages_selected(), job.copies, job.batches_done,
                        job.batches_total, "BATCH_OK", start_count, start_count + printed_raw,
                        notes=f"Пачка {job.batches_done}/{job.batches_total}, {job.paper}")
        self.hub.publish("batch_done", job=job.to_dict(), cooldown=cd)
        return True

    # ── Управление (используют веб и Telegram) ──────────────────────────────
    def skip_cooldown(self) -> None:
        self.cooldown_until = 0.0
        self._save_state()
        self.hub.publish("cooldown", remaining=0, total=self.cooldown_total, until=0, job_id=None)
        self.kick()

    def adjust_cooldown(self, minutes: float) -> None:
        """Продлить (или сократить) текущее охлаждение на `minutes`."""
        now = time.time()
        base = max(self.cooldown_until, now)
        self.cooldown_until = max(0.0, base + minutes * self.minute)
        self.cooldown_total = max(self.cooldown_total, self.cooldown_until - now)
        self._save_state()
        self.kick()

    def cancel(self, job_id: str) -> Job:
        job = self.queue.cancel(job_id)
        if self.current is not job:                       # не печатается: можно прибрать сразу
            self._forget(job)
        self.kick()
        return job

    async def emergency_stop(self) -> None:
        """Аварийная остановка: пауза очереди, отмена текущего задания, очистка спулера."""
        self.queue.set_paused(True)
        if self.current and self.current.status == JobStatus.RUNNING:
            self.queue.cancel(self.current.id)
        await self.backend.purge()
        self.kick()

    # ── Создание заданий ────────────────────────────────────────────────────
    def create_job(self, source: Path, name: str, owner: str = "web",
                   position: Optional[int] = None, **opts) -> Job:
        total = pdf_processor.get_total_pages(str(source))
        if total <= 0:
            raise QueueError("Не удалось прочитать PDF (файл повреждён или не PDF)")
        d = config.defaults()
        d.update({k: v for k, v in opts.items() if v is not None})
        paper = str(d["paper"]).upper()
        if paper not in config.supported_papers():
            raise QueueError(f"Формат {paper} не настроен на этом компьютере "
                             f"(доступно: {', '.join(config.supported_papers())})")
        if d["duplex"] not in ("long", "short", "none"):
            raise QueueError("duplex: long | short | none")
        if d["booklet"] not in ("none", "left", "right"):
            raise QueueError("booklet: none | left | right")
        page_from = max(1, int(d.get("page_from") or 1))
        page_to = min(total, int(d.get("page_to") or total))
        if page_from > page_to:
            raise QueueError(f"Неверный диапазон страниц: {page_from}–{page_to} (в файле {total})")
        job = Job(name=name, source=str(source), total_pages=total, owner=str(owner),
                  page_from=page_from, page_to=page_to if page_to != total else 0,
                  copies=max(1, min(99, int(d["copies"]))),
                  duplex="short" if d["booklet"] != "none" else d["duplex"],
                  paper=paper, booklet=d["booklet"],
                  batch_size=max(2, min(400, int(d["batch_size"]))),
                  cooldown=max(0, min(240, int(d["cooldown"]))))
        self.queue.add(job, position)
        self.kick()
        return job

    # ── Отчёты ──────────────────────────────────────────────────────────────
    def eta(self) -> dict[str, float]:
        """Оценка окончания (unix time) каждого активного задания с учётом очереди и охлаждения."""
        out: dict[str, float] = {}
        t = max(time.time(), self.cooldown_until)
        for j in self.queue.active():
            left = max(0, (j.batches_total or self._guess_batches(j)) - j.batches_done)
            per_batch = self._avg_batch_sec.get(j.id) or (j.batch_size / (CANON_PPM / 60.0))
            gap = j.cooldown * self.minute
            if left:
                t += left * per_batch + (left - 1) * gap
                out[j.id] = t
                t += gap
        return out

    @staticmethod
    def _guess_batches(j: Job) -> int:
        return max(1, -(-j.sides_total() // max(2, j.batch_size)))

    def status(self) -> dict:
        now = time.time()
        remaining = max(0.0, self.cooldown_until - now)
        eta = self.eta()
        jobs = self.queue.snapshot()
        for j in jobs:
            j["eta"] = eta.get(j["id"])
        return {
            "now": now, "phase": self.phase, "paused": self.queue.paused,
            "backend": self.backend.name,
            "printer": {"status": self.printer.status.value, "page_count": self.printer.page_count,
                        "error": self.printer.error_msg, "ts": self.printer_ts},
            "cooldown": {"remaining": remaining, "total": self.cooldown_total,
                         "until": self.cooldown_until if remaining else 0},
            "current": self.current.id if self.current else None,
            "batch": dict(self.batch),
            "jobs": jobs,
            "defaults": config.defaults(),
            "papers": config.supported_papers(),
        }
