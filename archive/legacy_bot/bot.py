"""
bot.py
"""
import asyncio
import logging
import sys
from pathlib import Path
from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from config import BOT_TOKEN, PRINTER_IP, PRINTER_NAME, SNMP_CFG, DOWNLOADS_DIR, ALLOWED_CHAT_ID, DOWNLOAD_TIMEOUT
from core import active_jobs, print_tasks
from state_manager import JobState
import snmp_manager
from handlers import router

# ── ВОТ ЭТОТ БЛОК НУЖНО ВЕРНУТЬ ───────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ],
)
# ──────────────────────────────────────────────────────────────────────────────

log = logging.getLogger(__name__)

async def on_startup(bot: Bot):
    log.info("🚀 Бот запущен. Проверка прерванных заданий...")
    interrupted = JobState.find_interrupted_jobs(Path(".")) # <--- Ищем в корне проекта
    
    for job_data in interrupted:
        chat_id = job_data.get("chat_id")
        pdf_name = job_data.get("pdf_name")
        done = job_data.get("completed_batches", 0)
        total = job_data.get("total_batches", 0)
        
        batch_dir_name = Path(job_data["_batches_dir"]).name
        
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="⚙️ Восстановить", callback_data=f"recovery_menu:{batch_dir_name}"),
                InlineKeyboardButton(text="🗑 Удалить", callback_data=f"clear:{batch_dir_name}")
            ]
        ])
        
        try:
            await bot.send_message(
                chat_id,
                f"⚠️ <b>Найдено прерванное задание!</b>\n\n"
                f"📄 Файл: <code>{pdf_name}</code>\n"
                f"📦 Прогресс: {done} из {total} пачек.\n\n"
                f"Желаете продолжить с места остановки?",
                reply_markup=kb,
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            log.error(f"Не удалось отправить recovery сообщение в чат {chat_id}: {e}")

async def on_shutdown(bot: Bot):
    log.warning("!!! Сигнал остановки системы. Перевожу задания в режим PAUSE...")
    for chat_id, job in active_jobs.items():
        if job.stop_event:
            job.stop_event.set()
    
    if print_tasks:
        log.info(f"Ожидаю завершения {len(print_tasks)} фоновых задач...")
        await asyncio.wait(list(print_tasks.values()), timeout=5.0)
    
    log.info("✅ Все состояния сохранены. До свидания!")

async def main() -> None:
    if not BOT_TOKEN:
        print("❌ Telegram token не задан в config.ini [telegram] token=")
        sys.exit(1)

    session = AiohttpSession(timeout=DOWNLOAD_TIMEOUT)
    bot = Bot(token=BOT_TOKEN, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    log.info("=" * 55)
    log.info("  Canon iR2425 — Бот-Оркестратор v7.0 (Clean Arch)")
    log.info("=" * 55)
    log.info(f"  Принтер: {PRINTER_IP} ({PRINTER_NAME})")
    log.info(f"  Разрешённый chat_id: {ALLOWED_CHAT_ID or 'все (небезопасно!)'}")

    try:
        ps = await snmp_manager.get_printer_state(PRINTER_IP, SNMP_CFG)
        log.info(f"  Принтер: {ps.status.value} | счётчик: {ps.page_count} стр.")
    except Exception as e:
        log.warning(f"  Принтер недоступен при старте: {e}")

    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass