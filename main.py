"""
main.py — Canon iR2425 Щадящая автопечать v7.2 (CLI Version)
=======================================================
Консольная версия бота (ОФФЛАЙН - без Telegram).
Поддерживает умное восстановление, бесконечное ожидание бумаги
и запись в AuditLog.

Запуск:
  python main.py document.pdf
  python main.py document.pdf --copies 2 --duplex short
  python main.py document.pdf --auto-resume
"""

import argparse
import asyncio
import configparser
import logging
import platform
import signal
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import snmp_manager
import print_service
import pdf_processor
from state_manager import Phase, JobState, AuditLog
from snmp_manager import SNMPConfig, PrinterStatus  # <--- ДОБАВЛЕН ИМПОРТ PrinterStatus

CONFIG_FILE = "config.ini"
_stop_event: Optional[asyncio.Event] = None
log = logging.getLogger(__name__)

def load_config() -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_FILE, encoding="utf-8")
    return cfg

def build_args(cfg: configparser.ConfigParser) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Canon iR2425 CLI AutoPrint v7.2")
    parser.add_argument("--booklet", choices=["none", "left", "right"], default=cfg.get("print", "booklet", fallback="none"))
    parser.add_argument("pdf")
    parser.add_argument("--ip", default=cfg.get("printer", "ip", fallback=None))
    parser.add_argument("--printer", default=cfg.get("printer", "name", fallback=None))
    parser.add_argument("--copies", type=int, default=cfg.getint("print", "copies", fallback=1))
    parser.add_argument("--duplex", choices=["long", "short", "none"], default=cfg.get("print", "duplex", fallback="long"))
    parser.add_argument("--batch", type=int, default=cfg.getint("print", "batch_size", fallback=64))
    parser.add_argument("--chunk", type=int, default=cfg.getint("print", "chunk_size", fallback=32))
    parser.add_argument("--cooldown", type=int, default=cfg.getint("print", "cooldown_minutes", fallback=25))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--auto-resume", action="store_true")
    
    args = parser.parse_args()
    if not args.ip: parser.error("Укажите IP: --ip или config.ini [printer] ip=")
    if not args.printer: parser.error("Укажите принтер: --printer или config.ini [printer] name=")
    return args

def setup_logging(log_dir: Path, cfg: configparser.ConfigParser) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"session_{datetime.now():%Y%m%d_%H%M%S}.log"
    fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s", datefmt="%H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    root.addHandler(fh)

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    root.addHandler(ch)
    log.info(f"Лог: {log_file}")

def _setup_signals(loop: asyncio.AbstractEventLoop, stop: asyncio.Event) -> None:
    def _handler():
        log.warning("[!] Сигнал остановки. Завершаю после текущего шага...")
        stop.set()
    if platform.system() != "Windows":
        for sig in (signal.SIGINT, signal.SIGTERM): loop.add_signal_handler(sig, _handler)
    else:
        def _win_handler(sig, frame): loop.call_soon_threadsafe(_handler)
        signal.signal(signal.SIGINT, _win_handler)

def progress_bar(current: int, total: int, width: int = 20) -> str:
    if total == 0: return "░" * width
    filled = int(width * current / total)
    return "█" * filled + "░" * (width - filled)

