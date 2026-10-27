"""
printer_backend.py — то, с чем разговаривает движок: реальный принтер или симулятор.

Симулятор нужен для тестов и для отладки интерфейса на машине без Canon
(config.ini: [printer] simulate = true  или  COOLPRINT_SIMULATE=1).
"""
import asyncio
import logging
from pathlib import Path
from typing import Optional

import config
import print_service
import snmp_manager
from snmp_manager import PrinterState, PrinterStatus

log = logging.getLogger(__name__)


class PrinterBackend:
    name = "base"

    async def state(self) -> PrinterState:
        raise NotImplementedError

    async def send(self, path: Path, duplex: str, paper: str,
                   stop: Optional[asyncio.Event] = None) -> bool:
        raise NotImplementedError

    async def purge(self) -> None:
        """Сбросить очередь печати принтера/спулера."""


class RealPrinter(PrinterBackend):
    name = "canon"

    def __init__(self) -> None:
        self.ip = config.PRINTER_IP

    async def state(self) -> PrinterState:
        return await snmp_manager.get_printer_state(self.ip, config.SNMP_CFG)

    async def send(self, path, duplex, paper, stop=None) -> bool:
        return await print_service.send_to_printer(
            file_path=path, printer_name=config.PRINTER_NAME, duplex=duplex,
            copies=1, stop_event=stop, paper=paper)

    async def purge(self) -> None:
        await print_service.purge_queues(config.PRINTER_NAME)


class SimulatedPrinter(PrinterBackend):
    """
    Виртуальный Canon: принимает PDF, «печатает» их со скоростью `speed` стр/с,
    ведёт счётчик как настоящий (A3 может считаться за несколько кликов).
    Поля fail_next_send / drop_next / status_override позволяют воспроизводить сбои.
    """
    name = "simulator"

    def __init__(self, speed: float = 40.0, start_count: int = 1000,
                 a3_weight: int = 1) -> None:
        self.speed = speed
        self.counter = start_count
        self.a3_weight = a3_weight
        self.sent: list[tuple[str, str, str, int]] = []   # (имя файла, дуплекс, формат, страниц)
        self.fail_next_send = 0          # сколько ближайших send() вернут False
        self.drop_next = 0               # сколько ближайших заданий «потеряется» (счётчик не вырастет)
        self.status_override: Optional[PrinterStatus] = None
        self._printing = 0.0             # сколько кликов осталось напечатать
        self._lock = asyncio.Lock()
        self._worker: Optional[asyncio.Task] = None

    async def state(self) -> PrinterState:
        if self.status_override is not None:
            return PrinterState(self.status_override, self.counter,
                                "simulated" if self.status_override != PrinterStatus.IDLE else "")
        status = PrinterStatus.PRINTING if self._printing > 0 else PrinterStatus.IDLE
        return PrinterState(status, self.counter)

    async def send(self, path, duplex, paper, stop=None) -> bool:
        if self.fail_next_send > 0:
            self.fail_next_send -= 1
            return False
        from pdf_processor import expected_pages_for_batch
        pages = expected_pages_for_batch(Path(path))
        weight = self.a3_weight if paper == "A3" else 1
        self.sent.append((Path(path).name, duplex, paper, pages))
        if self.drop_next > 0:
            self.drop_next -= 1
            return True
        self._printing += pages * weight
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run())
        return True

    async def _run(self) -> None:
        tick = 0.05
        while self._printing > 0:
            if self.status_override in (PrinterStatus.ERROR, PrinterStatus.OFFLINE, PrinterStatus.WARNING):
                await asyncio.sleep(tick)       # застряли: счётчик не растёт
                continue
            step = min(self._printing, self.speed * tick)
            self._printing -= step
            self._frac = getattr(self, "_frac", 0.0) + step
            whole = int(self._frac)
            self.counter += whole
            self._frac -= whole
            await asyncio.sleep(tick)
        self.counter += round(getattr(self, "_frac", 0.0))
        self._frac = 0.0

    async def purge(self) -> None:
        self._printing = 0.0


def make_backend() -> PrinterBackend:
    return SimulatedPrinter(a3_weight=config.paper_counter_weight("A3")) if config.SIMULATE else RealPrinter()
