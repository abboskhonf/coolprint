"""
web_api.py — веб-интерфейс и REST/SSE API CoolPrint.

Авторизация: токен из config.web_token() в заголовке `Authorization: Bearer …`,
`X-Token` или параметре `?token=` (нужен для SSE: EventSource не умеет заголовки).
Страница index.html отдаётся без токена, он нужен только для /api/*.
"""
import asyncio
import csv
import hmac
import json
import logging
import re
import uuid
from pathlib import Path

from aiohttp import web

import config
from engine import Engine
from jobs import JobStatus, QueueError
from state_manager import CSV_AUDIT_FILE

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_UPLOAD = 300 * 1024 * 1024
JOB_FIELDS = {"copies": int, "duplex": str, "paper": str, "booklet": str, "batch_size": int,
              "cooldown": int, "page_from": int, "page_to": int, "name": str}


def _json(data, status=200) -> web.Response:
    return web.json_response(data, status=status, dumps=lambda o: json.dumps(o, ensure_ascii=False))


def _err(msg: str, status=400) -> web.Response:
    return _json({"error": msg}, status)


def _safe_name(name: str) -> str:
    name = Path(name or "document.pdf").name
    name = re.sub(r"[^\w.\- ()а-яА-ЯёЁ]", "_", name).strip(" .") or "document.pdf"
    return name if name.lower().endswith(".pdf") else name + ".pdf"


def _coerce(raw: dict) -> dict:
    """Приводит значения формы/JSON к типам Job; пустые строки считаются «не задано»."""
    out = {}
    for k, cast in JOB_FIELDS.items():
        v = raw.get(k)
        if v is None or v == "":
            continue
        try:
            out[k] = cast(v)
        except (TypeError, ValueError):
            raise QueueError(f"Некорректное значение поля «{k}»")
    return out


@web.middleware
async def auth_mw(request: web.Request, handler):
    if request.path.startswith("/api/"):
        supplied = (request.headers.get("X-Token")
                    or request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
                    or request.query.get("token", ""))
        if not hmac.compare_digest(supplied.encode(), config.web_token().encode()):
            return _err("Нужен токен доступа", 401)
    try:
        return await handler(request)
    except QueueError as e:
        return _err(str(e), 400)
    except web.HTTPException:
        raise
    except Exception as e:
        log.exception("Ошибка в API")
        return _err(f"Внутренняя ошибка: {e}", 500)


