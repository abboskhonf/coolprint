"""Телеграм-интерфейс без сети: тексты, клавиатуры и обработка событий."""
import pytest

import config
from engine import Engine
from jobs import EventHub, JobQueue
from printer_backend import SimulatedPrinter


@pytest.fixture
def ui(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BOT_TOKEN", "123456:TESTTOKEN")
    monkeypatch.setattr(config, "ADMIN_CHAT_IDS", [1])
    monkeypatch.setattr(config, "VIEWER_CHAT_IDS", [2])
    import telegram_ui
    q = JobQueue(tmp_path / "q.json", EventHub())
    eng = Engine(q, SimulatedPrinter(), work_dir=tmp_path / "w", state_file=tmp_path / "s.json")
    u = telegram_ui.TelegramUI(eng)
    u.sent = []
    u._broadcast = lambda text, verbose=False: u.sent.append(text)
    return u, eng


async def test_roles_and_views(ui, pdf):
    u, eng = ui
    assert u.role(1) == "admin" and u.role(2) == "viewer" and u.role(3) is None
    assert "CoolPrint" in u.status_text()
    assert "пуста" in u.queue_view()[0]
    job = eng.create_job(pdf(), "doc.pdf", paper="A3")
    text, markup = u.queue_view()
    assert "doc.pdf" in text and "A3" in text and markup.inline_keyboard
    assert u.job_view(job)[1].inline_keyboard
    assert u.settings_view()[1].inline_keyboard
    u.pending["abc123"] = {"path": pdf(name="p.pdf"), "name": "p.pdf", "total": 10, "chat": 1, "ts": 0,
                           "opts": {k: config.defaults()[k] for k in
                                    ("paper", "duplex", "copies", "booklet", "batch_size", "cooldown")}}
    assert "p.pdf" in u.pending_view("abc123")[0]


async def test_events_notify(ui, pdf):
    u, eng = ui
    d = eng.create_job(pdf(), "doc.pdf").to_dict()
    u._on_event({"kind": "job_started", "job": d})
    u._on_event({"kind": "job_done", "job": d})
    u._on_event({"kind": "job_failed", "job": d, "error": "boom"})
    u._on_event({"kind": "printer_alert", "status": "error", "error": "Jam"})
    u._on_event({"kind": "halted", "reason": "стоп"})
    assert len(u.sent) == 5 and "boom" in u.sent[2] and "Jam" in u.sent[3]
