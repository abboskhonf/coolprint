"""
handlers.py
"""
import asyncio
import logging
import shutil
import uuid
from pathlib import Path
from aiogram import Router, F
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

import pdf_processor
import snmp_manager
from snmp_manager import PrinterStatus
from state_manager import JobState, Phase
from config import PRINTER_IP, SNMP_CFG, DOWNLOADS_DIR, DOWNLOAD_TIMEOUT
from core import JobStates, JobConfig, active_jobs, print_tasks, is_allowed, _esc, safe_edit, kb_main_config, kb_cancel_input
from print_job import run_print_job

log = logging.getLogger(__name__)
router = Router()

def _check_access(message_or_query) -> bool:
    cid = message_or_query.chat.id if isinstance(message_or_query, Message) else message_or_query.message.chat.id
    return is_allowed(cid)

async def _refresh_config_msg(query: CallbackQuery, job: JobConfig) -> None:
    await safe_edit(
        query.message.bot, query.message.chat.id, query.message.message_id,
        job.summary() + "\n\n⬇️ Настройте параметры и нажмите «🚀 Запустить»:",
        reply_markup=kb_main_config(job),
    )

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    if not _check_access(message): return
    await state.set_state(JobStates.waiting_for_file)
    await message.answer(
        "👋 <b>Canon iR2425 — Бот печати</b>\n\n"
        "Отправьте PDF-файл для настройки и запуска печати.\n\n"
        "Команды:\n  /status — статус принтера\n  /stop   — остановить текущую печать\n  /cancel — отменить текущую операцию",
        parse_mode=ParseMode.HTML,
    )

@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    if not _check_access(message): return
    await cmd_start(message, await message.bot.get_state(message.chat.id))

@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    if not _check_access(message): return
    chat_id = message.chat.id
    msg = await message.answer("🔍 Опрашиваю принтер...")
    state = await snmp_manager.get_printer_state(PRINTER_IP, SNMP_CFG)

    status_emoji = {
        PrinterStatus.IDLE: "✅", PrinterStatus.PRINTING: "🖨",
        PrinterStatus.WARMUP: "🔥", PrinterStatus.OFFLINE: "❌",
        PrinterStatus.WARNING: "⚠️", PrinterStatus.ERROR: "🔴",
    }.get(state.status, "❓")

    active = active_jobs.get(chat_id)
    active_text = f"\n\n🖨 <b>Активное задание:</b>\n  Файл: {_esc(active.pdf_name)}\n  Пачка: {active.current_batch}/{active.total_batches}" if active else ""

    await msg.edit_text(f"{status_emoji} <b>Принтер:</b> {_esc(PRINTER_IP)}\n  Статус: <code>{state.status.value}</code>\n  Счётчик: {state.page_count} стр." + active_text, parse_mode=ParseMode.HTML)

@router.message(Command("stop"))
async def cmd_stop(message: Message) -> None:
    if not _check_access(message): return
    chat_id = message.chat.id
    job = active_jobs.get(chat_id)
    if not job or not job.stop_event:
        return await message.answer("ℹ️ Нет активного задания печати.")
    job.stop_event.set()
    await message.answer("⏸ Команда остановки отправлена. Принтер завершит текущую пачку.")

@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    if not _check_access(message): return
    await state.set_state(JobStates.waiting_for_file)
    await state.update_data(job=None)
    await message.answer("↩️ Операция отменена. Отправьте новый PDF.")

@router.message(F.document)
async def handle_document(message: Message, state: FSMContext) -> None:
    if not _check_access(message): return
    chat_id = message.chat.id
    if chat_id in active_jobs: return await message.answer("⚠️ Сейчас идёт печать. Дождитесь завершения или остановите командой /stop")

    doc = message.document
    if not doc.file_name.lower().endswith(".pdf"): return await message.answer("❌ Пожалуйста, отправьте PDF-файл.")

    wait_msg = await message.answer("⬇️ Скачиваю файл...")
    save_path = DOWNLOADS_DIR / f"{chat_id}_{uuid.uuid4().hex[:8]}_{doc.file_name}"

    try:
        await message.bot.download(doc, destination=str(save_path), timeout=DOWNLOAD_TIMEOUT)
    except Exception as e:
        save_path.unlink(missing_ok=True)
        return await wait_msg.edit_text(f"❌ Ошибка загрузки ({type(e).__name__}): {_esc(str(e))}")

    total_pages = await asyncio.to_thread(pdf_processor.get_total_pages, str(save_path))
    if total_pages == 0:
        save_path.unlink(missing_ok=True)
        return await wait_msg.edit_text("❌ Не удалось прочитать PDF. Файл повреждён?")

    job = JobConfig(pdf_path=str(save_path), pdf_name=doc.file_name, total_pages=total_pages)
    await state.update_data(job=job)
    await state.set_state(JobStates.configuring)

    await wait_msg.edit_text(job.summary() + "\n\n⬇️ Настройте параметры и нажмите «🚀 Запустить»:", parse_mode=ParseMode.HTML, reply_markup=kb_main_config(job))

