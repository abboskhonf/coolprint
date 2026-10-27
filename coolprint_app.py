"""
coolprint_app.py — точка входа CoolPrint: движок + веб-интерфейс + Telegram.

Запуск вручную:  python coolprint_app.py
Как служба Windows:  windows_service.py (вызывает run_app в отдельном потоке).
"""
import asyncio
import logging
import logging.handlers
from typing import Optional

import config
from engine import Engine
from jobs import EventHub, JobQueue
from printer_backend import make_backend
from web_api import WebApi

log = logging.getLogger("coolprint")


def setup_logging() -> None:
    root = logging.getLogger()
    if root.handlers:
        return
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    fh = logging.handlers.RotatingFileHandler(config.DATA_DIR / "coolprint.log", maxBytes=2_000_000,
                                              backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.setLevel(logging.INFO)
    root.addHandler(fh)
    root.addHandler(sh)
    logging.getLogger("aiogram").setLevel(logging.WARNING)


async def run_app(stop: Optional[asyncio.Event] = None) -> None:
    """Работает, пока не сработает `stop` (или не придёт Ctrl+C)."""
    stop = stop or asyncio.Event()
    hub = EventHub()
    queue = JobQueue(config.DATA_DIR / "queue.json", hub, config.HISTORY_DAYS)
    backend = make_backend()
    engine = Engine(queue, backend)
    web = WebApi(engine)
    bot = None

    await engine.start()
    if config.WEB_ENABLED:
        try:
            await web.start(config.WEB_HOST, config.WEB_PORT)
            log.info(f"Токен веб-доступа: {config.DATA_DIR / 'web_token.txt'} (или [web] token)")
        except OSError as e:
            log.error(f"Веб-интерфейс не запущен ({config.WEB_HOST}:{config.WEB_PORT}): {e}")
    if config.BOT_TOKEN and config.ADMIN_CHAT_IDS:
        from telegram_ui import TelegramUI
        bot = TelegramUI(engine)
        await bot.start()
    else:
        log.info("Telegram отключён: нет [telegram] token / chat_id")
    log.info(f"CoolPrint запущен ({backend.name}), заданий в очереди: {len(queue.active())}")

    try:
        await stop.wait()
    finally:
        log.info("Остановка…")
        if bot:
            await bot.stop()
        await web.stop()
        await engine.stop()


def main() -> None:
    setup_logging()
    try:
        asyncio.run(run_app())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
