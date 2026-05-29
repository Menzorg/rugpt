"""TaskPoll model new fields: summary + task_ids snapshot."""
from uuid import uuid4
from src.engine.models.task_poll import TaskPoll


def test_task_poll_summary_default_none():
    poll = TaskPoll()
    assert poll.summary is None


def test_task_poll_summary_set():
    poll = TaskPoll(summary="## Сводка\n- задача 1: в работе")
    assert "Сводка" in poll.summary
    assert poll.to_dict()["summary"] == poll.summary


def test_task_poll_task_ids_default_empty():
    poll = TaskPoll()
    assert poll.task_ids == []


def test_task_poll_task_ids_serializable():
    ids = [uuid4(), uuid4()]
    poll = TaskPoll(task_ids=ids)
    d = poll.to_dict()
    assert d["task_ids"] == [str(x) for x in ids]
