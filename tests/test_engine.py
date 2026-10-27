import asyncio
import time

import pytest

from engine import Engine
from jobs import JobQueue, JobStatus, QueueError
from printer_backend import SimulatedPrinter
from snmp_manager import PrinterStatus


def build(tmp_path, **kw):
    q = JobQueue(tmp_path / "q.json")
    be = SimulatedPrinter(speed=500, a3_weight=kw.pop("a3_weight", 1))
    eng = Engine(q, be, poll_sec=0.02, idle_poll_sec=0.1, lost_job_sec=kw.pop("lost", 0.5),
                 watchdog_sec=kw.pop("watchdog", 5), minute=kw.pop("minute", 0.05),
                 work_dir=tmp_path / "work", state_file=tmp_path / "es.json")
    return q, be, eng


async def wait_for(cond, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("timeout")


async def test_job_runs_in_batches_with_cooldown(tmp_path, pdf):
    q, be, eng = build(tmp_path)
    await eng.start()
    try:
        job = eng.create_job(pdf(10), "d", batch_size=4, cooldown=2, duplex="none")
        await wait_for(lambda: job.status == JobStatus.DONE)
    finally:
        await eng.stop()
    assert job.batches_total == 3 and job.batches_done == 3
    assert job.pages_printed == 10
    assert sum(s[3] for s in be.sent) == 10
    assert eng.cooldown_until > 0              # cooldown стартует и после последней пачки


async def test_global_cooldown_between_jobs(tmp_path, pdf):
    q, be, eng = build(tmp_path, minute=0.2)
    await eng.start()
    try:
        a = eng.create_job(pdf(4, "a.pdf"), "a", batch_size=4, cooldown=1, duplex="none")
        b = eng.create_job(pdf(4, "b.pdf"), "b", batch_size=4, cooldown=1, duplex="none")
        await wait_for(lambda: a.status == JobStatus.DONE)
        t_a = time.time()
        await wait_for(lambda: b.status == JobStatus.DONE)
    finally:
        await eng.stop()
    assert b.finished - a.finished >= 0.15


async def test_reorder_takes_effect_at_batch_boundary(tmp_path, pdf):
    q, be, eng = build(tmp_path, minute=0.1)
    await eng.start()
    try:
        a = eng.create_job(pdf(6, "a.pdf"), "a", batch_size=2, cooldown=1, duplex="none")
        b = eng.create_job(pdf(2, "b.pdf"), "b", batch_size=2, cooldown=1, duplex="none")
        await wait_for(lambda: a.batches_done >= 1)
        q.move(b.id, 0)
        await wait_for(lambda: b.status == JobStatus.DONE)
        assert a.status != JobStatus.DONE        # b обогнал a
        await wait_for(lambda: a.status == JobStatus.DONE)
    finally:
        await eng.stop()
    assert a.batches_done == 3


async def test_pause_blocks_next_batch(tmp_path, pdf):
    q, be, eng = build(tmp_path, minute=0.1)
    await eng.start()
    try:
        a = eng.create_job(pdf(6), "a", batch_size=2, cooldown=1, duplex="none")
        await wait_for(lambda: a.batches_done >= 1)
        q.set_paused(True)
        await asyncio.sleep(0.5)
        done = a.batches_done
        await asyncio.sleep(0.4)
        assert a.batches_done == done and a.status != JobStatus.DONE
        q.set_paused(False)
        await wait_for(lambda: a.status == JobStatus.DONE)
    finally:
        await eng.stop()


async def test_skip_cooldown(tmp_path, pdf):
    q, be, eng = build(tmp_path, minute=60)       # 1 мин охлаждения = 60 с
    await eng.start()
    try:
        a = eng.create_job(pdf(4), "a", batch_size=2, cooldown=1, duplex="none")
        await wait_for(lambda: a.batches_done == 1)
        await asyncio.sleep(0.3)
        assert a.batches_done == 1 and eng.phase == "cooling"
        eng.skip_cooldown()
        await wait_for(lambda: a.status == JobStatus.DONE)
    finally:
        await eng.stop()


async def test_send_failure_halts_queue(tmp_path, pdf, monkeypatch):
    import config
    monkeypatch.setattr(config, "ON_FAILURE", "halt")
    q, be, eng = build(tmp_path)
    be.fail_next_send = 1
    await eng.start()
    try:
        a = eng.create_job(pdf(4), "a", batch_size=4, cooldown=0, duplex="none")
        await wait_for(lambda: a.status == JobStatus.FAILED)
    finally:
        await eng.stop()
    assert q.paused and "спулер" in a.error


async def test_lost_job_detected_and_retry_completes(tmp_path, pdf, monkeypatch):
    import config
    monkeypatch.setattr(config, "ON_FAILURE", "continue")
    q, be, eng = build(tmp_path, lost=0.3)
    be.drop_next = 1
    await eng.start()
    try:
        a = eng.create_job(pdf(4), "a", batch_size=4, cooldown=0, duplex="none")
        await wait_for(lambda: a.status == JobStatus.FAILED)
        assert "потеряно" in a.error
        q.retry(a.id)
        await wait_for(lambda: a.status == JobStatus.DONE)
    finally:
        await eng.stop()
    assert a.pages_printed == 4


async def test_watchdog_frozen_while_printer_error(tmp_path, pdf):
    q, be, eng = build(tmp_path, watchdog=0.6)
    be.speed = 10                                  # успеть поймать печать
    await eng.start()
    try:
        a = eng.create_job(pdf(4), "a", batch_size=4, cooldown=0, duplex="none")
        await wait_for(lambda: eng.phase in ("sending", "printing"))
        be.status_override = PrinterStatus.ERROR
        await asyncio.sleep(1.0)                  # дольше watchdog
        assert a.status == JobStatus.RUNNING
        be.status_override = None
        await wait_for(lambda: a.status == JobStatus.DONE)
    finally:
        await eng.stop()


async def test_a3_counter_weight(tmp_path, pdf):
    q, be, eng = build(tmp_path, a3_weight=2)
    import config
    orig = config.paper_counter_weight
    config.paper_counter_weight = lambda p: 2 if p == "A3" else 1
    await eng.start()
    try:
        a = eng.create_job(pdf(4), "a", batch_size=4, cooldown=0, duplex="none", paper="A3")
        await wait_for(lambda: a.status == JobStatus.DONE)
    finally:
        await eng.stop()
        config.paper_counter_weight = orig
    assert a.pages_printed == 4
    assert all(s[2] == "A3" for s in be.sent)


async def test_cancel_running_purges_and_records_progress(tmp_path, pdf):
    q, be, eng = build(tmp_path)
    be.speed = 4                                   # медленная печать
    await eng.start()
    try:
        a = eng.create_job(pdf(20), "a", batch_size=20, cooldown=0, duplex="none")
        await wait_for(lambda: eng.phase == "printing")
        await asyncio.sleep(0.5)
        eng.cancel(a.id)
        await wait_for(lambda: not a.in_flight)
    finally:
        await eng.stop()
    assert a.status == JobStatus.CANCELLED and 0 < a.recovery_skip < 20


def test_create_job_validation(tmp_path, pdf):
    q, be, eng = build(tmp_path)
    with pytest.raises(QueueError):
        eng.create_job(pdf(3), "x", paper="A5")
    with pytest.raises(QueueError):
        eng.create_job(pdf(3), "x", page_from=5, page_to=9)
    j = eng.create_job(pdf(3), "x", booklet="left", duplex="long")
    assert j.duplex == "short"
    bad = tmp_path / "bad.pdf"
    bad.write_text("nope")
    with pytest.raises(QueueError):
        eng.create_job(bad, "bad")
