"""
core.py
"""
import math
import logging
import asyncio
from dataclasses import dataclass, field
from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import DEFAULT_DUPLEX, DEFAULT_COPIES, DEFAULT_BATCH, DEFAULT_COOLDOWN, ALLOWED_CHAT_ID

log = logging.getLogger(__name__)

class JobStates(StatesGroup):
    waiting_for_file    = State()
    configuring         = State()
    waiting_pages_input = State()
    waiting_copies_input = State()
    waiting_batch_input  = State()
    waiting_cool_input   = State()
    printing            = State()
    waiting_recovery_pages = State()

@dataclass
class JobConfig:
    pdf_path:    str   = ""
    pdf_name:    str   = ""
    total_pages: int   = 0
    page_from:   int   = 1
    page_to:     int   = 0
    duplex:      str   = DEFAULT_DUPLEX
    booklet:     str   = "none"
    copies:      int   = DEFAULT_COPIES
    batch_size:  int   = DEFAULT_BATCH
    cooldown:    int   = DEFAULT_COOLDOWN

    status_msg_id:  int    = 0
    current_batch:  int    = 0
    total_batches:  int    = 0
    start_index:    int    = 0
    
    stop_event:     object = field(default=None, repr=False)
    warn_shown:     bool   = False
    recovery_skip_pages: int = 0

    def page_to_real(self) -> int:
        return self.page_to if self.page_to else self.total_pages

    def pages_to_print(self) -> int:
        return self.page_to_real() - self.page_from + 1

    def sheets_per_batch(self) -> int:
        if self.booklet != "none":
            return max(1, self.batch_size // 4)
        return (self.batch_size // 2) if self.duplex != "none" else self.batch_size

    def real_load(self) -> int:
        return self.sheets_per_batch()

    def duplex_ru(self) -> str:
        return {"long": "длинная ↔️", "short": "короткая ↕️", "none": "односторонняя 📄"}.get(self.duplex, self.duplex)

    def booklet_ru(self) -> str:
        return {"none": "Нет", "left": "Слева 📖", "right": "Справа 📖"}.get(self.booklet, self.booklet)

    def summary(self) -> str:
        pages_str = f"1–{self.total_pages}" if (self.page_from == 1 and not self.page_to) else f"{self.page_from}–{self.page_to_real()}"
        total_to_print = self.pages_to_print() * self.copies
        if self.booklet != "none":
            format_info = f"📖 <b>Брошюра</b> ({self.booklet_ru().lower()})"
            total_sheets = math.ceil(total_to_print / 4)
        else:
            format_info = "📄 <b>Обычный документ</b>"
            total_sheets = math.ceil(total_to_print / 2) if self.duplex != "none" else total_to_print

        return (
            f"📄 <b>{self.pdf_name}</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"⚙️ <b>ПАРАМЕТРЫ:</b>\n"
            f"  • Режим:      {format_info}\n"
            f"  • Диапазон:   {pages_str}\n"
            f"  • Копий:      {self.copies} шт.\n"
            f"  • Итого:      {total_to_print} стр. ➡️ <b>{total_sheets} листов</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🛠 <b>ЩАДЯЩИЙ РЕЖИМ:</b>\n"
            f"  • Пачка:      {self.batch_size} стр. из файла\n"
            f"  • Пауза:      {self.cooldown} мин. отдыха\n"
        )

# Глобальные хранилища состояния
active_jobs: dict[int, JobConfig] = {}
print_tasks: dict[int, asyncio.Task] = {}

# ── Утилиты и UI ──
def _esc(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def progress_bar(current: int, total: int, width: int = 20) -> str:
    if total == 0: return "░" * width
    filled = int(width * current / total)
    return "█" * filled + "░" * (width - filled)

def cooldown_bar(remaining_sec: float, total_sec: float, width: int = 20) -> str:
    if total_sec <= 0: return "░" * width
    elapsed = total_sec - remaining_sec
    fraction = max(0.0, min(1.0, elapsed / total_sec))
    filled = int(width * fraction)
    return "█" * filled + "░" * (width - filled)

def is_allowed(chat_id: int) -> bool:
    if not ALLOWED_CHAT_ID: return True
    return str(chat_id) == ALLOWED_CHAT_ID

async def safe_edit(bot: Bot, chat_id: int, msg_id: int, text: str, reply_markup=None) -> bool:
    try:
        await bot.edit_message_text(
            chat_id=chat_id, message_id=msg_id, text=text,
            parse_mode=ParseMode.HTML, reply_markup=reply_markup
        )
        return True
    except TelegramBadRequest as e:
        if "message is not modified" in str(e): return True
        log.warning(f"edit_message_text: {e}")
        return False

# ── Клавиатуры ──
def kb_main_config(job: JobConfig) -> InlineKeyboardMarkup:
    buttons = []
    b_none  = "📄 Обычный ✅" if job.booklet == "none" else "📄 Обычный"
    b_left  = "📖 Брошюра (Слева) ✅" if job.booklet == "left" else "📖 Брошюра (Слева)"
    b_right = "📖 Брошюра (Справа) ✅" if job.booklet == "right" else "📖 Брошюра (Справа)"
    
    buttons.append([InlineKeyboardButton(text=b_none, callback_data="booklet:none")])
    buttons.append([
        InlineKeyboardButton(text=b_left, callback_data="booklet:left"),
        InlineKeyboardButton(text=b_right, callback_data="booklet:right"),
    ])
    
    if job.booklet == "none":
        d_long  = "↔️ Длинная ✅" if job.duplex == "long" else "↔️ Длинная"
        d_short = "↕️ Короткая ✅" if job.duplex == "short" else "↕️ Короткая"
        d_none  = "📄 Одностор. ✅" if job.duplex == "none" else "📄 Одностор."
        buttons.append([
            InlineKeyboardButton(text=d_long,  callback_data="dup:long"),
            InlineKeyboardButton(text=d_short, callback_data="dup:short"),
            InlineKeyboardButton(text=d_none,  callback_data="dup:none"),
        ])
        
    page_label = f"📑 Страницы: все" if (job.page_from == 1 and not job.page_to) else f"📑 Страницы: {job.page_from}–{job.page_to_real()}"
    
    buttons.append([
        InlineKeyboardButton(text=page_label, callback_data="pages:all"),
        InlineKeyboardButton(text="✏️ Диапазон", callback_data="pages:custom"),
    ])
    buttons.append([
        InlineKeyboardButton(text="➖", callback_data="cop:-1"),
        InlineKeyboardButton(text=f"📑 Копий: {job.copies}", callback_data="cop:show"),
        InlineKeyboardButton(text="➕", callback_data="cop:+1"),
        InlineKeyboardButton(text="✏️", callback_data="cop:input"),
    ])
    buttons.append([
        InlineKeyboardButton(text="➖", callback_data="bat:-8"),
        InlineKeyboardButton(text=f"📦 Пачка: {job.batch_size} стр.", callback_data="bat:show"),
        InlineKeyboardButton(text="➕", callback_data="bat:+8"),
        InlineKeyboardButton(text="✏️", callback_data="bat:input"),
    ])
    buttons.append([
        InlineKeyboardButton(text="➖", callback_data="cool:-5"),
        InlineKeyboardButton(text=f"🌡 Пауза: {job.cooldown} мин", callback_data="cool:show"),
        InlineKeyboardButton(text="➕", callback_data="cool:+5"),
        InlineKeyboardButton(text="✏️", callback_data="cool:input"),
    ])
    
    buttons.append([InlineKeyboardButton(text="🚀 Запустить печать", callback_data="start")])
    buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def kb_printing(has_active: bool) -> InlineKeyboardMarkup:
    buttons = []
    if has_active:
        buttons.append([InlineKeyboardButton(text="⏸ Остановить после пачки", callback_data="stop_print")])
    buttons.append([InlineKeyboardButton(text="🔄 Обновить статус", callback_data="refresh_status")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def kb_cancel_input() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="↩️ Назад", callback_data="back_to_config"),
    ]])