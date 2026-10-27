import pytest

from jobs import Job, JobQueue, JobStatus, QueueError


def mk(name, **kw):
    return Job(name=name, source="x.pdf", total_pages=10, **kw)


def names(q):
    return [j.name for j in q.active()]


def test_add_move_and_persist(tmp_path):
    q = JobQueue(tmp_path / "q.json")
    a, b, c = (q.add(mk(n)) for n in "abc")
    q.move(c.id, 0)
    assert names(q) == ["c", "a", "b"]
    q.move_step(c.id, +1)
    assert names(q) == ["a", "c", "b"]
    q.move_step(a.id, -5)
    assert names(q) == ["a", "c", "b"]
    q2 = JobQueue(tmp_path / "q.json")
    assert [j.name for j in q2.jobs] == ["a", "c", "b"]


def test_insert_position_ignores_finished(tmp_path):
    q = JobQueue(tmp_path / "q.json")
    a, b = q.add(mk("a")), q.add(mk("b"))
    q.set_status(a.id, JobStatus.DONE)
    q.add(mk("c"), position=0)
    assert names(q) == ["c", "b"]


def test_next_runnable_respects_pause_and_hold(tmp_path):
    q = JobQueue(tmp_path / "q.json")
    a, b = q.add(mk("a")), q.add(mk("b"))
    q.hold(a.id)
    assert q.next_runnable() is b
    q.set_paused(True)
    assert q.next_runnable() is None
    q.set_paused(False)
    q.release(a.id)
    assert q.next_runnable() is a


def test_locked_fields_after_start(tmp_path):
    q = JobQueue(tmp_path / "q.json")
    a = q.add(mk("a"))
    q.update(a.id, paper="A3", cooldown=5)
    a.batches_done = 1
    with pytest.raises(QueueError):
        q.update(a.id, paper="A4")
    q.update(a.id, cooldown=9)
    assert a.cooldown == 9


def test_restart_recovery(tmp_path):
    q = JobQueue(tmp_path / "q.json")
    a, b = q.add(mk("a")), q.add(mk("b"))
    a.status, a.in_flight = JobStatus.RUNNING, True
    b.status = JobStatus.RUNNING
    q.save()
    q2 = JobQueue(tmp_path / "q.json")
    assert q2.get(a.id).status == JobStatus.HELD and q2.get(a.id).error
    assert q2.get(b.id).status == JobStatus.QUEUED


def test_retry_goes_to_tail_and_cancel_rules(tmp_path):
    q = JobQueue(tmp_path / "q.json")
    a, b = q.add(mk("a")), q.add(mk("b"))
    with pytest.raises(QueueError):
        q.retry(a.id)
    q.cancel(a.id)
    q.retry(a.id)
    assert names(q) == ["b", "a"]
    q.set_status(b.id, JobStatus.DONE)
    with pytest.raises(QueueError):
        q.cancel(b.id)