class WebApi:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.app = web.Application(middlewares=[auth_mw], client_max_size=MAX_UPLOAD)
        r = self.app.router
        r.add_get("/", self.index)
        r.add_get("/api/status", self.status)
        r.add_get("/api/events", self.events)
        r.add_post("/api/jobs", self.add_job)
        r.add_patch("/api/jobs/{id}", self.patch_job)
        r.add_post("/api/jobs/{id}/{action}", self.job_action)
        r.add_post("/api/queue/{action}", self.queue_action)
        r.add_post("/api/cooldown/{action}", self.cooldown_action)
        r.add_post("/api/emergency-stop", self.emergency)
        r.add_get("/api/defaults", self.get_defaults)
        r.add_put("/api/defaults", self.put_defaults)
        r.add_get("/api/history", self.history)
        self.runner: web.AppRunner | None = None

    # ── Запуск ──────────────────────────────────────────────────────────────
    async def start(self, host: str, port: int) -> None:
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        await web.TCPSite(self.runner, host, port).start()
        log.info(f"Веб-интерфейс: http://{host}:{port}/")

    async def stop(self) -> None:
        if self.runner:
            await self.runner.cleanup()

    # ── Страница и состояние ────────────────────────────────────────────────
    async def index(self, request):
        return web.FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    async def status(self, request):
        return _json(self.engine.status())

    async def events(self, request):
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream",
                                           "Cache-Control": "no-cache",
                                           "X-Accel-Buffering": "no"})
        await resp.prepare(request)
        q = self.engine.hub.subscribe()

        async def send(ev):
            await resp.write(f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode())

        try:
            await send({"kind": "hello", "ts": self.engine.status()["now"]})
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=15)
                    await send(ev)
                except asyncio.TimeoutError:
                    await resp.write(b": ping\n\n")      # держим соединение через прокси/NAT
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            self.engine.hub.unsubscribe(q)
        return resp

    # ── Задания ─────────────────────────────────────────────────────────────
    async def add_job(self, request):
        if not request.content_type.startswith("multipart/"):
            raise QueueError("Ожидается multipart/form-data с файлом PDF")
        reader = await request.multipart()
        fields: dict = {}
        saved: Path | None = None
        orig = ""
        try:
            async for part in reader:
                if part.name == "file":
                    orig = _safe_name(part.filename or "")
                    saved = config.UPLOADS_DIR / f"{uuid.uuid4().hex[:8]}_{orig}"
                    head = True
                    with open(saved, "wb") as f:
                        while chunk := await part.read_chunk(1 << 20):
                            if head and not chunk.startswith(b"%PDF"):
                                raise QueueError("Это не PDF-файл")
                            head = False
                            f.write(chunk)
                else:
                    fields[part.name] = (await part.text()).strip()
            if saved is None:
                raise QueueError("Файл не передан (поле «file»)")
            opts = _coerce(fields)
            pos = fields.get("position")
            name = opts.pop("name", None) or orig
            job = self.engine.create_job(saved, name, owner="web",
                                         position=int(pos) if pos not in (None, "") else None, **opts)
        except Exception:
            if saved:
                saved.unlink(missing_ok=True)
            raise
        return _json(job.to_dict(), 201)

    async def patch_job(self, request):
        body = await request.json()
        patch = _coerce(body)
        if "recovery_skip" in body:
            patch["recovery_skip"] = max(0, int(body["recovery_skip"]))
        if patch.get("paper"):
            patch["paper"] = patch["paper"].upper()
            if patch["paper"] not in config.supported_papers():
                raise QueueError(f"Формат {patch['paper']} недоступен")
        job = self.engine.queue.update(request.match_info["id"], **patch)
        self.engine.kick()
        return _json(job.to_dict())

    async def job_action(self, request):
        q, jid, act = self.engine.queue, request.match_info["id"], request.match_info["action"]
        if act == "up":
            q.move_step(jid, -1)
        elif act == "down":
            q.move_step(jid, +1)
        elif act == "top":
            q.move(jid, 0)
        elif act == "bottom":
            q.move(jid, 10 ** 6)
        elif act == "move":
            q.move(jid, int((await request.json())["to"]))
        elif act == "hold":
            q.hold(jid)
        elif act == "release":
            q.release(jid)
        elif act == "cancel":
            self.engine.cancel(jid)
        elif act == "retry":
            q.retry(jid)
        elif act == "delete":
            job = q.remove(jid)
            self.engine._forget(job)
            if Path(job.source).parent == config.UPLOADS_DIR:
                Path(job.source).unlink(missing_ok=True)
        else:
            return _err("Неизвестное действие", 404)
        self.engine.kick()
        return _json({"ok": True})

    # ── Очередь / охлаждение ────────────────────────────────────────────────
    async def queue_action(self, request):
        act = request.match_info["action"]
        if act not in ("pause", "resume"):
            return _err("Неизвестное действие", 404)
        self.engine.queue.set_paused(act == "pause")
        return _json({"paused": self.engine.queue.paused})

    async def cooldown_action(self, request):
        act = request.match_info["action"]
        if act == "skip":
            self.engine.skip_cooldown()
        elif act == "add":
            minutes = float((await request.json()).get("minutes", 0))
            self.engine.adjust_cooldown(max(-240.0, min(240.0, minutes)))
        else:
            return _err("Неизвестное действие", 404)
        return _json({"ok": True})

    async def emergency(self, request):
        await self.engine.emergency_stop()
        return _json({"ok": True})

    # ── Настройки по умолчанию и история ────────────────────────────────────
    async def get_defaults(self, request):
        return _json({"defaults": config.defaults(), "papers": config.supported_papers()})

    async def put_defaults(self, request):
        patch = await request.json()
        try:
            return _json({"defaults": config.update_defaults(patch)})
        except (ValueError, KeyError) as e:
            raise QueueError(str(e))

    async def history(self, request):
        limit = max(1, min(500, int(request.query.get("limit", 100))))
        if not CSV_AUDIT_FILE.exists():
            return _json({"rows": []})
        with open(CSV_AUDIT_FILE, encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f, delimiter=";"))
        return _json({"rows": rows[-limit:][::-1]})
