"""
telegram_notifier.py — Telegram уведомления (v6.0 Async)
=========================================================
Использует aiohttp для неблокирующей отправки.
Установка: pip install aiohttp

Если aiohttp недоступен — автоматически откатывается на
запуск requests в asyncio.to_thread (не требует доп. библиотек).
"""

import asyncio
import logging
import time
from typing import Optional

log = logging.getLogger(__name__)

_TG_MAX_LEN    = 4096
_HTTP_TIMEOUT  = 10
_RETRY_DELAYS  = [5, 15, 30]

# Пробуем aiohttp, иначе requests в thread executor
try:
    import aiohttp
    _BACKEND = "aiohttp"
except ImportError:
    try:
        import requests as _requests
        _BACKEND = "requests"
    except ImportError:
        _BACKEND = "none"


# ── Низкоуровневая async отправка ─────────────────────────────────────────────

async def _post_aiohttp(url: str, payload: dict) -> tuple[int, dict]:
    """POST через aiohttp. Возвращает (status_code, json_body)."""
    timeout = aiohttp.ClientTimeout(total=_HTTP_TIMEOUT)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, json=payload) as resp:
            return resp.status, await resp.json(content_type=None)


async def _post_requests(url: str, payload: dict) -> tuple[int, dict]:
    """POST через requests в thread executor (не блокирует event loop)."""
    def _sync():
        r = _requests.post(url, json=payload, timeout=_HTTP_TIMEOUT)
        return r.status_code, r.json()
    return await asyncio.to_thread(_sync)


async def _post(url: str, payload: dict) -> tuple[int, dict]:
    if _BACKEND == "aiohttp":
        return await _post_aiohttp(url, payload)
    elif _BACKEND == "requests":
        return await _post_requests(url, payload)
    raise RuntimeError("Нет HTTP-библиотеки. pip install aiohttp")


# ── Notifier ──────────────────────────────────────────────────────────────────

class TelegramNotifier:
    """
    Async Telegram notifier.
    При отсутствии токена — no-op (молчит, не падает).
    """

    def __init__(self, token: str, chat_id: str):
        self.enabled  = bool(token and chat_id and _BACKEND != "none")
        self._token   = token
        self._chat_id = str(chat_id)
        self._base    = f"https://api.telegram.org/bot{token}"

        if _BACKEND == "none":
            log.warning("Нет HTTP-библиотеки (aiohttp/requests) — Telegram отключён.")
        elif not token:
            log.info("Telegram token не задан — уведомления отключены.")
        else:
            log.info(f"Telegram backend: {_BACKEND}")

    # ── Внутренняя async отправка с retry ────────────────────────────────────
    async def _send_raw(self, text: str) -> bool:
        if not self.enabled:
            return False

        if len(text) > _TG_MAX_LEN:
            text = text[:_TG_MAX_LEN - 10] + "\n…(обрезано)"

        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_notification": False,
        }
        url = f"{self._base}/sendMessage"

        for attempt, delay in enumerate([0] + _RETRY_DELAYS, 1):
            if delay:
                await asyncio.sleep(delay)
            try:
                status, data = await _post(url, payload)

                if status == 200:
                    return True

                err_code = data.get("error_code", status)
                err_desc = data.get("description", "unknown")

                if err_code == 429:
                    retry_after = data.get("parameters", {}).get("retry_after", 30)
                    log.warning(f"Telegram rate limit — жду {retry_after}с...")
                    await asyncio.sleep(retry_after)
                    continue

                if err_code in (400, 401, 403):
                    log.error(f"Telegram конфиг ошибка [{err_code}]: {err_desc}")
                    self.enabled = False
                    return False

                log.warning(f"Telegram [{err_code}] попытка {attempt}: {err_desc}")

            except asyncio.TimeoutError:
                log.warning(f"Telegram: таймаут (попытка {attempt})")
            except Exception as e:
                log.warning(f"Telegram: ошибка (попытка {attempt}): {e}")

        log.error("Telegram: не удалось отправить после всех попыток")
        return False

    # ── Публичные методы ──────────────────────────────────────────────────────

    async def send(self, text: str) -> bool:
        """
        Неблокирующая отправка: запускает задачу в фоне.
        Использовать для промежуточных уведомлений.
        """
        if not self.enabled:
            return False
        asyncio.create_task(self._send_raw(text))
        return True

    async def send_and_wait(self, text: str) -> bool:
        """
        Блокирующая отправка: ждёт подтверждения.
        Использовать для КРИТИЧНЫХ сообщений (финал, ошибка).
        """
        return await self._send_raw(text)

    async def test_connection(self) -> bool:
        """Проверяет токен через getMe."""
        if not self.enabled:
            return False
        try:
            if _BACKEND == "aiohttp":
                timeout = aiohttp.ClientTimeout(total=_HTTP_TIMEOUT)
                async with aiohttp.ClientSession(timeout=timeout) as s:
                    async with s.get(f"{self._base}/getMe") as r:
                        data = await r.json(content_type=None)
                        ok = r.status == 200
            else:
                def _sync():
                    r = _requests.get(f"{self._base}/getMe", timeout=_HTTP_TIMEOUT)
                    return r.status_code == 200, r.json()
                ok, data = await asyncio.to_thread(_sync)

            if ok:
                name = data.get("result", {}).get("username", "?")
                log.info(f"Telegram бот @{name} подключён ✅")
                return True
            log.error(f"Telegram getMe провалился: {data}")
            return False
        except Exception as e:
            log.error(f"Telegram test_connection: {e}")
            return False


