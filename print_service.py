"""
print_service.py — Драйвер принтера (v7.1)
==========================================
Отвечает ТОЛЬКО за отправку PDF-файла на принтер.
Никакой работы с PDF-контентом — только subprocess.

Windows: SumatraPDF.exe рядом со скриптом.
Linux/macOS: стандартный CUPS (lp).
"""

import asyncio
import logging
import platform
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

DUPLEX_CUPS    = {"long": "two-sided-long-edge",
                  "short": "two-sided-short-edge",
                  "none":  "one-sided"}
DUPLEX_SUMATRA = {"long": "duplex", "short": "duplexshort", "none": None}


def _find_sumatra() -> Optional[Path]:
    candidates = [
        Path(__file__).parent / "SumatraPDF.exe",
        Path("SumatraPDF.exe"),
        Path(r"C:\Program Files\SumatraPDF\SumatraPDF.exe"),
        Path(r"C:\Program Files (x86)\SumatraPDF\SumatraPDF.exe"),
    ]
    return next((p for p in candidates if p.exists()), None)


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

async def _send_windows(file_path: Path, printer_name_base: str, duplex: str, copies: int) -> None:
    # PDFtoPrinter идеален для передачи в драйвер, когда мы не трогаем настройки
    pdf_printer = Path("PDFtoPrinter.exe")
    if not pdf_printer.exists(): 
        pdf_printer = Path(__file__).parent / "PDFtoPrinter.exe"
        if not pdf_printer.exists():
            raise FileNotFoundError("PDFtoPrinter.exe не найден.")
    
    # 1. Читаем конфиг, чтобы выбрать правильный логический принтер
    import configparser
    cfg = configparser.ConfigParser()
    cfg.read("config.ini", encoding="utf-8")
    
    # Маршрутизируем задание в зависимости от режима дуплекса
    if duplex == "long":
        target_printer = cfg.get("printer", "name_long", fallback=printer_name_base)
    elif duplex == "short":
        target_printer = cfg.get("printer", "name_short", fallback=printer_name_base)
    else:
        target_printer = cfg.get("printer", "name_none", fallback=printer_name_base)

    # 2. Отправляем ЧИСТЫЙ файл без CLI-костылей
    # Драйвер Canon UFR II сам применит А4 и нужный переплет, 
    # а также сожмет файл до ~1 МБ
    cmd = [
        str(pdf_printer), 
        str(file_path), 
        target_printer
    ]
    
    log.info(f"Отправка на логический принтер [{target_printer}]: {file_path.name}")
    
    rc, out, err = await _run_cmd(cmd, timeout=120)
    
    if rc != 0: 
        raise RuntimeError(f"PDFtoPrinter rc={rc}: {err}")

async def _send_unix(file_path: Path, printer_name: str, duplex: str, copies: int) -> None:
    """Отправка через CUPS. copies=1 — копии уже в PDF."""
    sides = DUPLEX_CUPS.get(duplex, "one-sided")
    cmd   = ["lp", "-d", printer_name, "-n", "1",
             "-o", f"sides={sides}", "-o", "media=A4", str(file_path)]
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
) -> bool:
    """
    Отправляет один PDF-чанк на принтер с retry.
    copies игнорируется — они размножены в PDF при нарезке.
    Возвращает True при успехе.
    """
    send_fn = _send_windows if platform.system() == "Windows" else _send_unix

    for attempt in range(1, retries + 1):
        try:
            await send_fn(file_path, printer_name, duplex, copies)
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
