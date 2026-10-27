"""
telegram_ui.py — Telegram-бот для удалённого управления CoolPrint.

Роли: admin ([telegram] chat_id) — полный доступ; viewer ([telegram] viewer_ids) —
только просмотр и уведомления. Остальным бот не отвечает.

Сеть нестабильна (в bot.log были сбои DNS), поэтому и опрос, и отправка
уведомлений повторяются с задержкой, а не роняют службу.
"""
import asyncio
import html
import logging
import time
import uuid
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton as Btn, InlineKeyboardMarkup, Message

import config
from engine import Engine
from jobs import Job, JobStatus, QueueError

log = logging.getLogger(__name__)

TG_DOWNLOAD_LIMIT = 20 * 1024 * 1024        # потолок Bot API для getFile
PENDING_TTL = 1800
LIVE_TTL = 900
PHASES = {"idle": "простой", "paused": "пауза", "preparing": "подготовка", "cooling": "охлаждение",
          "waiting_printer": "ждём принтер", "sending": "отправка", "printing": "печать"}
PSTATE = {"idle": "🟢 готов", "sleep": "🟢 спит", "printing": "🖨 печатает", "warmup": "🟡 прогрев",
          "offline": "🔴 нет связи", "error": "🔴 ошибка", "warning": "🟠 внимание", "unknown": "⚪ неизвестно"}
JSTATE = {"queued": "⏳", "running": "🖨", "held": "⏸", "done": "✅", "failed": "❌", "cancelled": "🚫"}

esc = html.escape