# Настройки кнопок
@router.callback_query(JobStates.configuring, F.data.startswith("booklet:"))
async def cb_booklet(query: CallbackQuery, state: FSMContext) -> None:
    job: JobConfig = (await state.get_data()).get("job")
    if not job: return await query.answer("Сессия истекла.")
    mode = query.data.split(":")[1]
    if job.booklet == mode: return await query.answer()
    job.booklet = mode
    job.duplex = "short" if mode != "none" else "long"
    await state.update_data(job=job)
    await query.answer(f"Режим: {job.booklet_ru()}")
    await _refresh_config_msg(query, job)

@router.callback_query(JobStates.configuring, F.data.startswith("dup:"))
async def cb_duplex(query: CallbackQuery, state: FSMContext) -> None:
    job: JobConfig = (await state.get_data()).get("job")
    if not job: return await query.answer("Сессия истекла.")
    if job.booklet != "none": return await query.answer("В режиме брошюры дуплекс всегда короткий!", show_alert=True)
    job.duplex = query.data.split(":")[1]
    await state.update_data(job=job)
    await query.answer(f"Дуплекс: {job.duplex_ru()}")
    await _refresh_config_msg(query, job)

@router.callback_query(JobStates.configuring, F.data.startswith("pages:"))
async def cb_pages(query: CallbackQuery, state: FSMContext) -> None:
    job: JobConfig = (await state.get_data()).get("job")
    if not job: return await query.answer("Сессия истекла.")
    action = query.data.split(":")[1]
    if action == "all":
        job.page_from, job.page_to = 1, 0
        await state.update_data(job=job)
        await query.answer("Все страницы")
        await _refresh_config_msg(query, job)
    elif action == "custom":
        await state.set_state(JobStates.waiting_pages_input)
        await query.answer()
        await safe_edit(query.message.bot, query.message.chat.id, query.message.message_id, f"✏️ Введите диапазон страниц (файл: {job.total_pages} стр.)\n\nФормат: <code>10-50</code>\nМаксимум: 1–{job.total_pages}", reply_markup=kb_cancel_input())

@router.message(JobStates.waiting_pages_input)
async def input_pages(message: Message, state: FSMContext) -> None:
    if not _check_access(message): return
    job: JobConfig = (await state.get_data()).get("job")
    if not job: return
    try:
        p_from, p_to = map(int, message.text.strip().split("-"))
        if p_from < 1 or p_to > job.total_pages or p_from >= p_to: raise ValueError
        job.page_from, job.page_to = p_from, p_to
        await state.update_data(job=job)
        await state.set_state(JobStates.configuring)
        await message.answer(job.summary() + "\n\n⬇️ Настройте параметры и нажмите «🚀 Запустить»:", parse_mode=ParseMode.HTML, reply_markup=kb_main_config(job))
    except ValueError:
        await message.answer(f"❌ Неверный формат. Введите диапазон вида <code>10-50</code>", parse_mode=ParseMode.HTML, reply_markup=kb_cancel_input())

@router.callback_query(JobStates.configuring, F.data.startswith("cop:"))
async def cb_copies(query: CallbackQuery, state: FSMContext) -> None:
    job: JobConfig = (await state.get_data()).get("job")
    if not job: return await query.answer("Сессия истекла.")
    action = query.data.split(":")[1]
    if action == "+1":
        job.copies = min(job.copies + 1, 20)
        await state.update_data(job=job)
        await query.answer(f"Копий: {job.copies}")
        await _refresh_config_msg(query, job)
    elif action == "-1":
        job.copies = max(job.copies - 1, 1)
        await state.update_data(job=job)
        await query.answer(f"Копий: {job.copies}")
        await _refresh_config_msg(query, job)
    elif action == "input":
        await state.set_state(JobStates.waiting_copies_input)
        await query.answer()
        await safe_edit(query.message.bot, query.message.chat.id, query.message.message_id, "✏️ Введите количество копий (1–20):", reply_markup=kb_cancel_input())

