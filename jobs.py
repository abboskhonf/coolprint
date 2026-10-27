"""
jobs.py — модель задания, персистентная очередь и шина событий.

Очередь — это упорядоченный список. Порядок = приоритет: движок всегда берёт
первое задание, готовое к печати. Перестановка действует на границе пачки,
поэтому переставлять можно и во время печати.
"""
import asyncio
import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict, fields
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


class JobStatus:
    QUEUED    = "queued"
    RUNNING   = "running"
    HELD      = "held"        # ручная пауза конкретного задания
    DONE      = "done"
    FAILED    = "failed"
    CANCELLED = "cancelled"

    ACTIVE   = (QUEUED, RUNNING, HELD)
    FINISHED = (DONE, FAILED, CANCELLED)


@dataclass
class Job:
    name:         str
    source:       str                      # путь к исходному PDF
    total_pages:  int                      # страниц в исходном PDF
    id:           str   = field(default_factory=lambda: uuid.uuid4().hex[:8])
    page_from:    int   = 1
    page_to:      int   = 0                # 0 = до конца
    copies:       int   = 1
    duplex:       str   = "long"           # long | short | none
    paper:        str   = "A4"             # A4 | A3
    booklet:      str   = "none"           # none | left | right
    batch_size:   int   = 64
    cooldown:     int   = 25               # минут между пачками
    owner:        str   = "web"            # кто добавил: "web" или chat_id
    status:       str   = JobStatus.QUEUED
    batches_total: int  = 0
    batches_done: int   = 0
    pages_printed: int  = 0                # по счётчику принтера
    recovery_skip: int  = 0                # сколько страниц текущей пачки уже напечатано
    in_flight:    bool  = False            # пачка отправлена на принтер и ещё не подтверждена
    error:        str   = ""
    created:      float = field(default_factory=time.time)
    started:      float = 0.0
    finished:     float = 0.0

    # ── Производные величины ────────────────────────────────────────────────
    def last_page(self) -> int:
        return min(self.page_to or self.total_pages, self.total_pages)

    def pages_selected(self) -> int:
        return max(0, self.last_page() - self.page_from + 1)

    def sides_total(self) -> int:
        """Сколько сторон будет напечатано (грубо; брошюра = 1 сторона на 2 страницы)."""
        n = self.pages_selected()
        if self.booklet != "none":
            n = -(-n // 4) * 2          # N страниц, добитых до кратного 4, дают N/2 ландшафтных сторон
        return n * self.copies

    def progress(self) -> float:
        if self.status == JobStatus.DONE:
            return 1.0
        return (self.batches_done / self.batches_total) if self.batches_total else 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["progress"] = round(self.progress(), 4)
        d["pages_selected"] = self.pages_selected()
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Job":
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names})


# ══════════════════════════════════════════════════════════════════════════════
# Шина событий
# ══════════════════════════════════════════════════════════════════════════════

class EventHub:
    """Простая pub/sub шина. Подписчики (SSE, Telegram) получают события в свои очереди."""

    def __init__(self) -> None:
        self._subs: set[asyncio.Queue] = set()
        self.recent: list[dict] = []          # последние события для новых подключений

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def publish(self, kind: str, **data) -> None:
        ev = {"kind": kind, "ts": time.time(), **data}
        if kind not in ("progress", "cooldown"):
            self.recent = (self.recent + [ev])[-50:]
        for q in list(self._subs):
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:           # медленный подписчик не должен тормозить движок
                try:
                    q.get_nowait()
                    q.put_nowait(ev)
                except Exception:
                    pass


# ══════════════════════════════════════════════════════════════════════════════
# Очередь
# ══════════════════════════════════════════════════════════════════════════════

class QueueError(ValueError):
    pass


class JobQueue:
    def __init__(self, path: Path, hub: Optional[EventHub] = None,
                 history_days: int = 14) -> None:
        self.path = Path(path)
        self.hub = hub or EventHub()
        self.history_days = history_days
        self._lock = threading.RLock()
        self.jobs: list[Job] = []
        self.paused = False                    # глобальная пауза очереди
        self._load()

    # ── Хранение ────────────────────────────────────────────────────────────
    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        self.paused = bool(data.get("paused", False))
        self.jobs = [Job.from_dict(j) for j in data.get("jobs", [])]
        # Служба упала/перезапущена. Задание между пачками просто возвращается в очередь.
        # Если пачка была в полёте, мы не знаем, сколько страниц успело выйти: ставим на
        # удержание, чтобы человек посмотрел на лоток и решил, а не печатал пачку повторно вслепую.
        for j in self.jobs:
            if j.status == JobStatus.RUNNING:
                if j.in_flight:
                    j.status = JobStatus.HELD
                    j.error = ("Служба перезапущена во время печати пачки. Проверьте лоток, при "
                               "необходимости укажите, сколько страниц уже напечатано, и возобновите.")
                else:
                    j.status = JobStatus.QUEUED

    def save(self) -> None:
        with self._lock:
            data = {"paused": self.paused, "jobs": [asdict(j) for j in self.jobs]}
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)

    def _changed(self, job: Optional[Job] = None) -> None:
        self.save()
        self.hub.publish("queue", job=job.to_dict() if job else None)

    # ── Чтение ──────────────────────────────────────────────────────────────
    def get(self, job_id: str) -> Job:
        with self._lock:
            for j in self.jobs:
                if j.id == job_id:
                    return j
        raise QueueError(f"Задание {job_id} не найдено")

    def snapshot(self) -> list[dict]:
        with self._lock:
            return [j.to_dict() for j in self.jobs]

    def active(self) -> list[Job]:
        with self._lock:
            return [j for j in self.jobs if j.status in JobStatus.ACTIVE]

    def next_runnable(self) -> Optional[Job]:
        """Первое задание в порядке очереди, которое можно печатать сейчас."""
        with self._lock:
            if self.paused:
                return None
            return next((j for j in self.jobs
                         if j.status in (JobStatus.QUEUED, JobStatus.RUNNING)), None)

    # ── Изменение ───────────────────────────────────────────────────────────
    def add(self, job: Job, position: Optional[int] = None) -> Job:
        with self._lock:
            if position is None or position >= len(self._active_idx()):
                self.jobs.append(job)
            else:
                self.jobs.insert(self._active_idx()[max(0, position)], job)
            self._changed(job)
        return job

    def _active_idx(self) -> list[int]:
        return [i for i, j in enumerate(self.jobs) if j.status in JobStatus.ACTIVE]

    def move(self, job_id: str, to: int) -> None:
        """Переставляет активное задание на позицию `to` среди активных (0 = печатается первым)."""
        with self._lock:
            job = self.get(job_id)
            if job.status not in JobStatus.ACTIVE:
                raise QueueError("Завершённое задание переставить нельзя")
            self.jobs.remove(job)
            idx = self._active_idx()
            to = max(0, min(to, len(idx)))
            self.jobs.insert(idx[to] if to < len(idx) else (idx[-1] + 1 if idx else 0), job)
            self._changed(job)

    def move_step(self, job_id: str, delta: int) -> None:
        with self._lock:
            order = [j.id for j in self.active()]
            if job_id not in order:
                raise QueueError("Задание не в очереди")
            self.move(job_id, order.index(job_id) + delta)

    def update(self, job_id: str, **patch) -> Job:
        """Меняет параметры задания. Формат/дуплекс/брошюру менять можно только до начала печати."""
        with self._lock:
            job = self.get(job_id)
            if job.status in JobStatus.FINISHED:
                raise QueueError("Завершённое задание изменить нельзя")
            started = job.batches_done > 0 or job.status == JobStatus.RUNNING
            locked = {"paper", "duplex", "booklet", "copies", "page_from", "page_to", "batch_size"}
            for k, v in patch.items():
                if k in locked and started and getattr(job, k) != v:
                    raise QueueError(f"«{k}» нельзя менять после начала печати")
                if k in ("batch_size", "cooldown", "copies", "paper", "duplex", "booklet",
                         "page_from", "page_to", "name", "recovery_skip"):
                    setattr(job, k, v)
            self._changed(job)
            return job

    def set_status(self, job_id: str, status: str, error: str = "") -> Job:
        with self._lock:
            job = self.get(job_id)
            job.status = status
            if error or status != JobStatus.FAILED:
                job.error = error
            if status == JobStatus.RUNNING and not job.started:
                job.started = time.time()
            if status in JobStatus.FINISHED:
                job.finished = time.time()
            self._changed(job)
            return job

    def touch(self, job: Job) -> None:
        """Сохранить изменения, внесённые движком в поля задания (прогресс)."""
        self._changed(job)

    def hold(self, job_id: str) -> Job:
        job = self.get(job_id)
        if job.status not in (JobStatus.QUEUED, JobStatus.RUNNING):
            raise QueueError("Приостановить можно только ожидающее задание")
        return self.set_status(job_id, JobStatus.HELD)

    def release(self, job_id: str) -> Job:
        job = self.get(job_id)
        if job.status != JobStatus.HELD:
            raise QueueError("Задание не на удержании")
        return self.set_status(job_id, JobStatus.QUEUED)

    def cancel(self, job_id: str) -> Job:
        job = self.get(job_id)
        if job.status in JobStatus.FINISHED:
            raise QueueError("Задание уже завершено")
        return self.set_status(job_id, JobStatus.CANCELLED)

    def retry(self, job_id: str) -> Job:
        """Вернуть упавшее/отменённое задание в хвост очереди; прогресс и пропуск страниц сохраняются."""
        with self._lock:
            job = self.get(job_id)
            if job.status not in (JobStatus.FAILED, JobStatus.CANCELLED):
                raise QueueError("Повторить можно только упавшее или отменённое задание")
            self.jobs.remove(job)
            self.jobs.append(job)
            job.status, job.error, job.finished = JobStatus.QUEUED, "", 0.0
            self._changed(job)
            return job

    def remove(self, job_id: str) -> Job:
        with self._lock:
            job = self.get(job_id)
            if job.status == JobStatus.RUNNING:
                raise QueueError("Сначала отмените печатающееся задание")
            self.jobs.remove(job)
            self._changed(None)
            return job

    def set_paused(self, paused: bool) -> None:
        with self._lock:
            self.paused = paused
            self._changed(None)
            self.hub.publish("paused", paused=paused)

    def prune_history(self) -> list[Job]:
        """Удаляет завершённые задания старше history_days; возвращает удалённые."""
        cutoff = time.time() - self.history_days * 86400
        with self._lock:
            old = [j for j in self.jobs if j.status in JobStatus.FINISHED
                   and (j.finished or j.created) < cutoff]
            if old:
                self.jobs = [j for j in self.jobs if j not in old]
                self.save()
            return old