def kb(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def fmt_dur(sec: float) -> str:
    sec = max(0, int(sec))
    h, m = sec // 3600, sec % 3600 // 60
    return f"{h} ч {m:02d} мин" if h else f"{m} мин {sec % 60:02d} с"


def short(name: str, n: int = 28) -> str:
    return name if len(name) <= n else name[:n - 1] + "…"


class TelegramUI:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.hub = engine.hub
        self.admins = set(config.ADMIN_CHAT_IDS)
        self.viewers = set(config.VIEWER_CHAT_IDS)
        session = AiohttpSession(timeout=config.DOWNLOAD_TIMEOUT)
        self.bot = Bot(config.BOT_TOKEN, session=session,
                       default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        self.dp = Dispatcher()
        self.pending: dict[str, dict] = {}              # id -> параметры загруженного, но не поставленного файла
        self.live: dict[int, tuple[int, float]] = {}    # chat_id -> (message_id, начало)
        self.quiet: set[int] = set()                    # без сообщений о каждой пачке
        self._tasks: list[asyncio.Task] = []
        self._alerted: dict[str, float] = {}
        self._register()

    # ── Жизненный цикл ──────────────────────────────────────────────────────
    async def start(self) -> None:
        self._tasks = [asyncio.create_task(self._poll_forever(), name="tg-poll"),
                       asyncio.create_task(self._notify_loop(), name="tg-notify"),
                       asyncio.create_task(self._live_loop(), name="tg-live")]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        try:
            await self.bot.session.close()
        except Exception:
            pass

    async def _poll_forever(self) -> None:
        delay = 5
        while True:
            try:
                await self.dp.start_polling(self.bot, handle_signals=False, close_bot_session=False)
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning(f"Telegram: опрос прерван ({e}); повтор через {delay} с")
                await asyncio.sleep(delay)
                delay = min(delay * 2, 120)

    # ── Права ───────────────────────────────────────────────────────────────
    def role(self, chat_id: int) -> str | None:
        return "admin" if chat_id in self.admins else "viewer" if chat_id in self.viewers else None

    async def _deny_viewer(self, event) -> bool:
        txt = "Только просмотр: у этого чата нет прав на управление."
        if isinstance(event, CallbackQuery):
            await event.answer(txt, show_alert=True)
        else:
            await event.answer(txt)
        return True

    # ── Тексты ──────────────────────────────────────────────────────────────
    def status_text(self) -> str:
        s = self.engine.status()
        p, cd, b = s["printer"], s["cooldown"], s["batch"]
        lines = [f"<b>CoolPrint</b> · {'⏸ очередь на паузе' if s['paused'] else PHASES.get(s['phase'], s['phase'])}",
                 f"Принтер: {PSTATE.get(p['status'], p['status'])} · счётчик {p['page_count']}"]
        if p["error"]:
            lines.append(f"⚠️ {esc(p['error'])}")
        if cd["remaining"] > 0:
            lines.append(f"❄️ Охлаждение: ещё {fmt_dur(cd['remaining'])}")
        cur = next((j for j in s["jobs"] if j["id"] == s["current"]), None)
        if cur and b["index"]:
            lines.append(f"📄 {esc(short(cur['name'], 40))} — пачка {b['index']}/{b['total']}"
                         + (f", {b['printed']}/{b['expected']} стр." if b["expected"] else "") + f" · {cur['paper']}")
        act = [j for j in s["jobs"] if j["status"] in JobStatus.ACTIVE]
        lines.append(f"В очереди: {len(act)}")
        etas = [j["eta"] for j in act if j.get("eta")]
        if etas:
            lines.append(f"Ориентировочно всё закончится: {time.strftime('%d.%m %H:%M', time.localtime(max(etas)))}")
        return "\n".join(lines)

    def status_kb(self) -> InlineKeyboardMarkup:
        paused = self.engine.queue.paused
        return kb([[Btn(text="🔄 Обновить", callback_data="s:refresh"),
                    Btn(text="▶️ Продолжить" if paused else "⏸ Пауза", callback_data="s:toggle")],
                   [Btn(text="⏭ Пропустить охлаждение", callback_data="s:skip"),
                    Btn(text="📋 Очередь", callback_data="q:list")]])

    def queue_view(self) -> tuple[str, InlineKeyboardMarkup]:
        act = self.engine.queue.active()
        if not act:
            return "Очередь пуста. Пришлите PDF, чтобы добавить задание.", kb([])
        eta = self.engine.eta()
        lines, rows = ["<b>Очередь</b>"], []
        for i, j in enumerate(act):
            prog = f" {j.batches_done}/{j.batches_total}" if j.batches_total else ""
            e = f" · ≈{time.strftime('%H:%M', time.localtime(eta[j.id]))}" if j.id in eta else ""
            lines.append(f"{i + 1}. {JSTATE[j.status]} {esc(short(j.name, 34))} · {j.paper} · "
                         f"{j.pages_selected()} стр.{prog}{e}")
            rows.append([Btn(text=f"{i + 1}. {short(j.name, 16)}", callback_data=f"j:{j.id}"),
                         Btn(text="⬆", callback_data=f"ja:{j.id}:up"), Btn(text="⬇", callback_data=f"ja:{j.id}:down")])
        rows.append([Btn(text="🔄 Обновить", callback_data="q:list")])
        return "\n".join(lines), kb(rows)

    def job_view(self, j: Job) -> tuple[str, InlineKeyboardMarkup]:
        started = j.batches_done > 0 or j.status == JobStatus.RUNNING
        text = (f"<b>{esc(j.name)}</b>\n{JSTATE[j.status]} {j.status} · {j.paper} · дуплекс {j.duplex}"
                + (f" · брошюра {j.booklet}" if j.booklet != "none" else "")
                + f"\nСтраниц: {j.pages_selected()} × {j.copies} · пачка {j.batch_size} · пауза {j.cooldown} мин"
                + (f"\nПрогресс: {j.batches_done}/{j.batches_total}" if j.batches_total else "")
                + (f"\n⚠️ {esc(j.error)}" if j.error else ""))
        rows = []
        if j.status in JobStatus.ACTIVE:
            if not started:
                rows.append([Btn(text=("✅ " if j.paper == p else "") + p, callback_data=f"ps:{j.id}:{p}")
                             for p in self.engine_papers()])
            rows.append([Btn(text="⏫ Первым", callback_data=f"ja:{j.id}:top"),
                         Btn(text="▶️ Вернуть" if j.status == JobStatus.HELD else "⏸ Удержать",
                             callback_data=f"ja:{j.id}:{'release' if j.status == JobStatus.HELD else 'hold'}")])
            rows.append([Btn(text="🚫 Отменить", callback_data=f"ja:{j.id}:cancel")])
        elif j.status in (JobStatus.FAILED, JobStatus.CANCELLED):
            rows.append([Btn(text="🔁 Повторить", callback_data=f"ja:{j.id}:retry")])
        rows.append([Btn(text="« Очередь", callback_data="q:list")])
        return text, kb(rows)

    def engine_papers(self) -> list[str]:
        return config.supported_papers()

    def settings_view(self) -> tuple[str, InlineKeyboardMarkup]:
        d = config.defaults()
        text = (f"<b>Параметры новых заданий</b>\nБумага {d['paper']} · дуплекс {d['duplex']} · копии {d['copies']}\n"
                f"Пачка {d['batch_size']} стр. · пауза {d['cooldown']} мин")
        rows = [[Btn(text=("✅ " if d["paper"] == p else "") + p, callback_data=f"set:paper:{p}") for p in self.engine_papers()],
                [Btn(text=("✅ " if d["duplex"] == x else "") + x, callback_data=f"set:duplex:{x}") for x in ("long", "short", "none")],
                [Btn(text="Пауза −5", callback_data="set:cooldown:-5"), Btn(text="Пауза +5", callback_data="set:cooldown:5")],
                [Btn(text="Пачка −10", callback_data="set:batch_size:-10"), Btn(text="Пачка +10", callback_data="set:batch_size:10")]]
        return text, kb(rows)

    def pending_view(self, pid: str) -> tuple[str, InlineKeyboardMarkup]:
        p = self.pending[pid]
        o = p["opts"]
        text = (f"📄 <b>{esc(p['name'])}</b> — {p['total']} стр.\n"
                f"Бумага {o['paper']} · дуплекс {o['duplex']} · копии {o['copies']}"
                + (f" · брошюра {o['booklet']}" if o["booklet"] != "none" else "")
                + f"\nПачка {o['batch_size']} стр. · пауза {o['cooldown']} мин")
        mark = lambda cur, v: ("✅ " if cur == v else "") + str(v)
        rows = [[Btn(text=mark(o["paper"], x), callback_data=f"pp:{pid}:paper:{x}") for x in self.engine_papers()],
                [Btn(text=mark(o["duplex"], x), callback_data=f"pp:{pid}:duplex:{x}") for x in ("long", "short", "none")],
                [Btn(text="−", callback_data=f"pp:{pid}:copies:-1"), Btn(text=f"Копии: {o['copies']}", callback_data="noop"),
                 Btn(text="+", callback_data=f"pp:{pid}:copies:1")],
                [Btn(text=("✅ " if o["booklet"] == x else "") + lbl, callback_data=f"pp:{pid}:booklet:{x}")
                 for x, lbl in (("none", "Обычная"), ("left", "Брошюра L"), ("right", "Брошюра R"))],
                [Btn(text="➕ В конец очереди", callback_data=f"pp:{pid}:go:end"),
                 Btn(text="⏫ В начало", callback_data=f"pp:{pid}:go:front")],
                [Btn(text="✖ Отмена", callback_data=f"pp:{pid}:go:cancel")]]
        return text, kb(rows)

    # ── Обработчики ─────────────────────────────────────────────────────────
    def _register(self) -> None:
        r = Router()
        self.dp.include_router(r)

        @self.dp.update.outer_middleware()
        async def gate(handler, event, data):
            msg = event.message or event.callback_query
            chat = (msg.message.chat if isinstance(msg, CallbackQuery) and msg.message else getattr(msg, "chat", None)) if msg else None
            if chat is None:
                return
            role = self.role(chat.id)
            if role is None:
                log.warning(f"Telegram: чужой чат {chat.id} отклонён")
                return
            data["role"] = role
            return await handler(event, data)

        @r.message(Command("start", "help"))
        async def _help(m: Message, role: str):
            await m.answer(
                "<b>CoolPrint</b> — управление печатью Canon iR2425.\n\n"
                "📎 Пришлите PDF — добавлю в очередь (выберите бумагу A4/A3, дуплекс, копии, брошюру).\n"
                "/status — состояние и охлаждение\n/queue — очередь, порядок, отмена\n"
                "/pause, /resume — пауза очереди\n/skip — пропустить охлаждение\n"
                "/settings — параметры новых заданий (бумага, пауза, пачка)\n"
                "/stop — аварийная остановка\n/quiet — не присылать сообщения по каждой пачке\n"
                f"\nРоль: {'администратор' if role == 'admin' else 'просмотр'}. "
                "Файлы больше 20 МБ загружайте через веб-интерфейс.")

        @r.message(Command("status"))
        async def _status(m: Message, role: str):
            sent = await m.answer(self.status_text(), reply_markup=self.status_kb() if role == "admin" else None)
            self.live[m.chat.id] = (sent.message_id, time.time())

        @r.message(Command("queue"))
        async def _queue(m: Message, role: str):
            text, markup = self.queue_view()
            await m.answer(text, reply_markup=markup if role == "admin" else None)

        @r.message(Command("pause", "resume"))
        async def _pause(m: Message, role: str):
            if role != "admin":
                return await self._deny_viewer(m)
            paused = m.text.lstrip("/").split("@")[0].startswith("pause")
            self.engine.queue.set_paused(paused)
            await m.answer("⏸ Очередь на паузе (текущая пачка допечатается)." if paused else "▶️ Очередь возобновлена.")

        @r.message(Command("skip"))
        async def _skip(m: Message, role: str):
            if role != "admin":
                return await self._deny_viewer(m)
            await m.answer("Пропустить охлаждение? Фьюзер может перегреться.",
                           reply_markup=kb([[Btn(text="Да, пропустить", callback_data="c:skip"),
                                             Btn(text="Нет", callback_data="c:no")]]))

        @r.message(Command("stop"))
        async def _stop(m: Message, role: str):
            if role != "admin":
                return await self._deny_viewer(m)
            await m.answer("🛑 Остановить печать, очистить спулер и поставить очередь на паузу?",
                           reply_markup=kb([[Btn(text="Да, остановить", callback_data="c:stop"),
                                             Btn(text="Нет", callback_data="c:no")]]))

        @r.message(Command("settings"))
        async def _settings(m: Message, role: str):
            if role != "admin":
                return await self._deny_viewer(m)
            text, markup = self.settings_view()
            await m.answer(text, reply_markup=markup)

        @r.message(Command("quiet"))
        async def _quiet(m: Message, role: str):
            if m.chat.id in self.quiet:
                self.quiet.discard(m.chat.id)
                await m.answer("🔔 Буду сообщать о каждой пачке.")
            else:
                self.quiet.add(m.chat.id)
                await m.answer("🔕 Только начало, конец и проблемы.")

        @r.message(F.document)
        async def _doc(m: Message, role: str):
            if role != "admin":
                return await self._deny_viewer(m)
            doc = m.document
            if not (doc.file_name or "").lower().endswith(".pdf") and doc.mime_type != "application/pdf":
                return await m.answer("Нужен PDF-файл.")
            if doc.file_size and doc.file_size > TG_DOWNLOAD_LIMIT:
                return await m.answer("Файл больше 20 МБ — Telegram не отдаёт такие боту. Загрузите через веб-интерфейс.")
            self._purge_pending()
            pid = uuid.uuid4().hex[:6]
            dest = config.UPLOADS_DIR / f"{pid}_{Path(doc.file_name or 'document.pdf').name}"
            note = await m.answer("⏬ Скачиваю…")
            try:
                await self.bot.download(doc, destination=dest)
                from pdf_processor import get_total_pages
                total = await asyncio.to_thread(get_total_pages, str(dest))
                if total <= 0:
                    raise ValueError("не удалось прочитать PDF")
            except Exception as e:
                dest.unlink(missing_ok=True)
                return await note.edit_text(f"❌ Не получилось принять файл: {esc(str(e))}")
            d = config.defaults()
            self.pending[pid] = {"path": dest, "name": doc.file_name or dest.name, "total": total,
                                 "chat": m.chat.id, "ts": time.time(),
                                 "opts": {k: d[k] for k in ("paper", "duplex", "copies", "booklet", "batch_size", "cooldown")}}
            text, markup = self.pending_view(pid)
            await note.edit_text(text, reply_markup=markup)

        @r.callback_query(F.data == "noop")
        async def _noop(c: CallbackQuery, role: str):
            await c.answer()

        @r.callback_query(F.data.startswith(("s:", "q:", "j:", "ja:", "ps:", "c:", "set:", "pp:")))
        async def _cb(c: CallbackQuery, role: str):
            kind = c.data.split(":", 1)[0]
            read_only = c.data in ("s:refresh", "q:list") or (kind == "j")
            if role != "admin" and not read_only:
                return await self._deny_viewer(c)
            try:
                await self._dispatch(c)
            except QueueError as e:
                await c.answer(str(e), show_alert=True)
            except TelegramBadRequest as e:
                if "not modified" not in str(e):
                    log.warning(f"Telegram edit: {e}")
                await c.answer()

    async def _dispatch(self, c: CallbackQuery) -> None:
        parts = c.data.split(":")
        kind, eng = parts[0], self.engine
        edit = lambda text, markup=None: c.message.edit_text(text, reply_markup=markup)

        if kind == "s":
            if parts[1] == "toggle":
                eng.queue.set_paused(not eng.queue.paused)
            elif parts[1] == "skip":
                return await edit("Пропустить охлаждение? Фьюзер может перегреться.",
                                  kb([[Btn(text="Да, пропустить", callback_data="c:skip"), Btn(text="Нет", callback_data="c:no")]]))
            await edit(self.status_text(), self.status_kb() if self.role(c.message.chat.id) == "admin" else None)
            await c.answer("Обновлено")
        elif kind == "q":
            text, markup = self.queue_view()
            await edit(text, markup if self.role(c.message.chat.id) == "admin" else None)
            await c.answer()
        elif kind == "j":
            text, markup = self.job_view(eng.queue.get(parts[1]))
            await edit(text, markup)
            await c.answer()
        elif kind == "ja":
            jid, act = parts[1], parts[2]
            q = eng.queue
            if act == "up":
                q.move_step(jid, -1)
            elif act == "down":
                q.move_step(jid, +1)
            elif act == "top":
                q.move(jid, 0)
            elif act == "hold":
                q.hold(jid)
            elif act == "release":
                q.release(jid)
            elif act == "cancel":
                eng.cancel(jid)
            elif act == "retry":
                q.retry(jid)
            eng.kick()
            if act in ("up", "down"):
                text, markup = self.queue_view()
            else:
                text, markup = self.job_view(q.get(jid))
            await edit(text, markup)
            await c.answer("Готово")
        elif kind == "ps":
            eng.check_queue(parts[2], eng.queue.get(parts[1]).duplex)
            job = eng.queue.update(parts[1], paper=parts[2])
            await edit(*self.job_view(job))
            await c.answer(f"Бумага: {parts[2]}")
        elif kind == "c":
            if parts[1] == "skip":
                eng.skip_cooldown()
                await edit("⏭ Охлаждение пропущено.")
            elif parts[1] == "stop":
                await eng.emergency_stop()
                await edit("🛑 Остановлено: спулер очищен, очередь на паузе. Продолжить: /resume")
            else:
                await edit("Отменено.")
            await c.answer()
        elif kind == "set":
            key, val = parts[1], parts[2]
            if key in ("cooldown", "batch_size"):
                val = config.defaults()[key] + int(val)
            config.update_defaults({key: val})
            await edit(*self.settings_view())
            await c.answer("Сохранено")
        elif kind == "pp":
            await self._pending_cb(c, parts)

    async def _pending_cb(self, c: CallbackQuery, parts: list[str]) -> None:
        pid, key, val = parts[1], parts[2], parts[3]
        p = self.pending.get(pid)
        if not p:
            return await c.answer("Файл устарел, пришлите его заново.", show_alert=True)
        o = p["opts"]
        if key == "go":
            self.pending.pop(pid)
            if val == "cancel":
                p["path"].unlink(missing_ok=True)
                await c.message.edit_text("Отменено, файл удалён.")
                return await c.answer()
            try:
                job = self.engine.create_job(p["path"], p["name"], owner=str(c.message.chat.id),
                                             position=0 if val == "front" else None, **o)
            except QueueError as e:
                p["path"].unlink(missing_ok=True)
                await c.message.edit_text(f"❌ {esc(str(e))}")
                return await c.answer()
            pos = [j.id for j in self.engine.queue.active()].index(job.id) + 1
            await c.message.edit_text(f"✅ <b>{esc(job.name)}</b> добавлен: позиция {pos}, {job.paper}, "
                                      f"{job.pages_selected()} стр. × {job.copies}.")
            return await c.answer("Добавлено")
        if key == "copies":
            o["copies"] = max(1, min(99, o["copies"] + int(val)))
        elif key == "booklet":
            o["booklet"] = val
            if val != "none":
                o["duplex"] = "short"
        elif key == "duplex":
            o["duplex"] = val
            if val != "short":
                o["booklet"] = "none"
        else:
            o[key] = val
        await c.message.edit_text(*self.pending_view(pid))
        await c.answer()

    def _purge_pending(self) -> None:
        now = time.time()
        for pid in [k for k, v in self.pending.items() if now - v["ts"] > PENDING_TTL]:
            self.pending.pop(pid)["path"].unlink(missing_ok=True)

    # ── Уведомления ─────────────────────────────────────────────────────────
    async def _send(self, chat_id: int, text: str) -> None:
        for attempt in range(4):
            try:
                await self.bot.send_message(chat_id, text)
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if attempt == 3:
                    log.warning(f"Telegram: сообщение для {chat_id} не доставлено: {e}")
                    return
                await asyncio.sleep(5 * 2 ** attempt)

    def _broadcast(self, text: str, verbose: bool = False) -> None:
        for cid in self.admins | self.viewers:
            if verbose and cid in self.quiet:
                continue
            asyncio.create_task(self._send(cid, text))

    async def _notify_loop(self) -> None:
        q = self.hub.subscribe()
        try:
            while True:
                ev = await q.get()
                try:
                    self._on_event(ev)
                except Exception:
                    log.exception("Telegram: ошибка обработки события")
        finally:
            self.hub.unsubscribe(q)

    def _on_event(self, ev: dict) -> None:
        k = ev["kind"]
        j = ev.get("job") or {}
        name = esc(short(j.get("name", ""), 40))
        if k == "job_started":
            self._broadcast(f"🖨 Начата печать: <b>{name}</b> ({j['paper']}, {j['pages_selected']} стр.)")
        elif k == "batch_done":
            cd = ev.get("cooldown", 0) / (self.engine.minute or 1)
            tail = f"\n❄️ Охлаждение {cd:.0f} мин" if j["batches_done"] < j["batches_total"] else ""
            self._broadcast(f"📦 {name}: пачка {j['batches_done']}/{j['batches_total']} готова{tail}", verbose=True)
        elif k == "job_done":
            self._broadcast(f"✅ Готово: <b>{name}</b> — напечатано {j['pages_printed']} стр. по счётчику")
        elif k == "job_failed":
            self._broadcast(f"❌ Ошибка: <b>{name}</b>\n{esc(ev['error'])}\n"
                            f"Прогресс {j['batches_done']}/{j['batches_total']} сохранён. /queue → Повторить")
        elif k == "job_cancelled":
            self._broadcast(f"🚫 Отменено: <b>{name}</b>")
        elif k == "halted":
            self._broadcast(f"⏸ {esc(ev['reason'])}. Очередь на паузе: /resume после проверки принтера.")
        elif k == "printer_alert":
            key = f"alert:{ev['status']}"
            if time.time() - self._alerted.get(key, 0) > 600:
                self._alerted[key] = time.time()
                self._broadcast(f"⚠️ Принтер: {PSTATE.get(ev['status'], ev['status'])}"
                                + (f" — {esc(ev['error'])}" if ev.get("error") else "")
                                + "\nПечать на паузе, ожидание устранения проблемы.")
        elif k == "printer_recovered":
            self._alerted.clear()
            self._broadcast("🟢 Принтер снова в норме, печать продолжается.")

    async def _live_loop(self) -> None:
        """Обновляет сообщения /status, пока они «живые» (LIVE_TTL), без спама новыми."""
        while True:
            await asyncio.sleep(10)
            for cid, (mid, t0) in list(self.live.items()):
                if time.time() - t0 > LIVE_TTL:
                    self.live.pop(cid, None)
                    continue
                try:
                    await self.bot.edit_message_text(
                        self.status_text(), chat_id=cid, message_id=mid,
                        reply_markup=self.status_kb() if cid in self.admins else None)
                except TelegramBadRequest as e:
                    if "not modified" not in str(e):
                        self.live.pop(cid, None)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass
