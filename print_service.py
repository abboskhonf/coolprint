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


async def _send_windows(file_path: Path, printer_name: str, duplex: str, copies: int) -> None:
    # Ищем Ghostscript (gswin64c.exe)
    gs_exe = Path("gswin64c.exe")
    if not gs_exe.exists():
        gs_exe = Path(__file__).parent / "gswin64c.exe"
        
    # Если не положили рядом, ищем в стандартных путях Windows
    if not gs_exe.exists():
        import glob
        # Ищет любую установленную версию Ghostscript
        gs_paths = glob.glob(r"C:\Program Files\gs\gs*\bin\gswin64c.exe")
        if gs_paths:
            gs_exe = Path(gs_paths[-1])  # Берем самую свежую версию
        else:
            raise FileNotFoundError(
                "Ghostscript не найден. Установите с ghostscript.com "
                "или положите gswin64c.exe и gsdll64.dll рядом со скриптом."
            )

    # ── КОМАНДА GHOSTSCRIPT ДЛЯ ИДЕАЛЬНОЙ ПЕЧАТИ ──
    cmd = [
        str(gs_exe),
        "-dPrinted",         # Режим печати (сохраняет оригинальные размеры)
        "-dBATCH",           # Закрыть GS после завершения
        "-dNOPAUSE",         # Не ждать нажатия клавиш между страницами
        "-dNOSAFER",         # Разрешить чтение файлов
        "-dNoCancel",        # Скрыть назойливое окно отмены Windows
        "-q",                # Тихий режим (без лишних логов в консоль)
        "-sPAPERSIZE=a4",    # 🔻 ЖЕСТКО ЗАДАЕМ А4 🔻
        "-sDEVICE=mswinpr2", # Устройство вывода: Windows Spooler
        f"-sOutputFile=%printer%{printer_name}" # Целевой принтер
    ]
    
    # 🔻 ЖЕСТКО ПЕРЕДАЕМ ДУПЛЕКС СПУЛЕРУ WINDOWS 🔻
    if duplex == "long":
        cmd.extend(["-dDuplex=true", "-dTumble=false"])
    elif duplex == "short":
        cmd.extend(["-dDuplex=true", "-dTumble=true"])
    else:
        cmd.extend(["-dDuplex=false"])
        
    cmd.append(str(file_path))
    
    log.info(f"Ghostscript отправляет вектор (A4): {file_path.name}")
    
    # Таймаут: Вектор через GS переваривается и улетает в спулер за пару секунд
    rc, out, err = await _run_cmd(cmd, timeout=120)
    
    if rc != 0: 
        raise RuntimeError(f"Ghostscript rc={rc}: {err}")


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