async def _async_main() -> None:
    global _stop_event
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    _stop_event = stop
    _setup_signals(loop, stop)

    cfg = load_config()
    args = build_args(cfg)

    if not Path(args.pdf).is_file():
        print(f"❌ Файл не найден: {args.pdf}")
        sys.exit(1)

    batches_dir = pdf_processor.get_batches_dir(args.pdf)
    setup_logging(batches_dir / "logs", cfg)

    snmp_cfg = SNMPConfig(
        community=cfg.get("snmp", "community", fallback="public"),
        port=cfg.getint("snmp", "port", fallback=161),
        timeout=cfg.getfloat("snmp", "timeout", fallback=3.0),
        retries=cfg.getint("snmp", "retries", fallback=2),
    )
    wake_timeout = cfg.getint("snmp", "wake_timeout_sec", fallback=360)
    watchdog_min = cfg.getint("snmp", "print_watchdog_min", fallback=600) # Ждем всю ночь

    total_pages = pdf_processor.get_total_pages(args.pdf)
    log.info("Нарезка PDF на пачки...")
    try:
        batches = await pdf_processor.split_pdf_into_batches(
            pdf_path=args.pdf, batch_size=args.batch, chunk_size=args.chunk,
            duplex=args.duplex, copies=args.copies, batches_dir=batches_dir, booklet_mode=args.booklet,
        )
        total_batches = len(batches)
    except Exception as e:
        log.error(f"Ошибка подготовки: {e}")
        return

    if args.dry_run:
        log.info(f"[dry-run] Пачки в: {batches_dir}")
        return

    # ── Умное восстановление состояния (CLI) ──
    state_mgr = JobState(batches_dir)
    data = state_mgr.load()
    start_idx = 0
    skip_pages = 0

    if data and data.get("phase") != Phase.DONE:
        start_idx = data.get("completed_batches", 0)
        snmp_start = data.get("snmp_start_count", 0)
        expected = data.get("snmp_expected_add", 0)
        
        log.warning(f"⚠️ Найдено прерванное задание на пачке {start_idx+1}.")
        
        # Авто-SNMP
        if snmp_start > 0:
            try:
                printer_state = await snmp_manager.get_printer_state(args.ip, snmp_cfg)
                if printer_state.page_count > snmp_start:
                    skip_pages = min(printer_state.page_count - snmp_start, expected)
            except Exception as e:
                log.error(f"Ошибка проверки SNMP при восстановлении: {e}")
                
        log.info(f"📊 Анализ счетчика: напечатано {skip_pages} из {expected} стр. в этой пачке.")
        
        if not args.auto_resume:
            if input(f"Продолжить с пропуском {skip_pages} стр.? [y/n]: ").strip().lower() != "y":
                return
    elif not data:
        state_mgr.save({
            "chat_id": 0, "pdf_name": Path(args.pdf).name, "_pdf_source_path": args.pdf,
            "copies": args.copies, "duplex": args.duplex, "booklet": args.booklet,
            "batch_size": args.batch, "cooldown": args.cooldown,
            "total_batches": total_batches, "completed_batches": 0,
            "phase": Phase.INIT, "snmp_start_session": 0, "session_start_ts": time.time()
        })

    log.info("Проверка связи с принтером...")
    printer_online = False
    for attempt in range(1, 4):
        state = await snmp_manager.get_printer_state(args.ip, snmp_cfg)
        if state.status != snmp_manager.PrinterStatus.OFFLINE:
            printer_online = True
            break
        log.warning(f"Принтер недоступен (попытка {attempt}/3) — жду 10с...")
        await asyncio.sleep(10)

    if not printer_online:
        log.error(f"Принтер оффлайн: {args.ip}")
        sys.exit(1)

    log.info(f"Принтер: {state.status.value} | счётчик: {state.page_count} стр.")
    completed = start_idx
    session_start = time.time()

    # ── Главный цикл печати ──
    for i in range(start_idx, total_batches):
        batch_chunks = batches[i]
        
        # ── МАГИЯ ЧАСТИЧНОГО ВОССТАНОВЛЕНИЯ ──
        if skip_pages > 0 and i == start_idx:
            log.info(f"Восстановление: пропускаем первые {skip_pages} стр. из пачки {i+1}")
            skipped = 0
            new_chunks = []
            for chunk in batch_chunks:
                pages_in_chunk = pdf_processor.expected_pages_for_batch(chunk)
                if skipped + pages_in_chunk <= skip_pages:
                    skipped += pages_in_chunk
                    log.info(f"  Пропущен чанк целиком: {chunk.name}")
                elif skipped < skip_pages:
                    pages_to_skip = skip_pages - skipped
                    sliced_path = chunk.with_name(chunk.stem + f"_sliced_{pages_to_skip}.pdf")
                    if not sliced_path.exists():
                        from pypdf import PdfReader, PdfWriter
                        reader = PdfReader(chunk)
                        writer = PdfWriter()
                        for p in range(pages_to_skip, len(reader.pages)): writer.add_page(reader.pages[p])
                        with open(sliced_path, "wb") as f: writer.write(f)
                        await asyncio.sleep(0.5)
                    new_chunks.append(sliced_path.resolve()) 
                    log.info(f"  Чанк {chunk.name} обрезан на {pages_to_skip} стр.")
                    skipped = skip_pages
                else:
                    new_chunks.append(chunk)
            
            batch_chunks = new_chunks
            skip_pages = 0 
            
            if not batch_chunks:
                log.info("Все страницы в этой пачке пропущены. Идем дальше.")
                completed += 1
                continue
        # ─────────────────────────────────────

        if stop.is_set():
            log.info("Остановлено пользователем.")
            break

        log.info(f"\n{'─' * 58}\n  ПАЧКА {i+1}/{total_batches} (из {len(batch_chunks)} частей)\n{'─' * 58}")

        idle_ok = await snmp_manager.wait_until_idle(
            ip=args.ip, cfg=snmp_cfg, timeout_sec=wake_timeout, stop_event=stop,
            on_offline_callback=lambda: log.warning(f"⚠️ Принтер ушёл в оффлайн!"),
        )

        if not idle_ok:
            if not stop.is_set():
                log.error("Принтер не вышел в IDLE.")
                state_mgr.update_phase(Phase.FAILED, completed_batches=completed, error="Printer not IDLE")
            break

        pre_state = await snmp_manager.get_printer_state(args.ip, snmp_cfg)
        start_count = pre_state.page_count
        expected = sum(pdf_processor.expected_pages_for_batch(c) for c in batch_chunks)

        state_mgr.update_phase(
            Phase.PRINTING, 
            current_batch=i + 1, 
            snmp_start_count=start_count,
            snmp_expected_add=expected
        )

        log.info(f"Счётчик ДО: {start_count} | Ожидаем +{expected} стр.")

        send_failed = False
        for chunk_idx, chunk_path in enumerate(batch_chunks):
            log.info(f"  -> Передача части {chunk_idx+1}/{len(batch_chunks)}: {chunk_path.name}")
            if not await print_service.send_to_printer(file_path=chunk_path, printer_name=args.printer, duplex=args.duplex, copies=1, stop_event=stop):
                send_failed = True
                break
                
        if stop.is_set(): break
        if send_failed:
            log.error("Ошибка отправки в спулер.")
            state_mgr.update_phase(Phase.FAILED, completed_batches=completed, error="Spooler failed")
            break

        # 4. SNMP КОНТРОЛЬ ПЕЧАТИ
        log.info("Ожидание аппаратного подтверждения...")
        success, msg, end_count, cycles_in_idle = False, "", start_count, 0
        watchdog_end = time.time() + (watchdog_min * 60)
        last_logged = -1
        
        while time.time() < watchdog_end:
            if stop.is_set():
                state_mgr.update_phase(Phase.PAUSED, completed_batches=completed)
                break

            try: state = await snmp_manager.get_printer_state(args.ip, snmp_cfg)
            except Exception: await asyncio.sleep(3.0); continue

            printed = state.page_count - start_count
            end_count = state.page_count

            if printed != last_logged and printed > 0:
                log.info(f"  [SNMP] Прогресс: {printed}/{expected} стр. (Статус: {state.status.value})")
                last_logged = printed

            if state.status in (PrinterStatus.PRINTING, PrinterStatus.WARMUP):
                cycles_in_idle = 0  
            elif state.status in (PrinterStatus.IDLE, PrinterStatus.SLEEP, PrinterStatus.UNKNOWN):
                if printed >= expected:
                    success, msg = True, f"Успешно: +{printed} стр."
                    break
                else:
                    cycles_in_idle += 1
                    if cycles_in_idle > 240: # 12 мин
                        success, msg = False, f"Таймаут (сеть/спулер): {printed}/{expected} стр."
                        break
            elif state.status in (PrinterStatus.WARNING, PrinterStatus.ERROR):
                cycles_in_idle = 0  # ЖДЕМ БЕСКОНЕЧНО
                # не спамим логами, выводим раз в 60 секунд (20 итераций)
                if cycles_in_idle % 20 == 0:
                    log.warning(f"  ⚠️ Принтер требует бумаги/внимания ({state.status.value}). Ждем...")

            await asyncio.sleep(3.0)

        if stop.is_set(): break
        if not success:
            log.error(f"Сбой пачки: {msg}")
            state_mgr.update_phase(Phase.FAILED, completed_batches=completed, error=msg)
            AuditLog.append(0, args.pdf, total_pages, args.copies, completed, total_batches, "FAILED", start_count, end_count, notes=msg)
            break

        log.info(f"✅ Пачка {i+1} OK. {msg}")
        completed += 1
        state_mgr.update_phase(Phase.COOLING, completed_batches=completed)
        AuditLog.append(0, args.pdf, total_pages, args.copies, completed, total_batches, "BATCH_OK", start_count, end_count)

        if i + 1 < total_batches and not stop.is_set():
            cool_sec = args.cooldown * 60
            cool_until = time.time() + cool_sec
            eta = datetime.now() + timedelta(seconds=cool_sec)
            log.info(f"🌡 Охлаждение {args.cooldown} мин. Следующая в {eta:%H:%M:%S}")
            
            while time.time() < cool_until:
                if stop.is_set(): break
                rem = int(cool_until - time.time())
                m, s = divmod(rem, 60)
                print(f"\r  [{progress_bar(cool_sec - rem, cool_sec, 30)}] {m:02d}:{s:02d} осталось", end="")
                try: await asyncio.wait_for(asyncio.shield(stop.wait()), timeout=2.0)
                except asyncio.TimeoutError: pass
            print("\r" + " " * 60 + "\r", end="")

    if completed == total_batches and not stop.is_set():
        log.info("🏁 ВСЁ НАПЕЧАТАНО!")
        state_mgr.update_phase(Phase.DONE)
        AuditLog.append(0, args.pdf, total_pages, args.copies, completed, total_batches, "ALL_DONE", 0, end_count, notes="CLI Успех")
        pdf_processor.cleanup_batches(batches)

def main() -> None:
    try: asyncio.run(_async_main())
    except KeyboardInterrupt: pass

if __name__ == "__main__":
    main()