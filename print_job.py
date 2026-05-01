"""
print_job.py
"""
import asyncio
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

import pdf_processor
import print_service
import snmp_manager
from snmp_manager import PrinterStatus
from state_manager import JobState, AuditLog, Phase

# Импортируем общие настройки и утилиты из core и config
from config import PRINTER_IP, PRINTER_NAME, SNMP_CFG, WAKE_TIMEOUT, WATCHDOG_MIN, CHUNKS_PER_BATCH
from core import JobConfig, safe_edit, kb_printing, _esc, progress_bar, cooldown_bar

log = logging.getLogger(__name__)

async def run_print_job(bot: Bot, chat_id: int, job: JobConfig) -> None:
    """
    Главный async цикл печати.
    Запускается как asyncio.Task, работает в фоне.
    Обновляет одно Telegram-сообщение (job.status_msg_id) в реальном времени.
    """
    stop: asyncio.Event = job.stop_event

    async def update_status(text: str, kb=None) -> None:
        if job.status_msg_id:
            await safe_edit(bot, chat_id, job.status_msg_id, text,
                            reply_markup=kb or kb_printing(not stop.is_set()))

    # ── Нарезка PDF ───────────────────────────────────────────────────────────
    await update_status("⚙️ <b>Подготовка...</b>\nНарезаю PDF на пачки...")

    source_pdf = job.pdf_path
    tmp_slice  = None

    if job.page_from != 1 or (job.page_to and job.page_to != job.total_pages):
        try:
            from pypdf import PdfReader, PdfWriter
            reader = PdfReader(job.pdf_path)
            writer = PdfWriter()
            p_from = job.page_from - 1          
            p_to   = job.page_to_real()         
            for p in range(p_from, min(p_to, len(reader.pages))):
                writer.add_page(reader.pages[p])
            tmp_slice = job.pdf_path + "_slice.pdf"
            with open(tmp_slice, "wb") as f:
                writer.write(f)
            source_pdf = tmp_slice
        except Exception as e:
            await update_status(f"❌ Ошибка нарезки диапазона: {_esc(str(e))}")
            return

    batches_dir = pdf_processor.get_batches_dir(source_pdf)
    actual_batch = job.batch_size
    actual_chunk = max(8, actual_batch // CHUNKS_PER_BATCH)

    try:
        batches = await pdf_processor.split_pdf_into_batches(
            pdf_path=source_pdf,
            batch_size=actual_batch,
            chunk_size=actual_chunk,
            duplex=job.duplex,
            copies=job.copies,
            batches_dir=batches_dir,
            booklet_mode=job.booklet,
        )
    except Exception as e:
        await update_status(f"❌ Ошибка подготовки файлов:\n{_esc(str(e))}")
        return

    total_batches = len(batches)
    job.total_batches = total_batches 
    
    # Инициализируем JSON стейт
    state_mgr = JobState(batches_dir)
    state_mgr.save({
        "chat_id": chat_id,
        "pdf_name": job.pdf_name,
        "_pdf_source_path": job.pdf_path,
        "copies": job.copies,
        "duplex": job.duplex,
        "booklet": job.booklet,
        "batch_size": job.batch_size,
        "cooldown": job.cooldown,
        "total_batches": total_batches,
        "completed_batches": job.start_index,
        "phase": Phase.INIT,
        "snmp_start_session": 0, 
        "session_start_ts": time.time()
    })

    cool_total_sec = float(job.cooldown * 60)
    session_start = time.time()
    completed = job.start_index

    # ── Проверка принтера ─────────────────────────────────────────────────────
    await update_status("🔍 <b>Проверка принтера...</b>")

    printer_online = False
    for attempt in range(1, 4):
        state = await snmp_manager.get_printer_state(PRINTER_IP, SNMP_CFG)
        if state.status != PrinterStatus.OFFLINE:
            printer_online = True
            break
        try:
            await asyncio.wait_for(asyncio.shield(stop.wait()), timeout=10.0)
            break
        except asyncio.TimeoutError:
            pass

    if not printer_online:
        await update_status(
            f"❌ <b>Принтер недоступен!</b>\nIP: <code>{_esc(PRINTER_IP)}</code>\nПроверьте сеть и питание.",
            kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ OK", callback_data="cancel")]])
        )
        return

    if stop.is_set():
        await update_status("⏸ Остановлено до начала печати.")
        return

    # ── Главный цикл ──────────────────────────────────────────────────────────
    for i in range(job.start_index, total_batches):
        batch_chunks = batches[i]
        job.current_batch = i + 1

        # ── МАГИЯ ЧАСТИЧНОГО ВОССТАНОВЛЕНИЯ (ОТРЕЗАЕМ УЖЕ НАПЕЧАТАННОЕ) ──
        if getattr(job, 'recovery_skip_pages', 0) > 0 and i == job.start_index:
            log.info(f"Восстановление: пропускаем первые {job.recovery_skip_pages} стр. из пачки {i+1}")
            skipped = 0
            new_chunks = []
            for chunk in batch_chunks:
                pages_in_chunk = pdf_processor.expected_pages_for_batch(chunk)
                if skipped + pages_in_chunk <= job.recovery_skip_pages:
                    skipped += pages_in_chunk
                    log.info(f"  Пропущен чанк целиком: {chunk.name}")
                elif skipped < job.recovery_skip_pages:
                    pages_to_skip = job.recovery_skip_pages - skipped
                    sliced_path = chunk.with_name(chunk.stem + f"_sliced_{pages_to_skip}.pdf")
                    if not sliced_path.exists():
                        from pypdf import PdfReader, PdfWriter
                        reader = PdfReader(chunk)
                        writer = PdfWriter()
                        for p in range(pages_to_skip, len(reader.pages)):
                            writer.add_page(reader.pages[p])
                        with open(sliced_path, "wb") as f:
                            writer.write(f)
                        await asyncio.sleep(0.5)  # <--- Задержка для файловой системы Windows
                    # <--- Передаем абсолютный путь, Sumatra это любит
                    new_chunks.append(sliced_path.resolve()) 
                    log.info(f"  Чанк {chunk.name} обрезан на {pages_to_skip} стр.")
                    skipped = job.recovery_skip_pages
                else:
                    new_chunks.append(chunk)
            
            batch_chunks = new_chunks
            job.recovery_skip_pages = 0 # Сброс, чтобы следующие пачки печатались нормально
            
            if not batch_chunks:
                log.info("Все страницы в этой пачке пропущены. Идем дальше.")
                completed += 1
                continue
        # ──────────────────────────────────────────────────────────────────
        
        if stop.is_set():
            await update_status(
                f"⏸ <b>Остановлено</b>\n\nНапечатано: {completed}/{total_batches} пачек\nСледующий запуск продолжит с пачки {i+1}.",
                kb=InlineKeyboardMarkup(inline_keyboard=[])
            )
            break

        try:
            copies_in_batch = sorted(list(set(int(c.stem.split('_')[0].replace('cp', '')) for c in batch_chunks)))
            if len(copies_in_batch) == 1:
                copy_str = f"📑 <b>Копия: {copies_in_batch[0]} из {job.copies}</b>\n"
                p_start = batch_chunks[0].stem.split('_p')[-1].split('-')[0]
                p_end = batch_chunks[-1].stem.split('-')[-1].split('.')[0]
                page_range = f"{int(p_start)}–{int(p_end)}"
            else:
                copy_str = f"📑 <b>Копии: {copies_in_batch[0]} ➡️ {copies_in_batch[-1]} из {job.copies}</b>\n"
                page_range = "переход между копиями (стык)"
        except Exception:
            copy_str = ""
            page_range = "unknown"

        bar = progress_bar(i, total_batches)

        await update_status(
            f"⏳ <b>Пачка {i+1}/{total_batches}</b>\n[{bar}]\n\n{copy_str}"
            f"📄 Страницы: {page_range} (из {len(batch_chunks)} частей)\n🔄 Ожидаю готовности принтера..."
        )

        idle_ok = await snmp_manager.wait_until_idle(
            ip=PRINTER_IP, cfg=SNMP_CFG, timeout_sec=WAKE_TIMEOUT, stop_event=stop,
            on_offline_callback=lambda: asyncio.create_task(update_status(f"⚠️ <b>Принтер ушёл в оффлайн!</b>\nЖду возвращения... (пачка {i+1}/{total_batches})")),
        )

        if not idle_ok:
            if not stop.is_set():
                await update_status(f"❌ <b>Принтер не готов!</b>\n\nПачка {i+1} пропущена — принтер не вышел в IDLE.", kb=InlineKeyboardMarkup(inline_keyboard=[]))
                state_mgr.update_phase(Phase.FAILED, completed_batches=completed, error="Printer not IDLE")
                AuditLog.append(chat_id, job.pdf_name, job.total_pages, job.copies, completed, total_batches, "FAILED", 0, 0, notes="Ошибка: принтер не вышел в IDLE")
            break

        pre_state   = await snmp_manager.get_printer_state(PRINTER_IP, SNMP_CFG)
        start_count = pre_state.page_count
        expected = sum(pdf_processor.expected_pages_for_batch(c) for c in batch_chunks)

        state_mgr.update_phase(
            Phase.PRINTING, 
            current_batch=i + 1, 
            snmp_start_count=start_count,  # <--- ВАЖНО: ЗАПОМИНАЕМ СЧЕТЧИК
            snmp_expected_add=expected
        )

        await update_status(f"🖨 <b>Пачка {i+1}/{total_batches}</b>\n[{bar}]\n\n📄 Страницы: {page_range}\n📨 Отправляю задание в спулер...\n📊 Счётчик: {start_count} → ожидаем +{expected}")

        send_failed = False
        for chunk_idx, chunk_path in enumerate(batch_chunks):
            await update_status(f"🖨 <b>Пачка {i+1}/{total_batches}</b>\n[{bar}]\n\n📨 Передача части {chunk_idx+1}/{len(batch_chunks)} по Wi-Fi...")
            sent = await print_service.send_to_printer(file_path=chunk_path, printer_name=PRINTER_NAME, duplex=job.duplex, copies=1, stop_event=stop)
            if not sent:
                send_failed = True
                break
                
        if stop.is_set():
            break   
        
        if send_failed:
            await update_status(f"❌ <b>Ошибка отправки!</b>\n\nПачка {i+1} не ушла в спулер.", kb=InlineKeyboardMarkup(inline_keyboard=[]))
            state_mgr.update_phase(Phase.FAILED, completed_batches=completed, error="Spooler send failed")
            AuditLog.append(chat_id, job.pdf_name, job.total_pages, job.copies, completed, total_batches, "FAILED", start_count, start_count, notes="Ошибка отправки в спулер")
            break

        # 4. SNMP УМНЫЙ КОНТРОЛЬ ПЕЧАТИ
        await update_status(f"🖨 <b>Печатаю пачку {i+1}/{total_batches}</b>\n[{bar}]\n\n📄 Страницы: {page_range}\n⏳ Жду подтверждения (отправка по сети)...")
        log.info(f"  [SNMP] Контроль печати: ждём +{expected} стр. (с {start_count})")

        success, msg, end_count, cycles_in_idle = False, "", start_count, 0
        watchdog_end = time.time() + (WATCHDOG_MIN * 60)
        last_logged_printed, last_notified_status = -1, None
        
        last_tg_update = 0  # <--- ДОБАВЛЕНО: Таймер для защиты от спама в Telegram

        while time.time() < watchdog_end:
            if stop.is_set():
                state_mgr.update_phase(Phase.PAUSED, completed_batches=completed)
                AuditLog.append(chat_id, job.pdf_name, job.total_pages, job.copies, completed, total_batches, "PAUSED", start_count, end_count, notes="Остановлено пользователем")
                break

            try:
                state = await snmp_manager.get_printer_state(PRINTER_IP, SNMP_CFG)
            except Exception:
                await asyncio.sleep(3.0)
                continue

            current_pages = state.page_count
            printed = current_pages - start_count
            end_count = current_pages

            if printed != last_logged_printed and printed > 0:
                log.info(f"  [SNMP] Прогресс: {printed}/{expected} стр. (Статус: {state.status.value})")
                last_logged_printed = printed
                
                # --- ЖИВОЙ ПРОГРЕСС В ТЕЛЕГРАМ (С ЛИМИТОМ 15 СЕКУНД) ---
                if time.time() - last_tg_update >= 15:
                    await update_status(
                        f"🖨 <b>Печатаю пачку {i+1}/{total_batches}</b>\n"
                        f"[{bar}]\n\n"
                        f"{copy_str}"
                        f"📄 Страницы: {page_range}\n"
                        f"📊 Прогресс печати: <b>{printed} из {expected} стр.</b>\n"
                        f"<i>⏳ Идет процесс...</i>"
                    )
                    last_tg_update = time.time()  # Обновляем таймер после отправки
                # --------------------------------------------------------

            if state.status in (PrinterStatus.PRINTING, PrinterStatus.WARMUP):
                cycles_in_idle = 0  
                if last_notified_status in (PrinterStatus.WARNING, PrinterStatus.ERROR):
                    await update_status(f"🖨 <b>Печатаю пачку {i+1}/{total_batches}</b>\n[{bar}]\n\n✅ <b>Проблема решена, печать продолжается!</b>\n📄 Страницы: {page_range}\n📊 Прогресс: {max(0, printed)}/{expected} стр.")
                    last_notified_status = state.status

            elif state.status in (PrinterStatus.IDLE, PrinterStatus.SLEEP, PrinterStatus.UNKNOWN):
                if printed >= expected:
                    success, msg = True, f"Успешно: +{printed} стр."
                    log.info(f"  ✅ [SNMP] Пачка завершена. {msg}")
                    break
                else:
                    cycles_in_idle += 1
                    if cycles_in_idle > 240:  # 12 минуты ожидания
                        success, msg = False, f"Счётчик +{printed}, ожидалось ≥{expected}. Задание потеряно в сети/спулере."
                        log.warning(f"  ⚠️ [SNMP] Ошибка: {msg}")
                        break

            elif state.status in (PrinterStatus.WARNING, PrinterStatus.ERROR):
                cycles_in_idle = 0  # Ждем человека бесконечно
                if last_notified_status != state.status:
                    alert_icon = "⚠️" if state.status == PrinterStatus.WARNING else "❌"
                    alert_reason = "нет бумаги?" if state.status == PrinterStatus.WARNING else "замятие/открыта крышка"
                    log.warning(f"  {alert_icon} [SNMP] Принтер требует внимания ({alert_reason}). Ждем человека...")
                    await update_status(f"{alert_icon} <b>Вмешательство человека!</b>\nПачка {i+1}/{total_batches}\n\nПринтер сообщил о проблеме (статус: <code>{state.status.value}</code>).\n👉 <b>Проверьте лоток с бумагой или замятие.</b>\n\n<i>Бот ждет. Печать продолжится автоматически после устранения проблемы.</i>")
                    last_notified_status = state.status

            await asyncio.sleep(3.0)
            
        if not success and msg == "": msg = "Таймаут (watchdog) превышен."

        if stop.is_set(): break

        if not success:
            await update_status(f"❌ <b>Ошибка верификации! Пачка {i+1}/{total_batches}</b>\n\n🔴 {_esc(msg)}\n\nПроверьте принтер и запустите заново.", kb=InlineKeyboardMarkup(inline_keyboard=[]))
            state_mgr.update_phase(Phase.FAILED, completed_batches=completed, error=msg)
            AuditLog.append(chat_id, job.pdf_name, job.total_pages, job.copies, completed, total_batches, "FAILED", start_count, end_count, notes=f"SNMP Сбой: {msg}")
            break

        completed += 1
        state_mgr.update_phase(Phase.COOLING, completed_batches=completed)
        AuditLog.append(chat_id, job.pdf_name, job.total_pages, job.copies, completed, total_batches, "BATCH_OK", start_count, end_count, notes=f"Пачка {i+1} напечатана успешно")
        
        pages_printed = end_count - start_count
        bar_done      = progress_bar(completed, total_batches)    

        if i + 1 < total_batches and not stop.is_set():
            cool_until = time.time() + cool_total_sec
            eta = datetime.now() + timedelta(seconds=cool_total_sec)
            
            log.info(f"  🌡 Уходим на охлаждение {job.cooldown} мин. Продолжение в {eta:%H:%M}")

            while True:
                remaining = cool_until - time.time()
                if remaining <= 0 or stop.is_set(): break

                m, s = divmod(int(remaining), 60)
                cbar = cooldown_bar(remaining, cool_total_sec)
                await update_status(f"✅ <b>Пачка {i+1}/{total_batches} напечатана</b>\n[{bar_done}]\n\n📊 Напечатано: {pages_printed} стр.\n\n🌡 <b>Охлаждение принтера:</b>\n[{cbar}] {m:02d}:{s:02d}\n⏰ Продолжение в {eta:%H:%M}")

                try: await asyncio.wait_for(asyncio.shield(stop.wait()), timeout=15.0)
                except asyncio.TimeoutError: pass

    # ── Финализация ───────────────────────────────────────────────────────────
    if completed == total_batches and not stop.is_set():
        state_mgr.update_phase(Phase.DONE)
        duration_min = int((time.time() - session_start) / 60)
        
        AuditLog.append(chat_id, job.pdf_name, job.total_pages, job.copies, completed, total_batches, "ALL_DONE", 0, end_count, duration_min=duration_min, notes="Сессия успешно завершена")
        
        h, m = divmod(duration_min, 60)
        dur_str = f"{h}ч {m}мин" if h else f"{m}мин"
        bar_full = progress_bar(total_batches, total_batches)

        await update_status(
            f"🏁 <b>Печать завершена!</b>\n[{bar_full}]\n\n📄 <b>Файл:</b> {_esc(job.pdf_name)}\n📦 Пачек: {total_batches}\n📊 Страниц: {job.pages_to_print() * job.copies}\n⏱ Время: {dur_str}\n\n📥 <b>Заберите документы из принтера!</b>",
            kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Готово", callback_data="cancel")]])
        )
        pdf_processor.cleanup_batches(batches)

    if tmp_slice and Path(tmp_slice).exists():
        try: Path(tmp_slice).unlink()
        except Exception: pass