@router.message(JobStates.waiting_copies_input)
async def input_copies(message: Message, state: FSMContext) -> None:
    if not _check_access(message): return
    job: JobConfig = (await state.get_data()).get("job")
    if not job: return
    try:
        n = int(message.text.strip())
        if not 1 <= n <= 20: raise ValueError
        job.copies = n
        await state.update_data(job=job)
        await state.set_state(JobStates.configuring)
        await message.answer(job.summary() + "\n\n⬇️ Настройте параметры и нажмите «🚀 Запустить»:", parse_mode=ParseMode.HTML, reply_markup=kb_main_config(job))
    except ValueError:
        await message.answer("❌ Введите число от 1 до 20:", reply_markup=kb_cancel_input())

@router.callback_query(JobStates.configuring, F.data.startswith("bat:"))
async def cb_batch(query: CallbackQuery, state: FSMContext) -> None:
    job: JobConfig = (await state.get_data()).get("job")
    if not job: return await query.answer("Сессия истекла.")
    action = query.data.split(":")[1]
    if action == "+8":
        job.batch_size = min(((job.batch_size // 8) + 1) * 8, 200)
        await state.update_data(job=job)
        await query.answer(f"Пачка: {job.batch_size} стр.")
        await _refresh_config_msg(query, job)
    elif action == "-8":
        job.batch_size = max(((job.batch_size - 1) // 8) * 8, 8)
        await state.update_data(job=job)
        await query.answer(f"Пачка: {job.batch_size} стр.")
        await _refresh_config_msg(query, job)
    elif action == "input":
        await state.set_state(JobStates.waiting_batch_input)
        await query.answer()
        await safe_edit(query.message.bot, query.message.chat.id, query.message.message_id, "✏️ Введите размер пачки в страницах (8–200):", reply_markup=kb_cancel_input())

@router.message(JobStates.waiting_batch_input)
async def input_batch(message: Message, state: FSMContext) -> None:
    if not _check_access(message): return
    job: JobConfig = (await state.get_data()).get("job")
    if not job: return
    try:
        n = int(message.text.strip())
        if not 8 <= n <= 200: raise ValueError
        job.batch_size = max(8, int(round(n / 8)) * 8)
        await state.update_data(job=job)
        await state.set_state(JobStates.configuring)
        await message.answer(job.summary() + "\n\n⬇️ Настройте параметры и нажмите «🚀 Запустить»:", parse_mode=ParseMode.HTML, reply_markup=kb_main_config(job))
    except ValueError:
        await message.answer("❌ Введите число от 8 до 200:", reply_markup=kb_cancel_input())

@router.callback_query(JobStates.configuring, F.data.startswith("cool:"))
async def cb_cool(query: CallbackQuery, state: FSMContext) -> None:
    job: JobConfig = (await state.get_data()).get("job")
    if not job: return await query.answer("Сессия истекла.")
    action = query.data.split(":")[1]
    if action == "+5":
        job.cooldown = min(job.cooldown + 5, 120)
        await state.update_data(job=job)
        await query.answer(f"Охлаждение: {job.cooldown} мин")
        await _refresh_config_msg(query, job)
    elif action == "-5":
        job.cooldown = max(job.cooldown - 5, 5)
        await state.update_data(job=job)
        await query.answer(f"Охлаждение: {job.cooldown} мин")
        await _refresh_config_msg(query, job)
    elif action == "input":
        await state.set_state(JobStates.waiting_cool_input)
        await query.answer()
        await safe_edit(query.message.bot, query.message.chat.id, query.message.message_id, "✏️ Введите паузу охлаждения в минутах (5–120):", reply_markup=kb_cancel_input())

@router.message(JobStates.waiting_cool_input)
async def input_cool(message: Message, state: FSMContext) -> None:
    if not _check_access(message): return
    job: JobConfig = (await state.get_data()).get("job")
    if not job: return
    try:
        n = int(message.text.strip())
        if not 5 <= n <= 120: raise ValueError
        job.cooldown = n
        await state.update_data(job=job)
        await state.set_state(JobStates.configuring)
        await message.answer(job.summary() + "\n\n⬇️ Настройте параметры и нажмите «🚀 Запустить»:", parse_mode=ParseMode.HTML, reply_markup=kb_main_config(job))
    except ValueError:
        await message.answer("❌ Введите число от 5 до 120:", reply_markup=kb_cancel_input())

@router.callback_query(F.data == "back_to_config")
async def cb_back(query: CallbackQuery, state: FSMContext) -> None:
    job: JobConfig = (await state.get_data()).get("job")
    if not job: return await query.answer("Сессия истекла. Отправьте PDF заново.")
    await state.set_state(JobStates.configuring)
    await query.answer()
    await safe_edit(query.message.bot, query.message.chat.id, query.message.message_id, job.summary() + "\n\n⬇️ Настройте параметры и нажмите «🚀 Запустить»:", reply_markup=kb_main_config(job))

@router.callback_query(JobStates.configuring, F.data == "start")
async def cb_start_print(query: CallbackQuery, state: FSMContext) -> None:
    chat_id = query.message.chat.id
    job: JobConfig = (await state.get_data()).get("job")
    if not job: return await query.answer("Сессия истекла. Отправьте PDF заново.")
    if chat_id in active_jobs: return await query.answer("⚠️ Уже идёт печать!")

    if job.real_load() > 30 and not job.warn_shown:
        job.warn_shown = True
        await state.update_data(job=job)
        return await query.answer(f"⚠️ Высокая нагрузка: {job.real_load()} листов/пачку! Рекомендуется ≤30. Нажмите 'Запустить' ещё раз.", show_alert=True)

    await query.answer("🚀 Запускаю!")
    await state.set_state(JobStates.printing)

    job.stop_event = asyncio.Event()
    active_jobs[chat_id] = job

    await safe_edit(query.message.bot, chat_id, query.message.message_id, f"🚀 <b>Запускаю печать...</b>\n\n📄 {_esc(job.pdf_name)}\n📦 Рассчитываю пачки...")
    job.status_msg_id = query.message.message_id

    task = asyncio.create_task(run_print_job(query.message.bot, chat_id, job), name=f"print_{chat_id}")
    print_tasks[chat_id] = task

    def _task_done(t: asyncio.Task):
        print_tasks.pop(chat_id, None)
        active_jobs.pop(chat_id, None)
        if not t.cancelled() and t.exception(): log.error(f"Задание печати упало: {t.exception()}")

    task.add_done_callback(_task_done)

@router.callback_query(F.data == "stop_print")
async def cb_stop_print(query: CallbackQuery) -> None:
    chat_id = query.message.chat.id
    job = active_jobs.get(chat_id)
    if not job or not job.stop_event: return await query.answer("Нет активной печати.")
    job.stop_event.set()
    await query.answer("⏸ Остановка после текущей пачки...")

@router.callback_query(F.data == "refresh_status")
async def cb_refresh(query: CallbackQuery) -> None:
    chat_id = query.message.chat.id
    job = active_jobs.get(chat_id)
    if not job: return await query.answer("Задание завершено.")
    state = await snmp_manager.get_printer_state(PRINTER_IP, SNMP_CFG)
    await query.answer(f"Принтер: {state.status.value} | Пачка: {job.current_batch}/{job.total_batches}", show_alert=False)

@router.callback_query(F.data == "cancel")
async def cb_cancel(query: CallbackQuery, state: FSMContext) -> None:
    chat_id = query.message.chat.id
    if chat_id in active_jobs: return await query.answer("Идёт печать. Для остановки используйте /stop")
    await state.set_state(JobStates.waiting_for_file)
    await state.update_data(job=None)
    await query.answer("Отменено")
    await safe_edit(query.message.bot, chat_id, query.message.message_id, "↩️ Отменено. Отправьте новый PDF для печати.")

# ── Умное Восстановление (Recovery с авто-SNMP) ──

@router.callback_query(F.data.startswith("recovery_menu:"))
async def cb_recovery_menu(query: CallbackQuery, state: FSMContext):
    batch_dir_name = query.data.split(":")[1]
    state_mgr = JobState(Path(batch_dir_name))
    data = state_mgr.load()
    
    if not data or data.get("phase") == Phase.DONE:
        return await query.answer("Задание уже завершено или удалено.", show_alert=True)

    done = data.get("completed_batches", 0)
    snmp_start = data.get("snmp_start_count", 0)
    expected = data.get("snmp_expected_add", 0)
    
    # 1. Получаем актуальный счетчик с принтера прямо сейчас!
    printer_state = await snmp_manager.get_printer_state(PRINTER_IP, SNMP_CFG)
    current_count = printer_state.page_count
    
    # 2. Высчитываем, сколько реально распечаталось до сбоя
    printed_in_batch = 0
    if snmp_start > 0 and current_count >= snmp_start:
        printed_in_batch = current_count - snmp_start
        
    # Защита: если кто-то другой печатал и счетчик улетел в космос
    if printed_in_batch > expected:
        printed_in_batch = expected

    # 3. Формируем кнопки с уже готовой математикой
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🤖 Умное продолжение (пропуск {printed_in_batch} стр)", callback_data=f"rec_run:{batch_dir_name}:smart:{printed_in_batch}")],
        [InlineKeyboardButton(text=f"▶️ Начать пачку {done+1} заново", callback_data=f"rec_run:{batch_dir_name}:full:0")],
        [InlineKeyboardButton(text=f"⏭ Пропустить пачку {done+1} целиком", callback_data=f"rec_run:{batch_dir_name}:skip:0")],
        [InlineKeyboardButton(text="🗑 Удалить задание", callback_data=f"clear:{batch_dir_name}")]
    ])
    
    await query.message.edit_text(
        f"⚙️ <b>Умное восстановление</b>\n\n"
        f"Задание прервано на пачке <b>{done+1}</b>.\n"
        f"📊 <b>Анализ счетчика SNMP:</b>\n"
        f"Счетчик старта: {snmp_start}\n"
        f"Счетчик сейчас: {current_count}\n"
        f"Успешно напечатано: <b>{printed_in_batch} из {expected} стр.</b>\n\n"
        f"Что будем делать?", reply_markup=kb, parse_mode=ParseMode.HTML
    )

@router.callback_query(F.data.startswith("rec_run:"))
async def cb_recovery_run(query: CallbackQuery):
    parts = query.data.split(":")
    batch_dir_name = parts[1]
    mode = parts[2]
    skip_pages = int(parts[3])
    
    skip_batch = (mode == "skip")
    
    await _execute_recovery(query.message.bot, query.message.chat.id, batch_dir_name, query.message.message_id, skip_pages=skip_pages, skip_batch=skip_batch)

async def _execute_recovery(bot, chat_id, batch_dir_name, msg_id, skip_pages=0, skip_batch=False):
    batches_dir = Path(batch_dir_name)
    state_mgr = JobState(batches_dir)
    data = state_mgr.load()
    
    pdf_path = Path(data.get("_pdf_source_path", ""))
    if not pdf_path.exists():
        return await bot.send_message(chat_id, "❌ Исходный PDF удален. Восстановление невозможно.")
    if chat_id in active_jobs:
        return await bot.send_message(chat_id, "⚠️ Уже идёт печать! Сначала остановите её.")

    job = JobConfig(
        pdf_path=str(pdf_path), pdf_name=data.get("pdf_name", "document.pdf"),
        total_pages=pdf_processor.get_total_pages(str(pdf_path)), 
        page_from=data.get("page_from", 1),
        page_to=data.get("page_to", 0),
        copies=data.get("copies", 1),
        duplex=data.get("duplex", "long"), booklet=data.get("booklet", "none"),
        batch_size=data.get("batch_size", 64), cooldown=data.get("cooldown", 25),
        start_index=data.get("completed_batches", 0)
    )
    
    if skip_batch:
        job.start_index += 1
        
    job.recovery_skip_pages = skip_pages
    
    await safe_edit(bot, chat_id, msg_id, f"🔄 <b>Восстановление сессии...</b>\nПропускаем уже напечатанные страниц: {skip_pages}")
    
    job.stop_event = asyncio.Event()
    active_jobs[chat_id] = job
    job.status_msg_id = msg_id
    
    task = asyncio.create_task(run_print_job(bot, chat_id, job), name=f"print_{chat_id}")
    print_tasks[chat_id] = task

    def _task_done(t: asyncio.Task):
        print_tasks.pop(chat_id, None)
        active_jobs.pop(chat_id, None)
        if not t.cancelled() and t.exception(): log.error(f"Задание упало: {t.exception()}")

    task.add_done_callback(_task_done)

@router.callback_query(F.data.startswith("clear:"))
async def cb_recovery_clear(query: CallbackQuery):
    batch_dir_name = query.data.split(":")[1]
    batches_dir = Path(batch_dir_name)
    if batches_dir.exists():
        try: shutil.rmtree(batches_dir)
        except Exception as e: log.error(f"Ошибка удаления {batches_dir}: {e}")

    await query.message.edit_text("🗑 <b>Задание удалено и очищено.</b>", parse_mode=ParseMode.HTML)
    await query.answer()