# ── HTML-экранирование ────────────────────────────────────────────────────────

def _esc(text: str) -> str:
    return (str(text)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;"))


# ── Шаблоны сообщений ─────────────────────────────────────────────────────────

class PrintMessages:

    @staticmethod
    def session_start(pdf_name: str, total_batches: int, copies: int,
                      duplex: str, cooldown_min: int, total_pages: int) -> str:
        duplex_ru = {
            "long":  "двустор. длинная",
            "short": "двустор. короткая",
            "none":  "односторонняя",
        }.get(duplex, duplex)
        return (
            f"🖨 <b>Печать запущена</b>\n\n"
            f"📄 <b>Файл:</b> <code>{_esc(pdf_name)}</code>\n"
            f"📊 <b>Страниц:</b> {total_pages}\n"
            f"📦 <b>Пачек:</b> {total_batches}\n"
            f"📑 <b>Копий:</b> {copies}\n"
            f"🔄 <b>Дуплекс:</b> {duplex_ru}\n"
            f"🌡 <b>Охлаждение:</b> {cooldown_min} мин между пачками\n\n"
            f"⏳ Слежу за принтером..."
        )

    @staticmethod
    def batch_start(batch_num: int, total: int, page_range: str) -> str:
        return (
            f"▶️ <b>Пачка {batch_num}/{total}</b>\n"
            f"📄 Страницы: {_esc(page_range)}\n"
            f"🖨 Отправляю на принтер..."
        )

    @staticmethod
    def batch_done(batch_num: int, total: int, pages_printed: int,
                   next_batch_eta: Optional[str] = None) -> str:
        msg = (
            f"✅ <b>Пачка {batch_num}/{total} напечатана</b>\n"
            f"📊 Напечатано: {pages_printed} стр."
        )
        if next_batch_eta:
            msg += f"\n🌡 Охлаждение. Следующая в <b>{_esc(next_batch_eta)}</b>"
        else:
            msg += "\n🏁 Последняя пачка!"
        return msg

    @staticmethod
    def cooling_done(next_batch_num: int, total: int) -> str:
        return (
            f"❄️ Принтер остыл.\n"
            f"▶️ Запускаю пачку <b>{next_batch_num}/{total}</b>..."
        )

    @staticmethod
    def session_complete(total_batches: int, total_pages: int,
                         duration_min: int) -> str:
        h, m = divmod(duration_min, 60)
        dur = f"{h}ч {m}мин" if h else f"{m}мин"
        return (
            f"🏁 <b>Печать завершена!</b>\n\n"
            f"📦 Пачек: {total_batches}\n"
            f"📊 Страниц: {total_pages}\n"
            f"⏱ Время: {dur}\n\n"
            f"📥 Заберите документы из принтера."
        )

    @staticmethod
    def batch_failed(batch_num: int, total: int, error: str) -> str:
        return (
            f"❌ <b>Ошибка! Пачка {batch_num}/{total}</b>\n\n"
            f"🔴 <b>Причина:</b> {_esc(error)}\n\n"
            f"⚠️ Проверьте принтер и перезапустите скрипт.\n"
            f"Продолжит с этой пачки."
        )

    @staticmethod
    def printer_offline(ip: str, batch_num: int) -> str:
        return (
            f"⚠️ <b>Принтер недоступен!</b>\n\n"
            f"🌐 IP: <code>{_esc(ip)}</code>\n"
            f"📦 Пачка: {batch_num}\n\n"
            f"Проверьте сеть. Скрипт ждёт автоматически."
        )

    @staticmethod
    def paused_by_user(batch_num: int, total: int) -> str:
        return (
            f"⏸ <b>Печать приостановлена</b>\n\n"
            f"Остановлено на пачке {batch_num}/{total}.\n"
            f"Запустите снова — продолжит с этого места."
        )

    @staticmethod
    def session_resumed(batch_num: int, total: int) -> str:
        return (
            f"▶️ <b>Сессия восстановлена</b>\n"
            f"Продолжаю с пачки <b>{batch_num}/{total}</b>..."
        )

    @staticmethod
    def warning(text: str) -> str:
        return f"⚠️ <b>Предупреждение</b>\n{_esc(text)}"
