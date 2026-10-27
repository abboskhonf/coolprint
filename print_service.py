"""
print_service.py — Драйвер принтера (v8.0)
==========================================
Отвечает ТОЛЬКО за отправку PDF-файла на принтер.
Никакой работы с PDF-контентом — только subprocess.

Windows: PDFtoPrinter.exe в папке приложения; формат бумаги и дуплекс выбираются
         логическим принтером (config.printer_queue).
Linux/macOS: стандартный CUPS (lp).
"""

import asyncio
import logging
import platform
import subprocess
import time
from pathlib import Path
from typing import Optional

import config

log = logging.getLogger(__name__)

DUPLEX_CUPS = {"long": "two-sided-long-edge",
               "short": "two-sided-short-edge",
               "none":  "one-sided"}


def _find_pdftoprinter() -> Path:
    for base in (config.APP_DIR, Path(__file__).resolve().parent):
        p = base / "PDFtoPrinter.exe"
        if p.exists():
            return p
    raise FileNotFoundError("PDFtoPrinter.exe не найден рядом с программой.")


async def _run_cmd(cmd: list[str], timeout: float) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise


def resolve_windows_queue(printer_name_base: str, duplex: str, paper: str) -> str:
    """Логический принтер для (формат, дуплекс); без настройки — базовое имя."""
    return config.printer_queue(paper, duplex) or printer_name_base


_printers_cache: dict = {"ts": 0.0, "names": None}


def installed_printers(ttl: float = 30.0) -> Optional[set[str]]:
    """Имена принтеров Windows (машинные). None — если узнать не удалось (не блокируем печать)."""
    if platform.system() != "Windows":
        return None
    if _printers_cache["names"] is not None and time.monotonic() - _printers_cache["ts"] < ttl:
        return _printers_cache["names"]
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", "Get-Printer | ForEach-Object Name"],
                             capture_output=True, text=True, timeout=20,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        names = {ln.strip() for ln in out.stdout.splitlines() if ln.strip()}
    except Exception as e:
        log.warning(f"Не удалось получить список принтеров Windows: {e}")
        return None
    if not names:
        return None
    _printers_cache.update(ts=time.monotonic(), names=names)
    return names


def check_queue(printer_name_base: str, duplex: str, paper: str) -> Optional[str]:
    """Текст проблемы, если логического принтера для (формат, дуплекс) нет в Windows; иначе None."""
    names = installed_printers()
    if names is None:
        return None
    target = resolve_windows_queue(printer_name_base, duplex, paper)
    if target.lower() in {n.lower() for n in names}:
        return None
    return (f"Принтер Windows «{target}» не найден (формат {paper}, дуплекс {duplex}). "
            f"Создайте его для всей системы или поправьте имя в config.ini [paper]/[printer]. "
            f"Сейчас установлены: {', '.join(sorted(names))}")


async def _send_windows(file_path: Path, printer_name_base: str, duplex: str,
                        copies: int, paper: str) -> None:
    pdf_printer = _find_pdftoprinter()
    target_printer = resolve_windows_queue(printer_name_base, duplex, paper)
    problem = check_queue(printer_name_base, duplex, paper)
    if problem:
        raise FileNotFoundError(problem)        # повторы бессмысленны: принтера нет

    # Отправляем чистый файл: драйвер Canon UFR II сам применит формат и переплёт,
    # которые заданы в настройках логического принтера.
    cmd = [str(pdf_printer), str(file_path), target_printer]
    log.info(f"Отправка на логический принтер [{target_printer}]: {file_path.name}")

    rc, out, err = await _run_cmd(cmd, timeout=120)
    if rc != 0:
        raise RuntimeError(f"PDFtoPrinter rc={rc}: {(err or out).strip()}")


async def _send_unix(file_path: Path, printer_name: str, duplex: str,
                     copies: int, paper: str) -> None:
    """Отправка через CUPS. copies=1 — копии уже в PDF."""
    sides = DUPLEX_CUPS.get(duplex, "one-sided")
    cmd   = ["lp", "-d", printer_name, "-n", "1",
             "-o", f"sides={sides}", "-o", f"media={paper}", str(file_path)]
    log.debug(f"CMD: {' '.join(cmd)}")
    rc, _, err = await _run_cmd(cmd, timeout=300)
    if rc != 0:
        raise RuntimeError(f"lp rc={rc}: {err.strip()}")


async def send_to_printer(
    file_path:       Path,
    printer_name:    str,
    duplex:          str,
    copies:          int,                           # для совместимости, не используется
    retries:         int                  = 3,
    retry_delay_sec: float                = 60.0,
    stop_event:      Optional[asyncio.Event] = None,
    paper:           str                  = "A4",
) -> bool:
    """
    Отправляет один PDF-чанк на принтер с retry.
    copies игнорируется — они размножены в PDF при нарезке.
    Возвращает True при успехе.
    """
    send_fn = _send_windows if platform.system() == "Windows" else _send_unix

    for attempt in range(1, retries + 1):
        try:
            await send_fn(file_path, printer_name, duplex, copies, paper)
            log.info(f"  Задание принято спулером (попытка {attempt}/{retries})")
            return True
        except FileNotFoundError as e:
            log.error(str(e))
            return False
        except asyncio.TimeoutError:
            log.warning(f"  Таймаут спулера ({attempt}/{retries})")
        except Exception as e:
            log.warning(f"  Ошибка ({attempt}/{retries}): {e}")

        if attempt < retries:
            log.info(f"  Жду {retry_delay_sec:.0f}с...")
            if stop_event:
                try:
                    await asyncio.wait_for(
                        asyncio.shield(stop_event.wait()),
                        timeout=retry_delay_sec,
                    )
                    return False
                except asyncio.TimeoutError:
                    pass
            else:
                await asyncio.sleep(retry_delay_sec)

            if stop_event and stop_event.is_set():
                return False

    return False


async def purge_queues(printer_name_base: str) -> int:
    """Сбрасывает задания в спулере (кнопка «Отмена»). Возвращает число очередей, которые удалось очистить."""
    if platform.system() == "Windows":
        names = {config.printer_queue(p, d) for p in config.PAPER_SIZES for d in ("long", "short", "none")}
        names.discard(None)
        names = names or {printer_name_base}
        ok = 0
        for n in names:
            ps = (f"Get-PrintJob -PrinterName '{n}' -ErrorAction SilentlyContinue | "
                  f"Remove-PrintJob -ErrorAction SilentlyContinue")
            try:
                rc, _, _ = await _run_cmd(["powershell", "-NoProfile", "-Command", ps], timeout=30)
                ok += rc == 0
            except Exception as e:
                log.warning(f"Не удалось очистить очередь {n}: {e}")
        return ok
    try:
        rc, _, _ = await _run_cmd(["cancel", "-a", printer_name_base], timeout=15)
        return int(rc == 0)
    except Exception:
        return 0
