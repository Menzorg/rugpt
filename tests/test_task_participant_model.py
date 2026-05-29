from datetime import datetime
from uuid import uuid4

from src.engine.models.task_participant import TaskParticipant


def test_to_dict_returns_strings_for_uuids_and_iso_for_datetime():
    task_id = uuid4()
    user_id = uuid4()
    added_by = uuid4()
    now = datetime(2026, 5, 2, 12, 0, 0)
    p = TaskParticipant(
        task_id=task_id, user_id=user_id,
        added_at=now, added_by_user_id=added_by,
    )
    assert p.to_dict() == {
        "task_id": str(task_id),
        "user_id": str(user_id),
        "added_at": "2026-05-02T12:00:00",
        "added_by_user_id": str(added_by),
    }


def test_to_dict_handles_null_added_by():
    p = TaskParticipant(
        task_id=uuid4(), user_id=uuid4(),
        added_at=datetime(2026, 5, 2), added_by_user_id=None,
    )
    assert p.to_dict()["added_by_user_id"] is None
