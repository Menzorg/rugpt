"""
Tests for TaskMergeService (task merge feature).

No DB/LLM/Kafka — all deps are AsyncMock. Covers:
- _parse_agent_json tolerance (plain / fenced / surrounded / garbage)
- preview(): agent output parsed into a persisted preview request
- preview(): mechanical title fallback when the agent returns nothing usable
- apply(): deterministic create + close orchestration
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from src.engine.models.task import Task
from src.engine.models.user import User
from src.engine.models.task_merge_request import TaskMergeRequest
from src.engine.services.task_merge_service import TaskMergeService


def _user(is_admin=False):
    return User(
        id=uuid4(), org_id=uuid4(), name="Actor", username="actor",
        email="actor@test.com", is_admin=is_admin,
    )


def _task(title="T", creator_id=None, assignee_id=None):
    return Task(
        id=uuid4(), org_id=uuid4(), title=title, status="created",
        assignee_user_id=assignee_id or uuid4(),
        created_by_user_id=creator_id or uuid4(),
    )


def _make_service(**overrides):
    deps = dict(
        task_storage=AsyncMock(),
        task_service=AsyncMock(),
        chat_service=AsyncMock(),
        message_storage=AsyncMock(),
        chat_storage=AsyncMock(),
        task_event_service=AsyncMock(),
        task_participant_storage=AsyncMock(),
        role_storage=AsyncMock(),
        agent_executor=AsyncMock(),
        merge_request_storage=AsyncMock(),
        user_storage=AsyncMock(),
    )
    deps.update(overrides)
    return TaskMergeService(**deps), deps


# === _parse_agent_json ===

def test_parse_plain_json():
    out = TaskMergeService._parse_agent_json('{"title":"A","description":"B","summary":"C"}')
    assert out == {"title": "A", "description": "B", "summary": "C"}


def test_parse_fenced_json():
    raw = '```json\n{"title":"A","description":"B","summary":"C"}\n```'
    assert TaskMergeService._parse_agent_json(raw)["title"] == "A"


def test_parse_json_with_surrounding_text():
    raw = 'Вот результат:\n{"title":"A","summary":"C"}\nГотово.'
    out = TaskMergeService._parse_agent_json(raw)
    assert out["title"] == "A" and out["summary"] == "C"


def test_parse_garbage_returns_empty():
    assert TaskMergeService._parse_agent_json("no json here") == {}
    assert TaskMergeService._parse_agent_json("") == {}


# === preview ===

def test_preview_persists_parsed_proposal():
    async def go():
        svc, deps = _make_service()
        actor = _user()
        t1, t2 = _task("Купить сервер"), _task("Настроить сервер")

        deps["chat_service"].get_task_chat = AsyncMock(return_value=SimpleNamespace(id=uuid4()))
        deps["chat_service"].list_messages = AsyncMock(return_value=[])
        deps["user_storage"].get_by_id = AsyncMock(return_value=actor)
        deps["role_storage"].get_by_code = AsyncMock(return_value=SimpleNamespace(id=uuid4()))
        deps["agent_executor"].execute = AsyncMock(return_value=(
            SimpleNamespace(content='{"title":"Сервер","description":"Купить и настроить","summary":"Объединил две задачи о сервере"}'),
            {},
        ))
        deps["merge_request_storage"].create = AsyncMock(side_effect=lambda req: req)

        req = await svc.preview(actor, [t1, t2])

        assert req.proposed_title == "Сервер"
        assert req.proposed_description == "Купить и настроить"
        assert req.proposed_summary == "Объединил две задачи о сервере"
        assert req.source_task_ids == [t1.id, t2.id]
        assert req.status == "previewed"
        # snapshot captured both source tasks as-is
        assert len(req.source_snapshot["tasks"]) == 2
        deps["merge_request_storage"].create.assert_awaited_once()

    asyncio.run(go())


def test_preview_title_fallback_on_garbage():
    async def go():
        svc, deps = _make_service()
        actor = _user()
        t1, t2 = _task("Альфа"), _task("Бета")

        deps["chat_service"].get_task_chat = AsyncMock(return_value=None)
        deps["chat_service"].list_messages = AsyncMock(return_value=[])
        deps["user_storage"].get_by_id = AsyncMock(return_value=actor)
        deps["role_storage"].get_by_code = AsyncMock(return_value=SimpleNamespace(id=uuid4()))
        deps["agent_executor"].execute = AsyncMock(return_value=(SimpleNamespace(content="мусор"), {}))
        deps["merge_request_storage"].create = AsyncMock(side_effect=lambda req: req)

        req = await svc.preview(actor, [t1, t2])
        assert req.proposed_title.startswith("Объединённая задача:")

    asyncio.run(go())


def test_preview_raises_without_role():
    async def go():
        svc, deps = _make_service()
        deps["role_storage"].get_by_code = AsyncMock(return_value=None)
        deps["chat_service"].get_task_chat = AsyncMock(return_value=None)
        deps["chat_service"].list_messages = AsyncMock(return_value=[])
        deps["user_storage"].get_by_id = AsyncMock(return_value=_user())
        try:
            await svc.preview(_user(), [_task(), _task()])
            assert False, "expected RuntimeError"
        except RuntimeError:
            pass

    asyncio.run(go())


# === apply ===

def test_apply_creates_new_and_closes_sources():
    async def go():
        svc, deps = _make_service()
        actor = _user()
        t1, t2 = _task("A"), _task("B")
        new_task = _task("Объединённая")

        by_id = {t1.id: t1, t2.id: t2}
        deps["task_storage"].get_by_id = AsyncMock(side_effect=lambda tid: by_id.get(tid))
        deps["task_storage"].set_merged_into = AsyncMock(return_value=True)
        deps["task_participant_storage"].list_user_ids = AsyncMock(return_value=[])
        deps["task_service"].create = AsyncMock(return_value=new_task)
        deps["task_service"].deactivate = AsyncMock(return_value=True)
        deps["chat_service"].get_task_chat = AsyncMock(return_value=SimpleNamespace(id=uuid4()))
        deps["task_event_service"].record = AsyncMock()
        deps["merge_request_storage"].mark_applied = AsyncMock()

        mr = TaskMergeRequest(
            org_id=actor.org_id, actor_user_id=actor.id,
            source_task_ids=[t1.id, t2.id], proposed_summary="итог",
        )
        assignee = uuid4()

        result = await svc.apply(
            actor, mr, title="Объединённая", description="desc",
            assignee_user_id=assignee,
        )

        assert result is new_task
        # created once with actor as creator and the chosen assignee
        deps["task_service"].create.assert_awaited_once()
        kwargs = deps["task_service"].create.call_args.kwargs
        assert kwargs["created_by_user_id"] == actor.id
        assert kwargs["assignee_user_id"] == assignee
        assert kwargs["title"] == "Объединённая"
        # both sources closed + linked + audited
        assert deps["task_service"].deactivate.await_count == 2
        assert deps["task_storage"].set_merged_into.await_count == 2
        for c in deps["task_storage"].set_merged_into.await_args_list:
            assert c.args[1] == new_task.id
        assert deps["task_event_service"].record.await_count == 2
        for c in deps["task_event_service"].record.await_args_list:
            assert c.kwargs["event_type"] == "merged"
            assert c.kwargs["payload"]["into_task_id"] == str(new_task.id)
        deps["merge_request_storage"].mark_applied.assert_awaited_once_with(mr.id, new_task.id)

    asyncio.run(go())


def test_apply_rejects_when_fewer_than_two_live_sources():
    async def go():
        svc, deps = _make_service()
        actor = _user()
        t1 = _task("A")
        # only one of the two source ids still resolves (other deactivated/gone)
        deps["task_storage"].get_by_id = AsyncMock(side_effect=lambda tid: t1 if tid == t1.id else None)
        mr = TaskMergeRequest(
            org_id=actor.org_id, actor_user_id=actor.id,
            source_task_ids=[t1.id, uuid4()],
        )
        try:
            await svc.apply(actor, mr, title="X", description=None, assignee_user_id=uuid4())
            assert False, "expected ValueError"
        except ValueError:
            pass

    asyncio.run(go())


def test_apply_dedups_duplicate_source_ids():
    """A request with the same id twice must not become a degenerate self-merge."""
    async def go():
        svc, deps = _make_service()
        actor = _user()
        t1 = _task("A")
        deps["task_storage"].get_by_id = AsyncMock(side_effect=lambda tid: t1 if tid == t1.id else None)
        mr = TaskMergeRequest(
            org_id=actor.org_id, actor_user_id=actor.id,
            source_task_ids=[t1.id, t1.id],  # duplicate
        )
        try:
            await svc.apply(actor, mr, title="X", description=None, assignee_user_id=uuid4())
            assert False, "expected ValueError (only one distinct source)"
        except ValueError:
            pass
        deps["task_service"].create.assert_not_awaited()

    asyncio.run(go())


def test_apply_tolerates_source_close_failure():
    """If closing one source fails, the merge still creates the new task and
    finalizes the request (never stuck 'previewed'); the failure is swallowed."""
    async def go():
        svc, deps = _make_service()
        actor = _user()
        t1, t2 = _task("A"), _task("B")
        new_task = _task("Merged")
        by_id = {t1.id: t1, t2.id: t2}
        deps["task_storage"].get_by_id = AsyncMock(side_effect=lambda tid: by_id.get(tid))
        deps["task_storage"].set_merged_into = AsyncMock(return_value=True)
        deps["task_participant_storage"].list_user_ids = AsyncMock(return_value=[])
        deps["task_service"].create = AsyncMock(return_value=new_task)
        # First source fails to deactivate, second succeeds.
        deps["task_service"].deactivate = AsyncMock(side_effect=[RuntimeError("db hiccup"), True])
        deps["chat_service"].get_task_chat = AsyncMock(return_value=SimpleNamespace(id=uuid4()))
        deps["task_event_service"].record = AsyncMock()
        deps["merge_request_storage"].mark_applied = AsyncMock()

        mr = TaskMergeRequest(
            org_id=actor.org_id, actor_user_id=actor.id,
            source_task_ids=[t1.id, t2.id], proposed_summary="итог",
        )
        result = await svc.apply(actor, mr, title="Merged", description=None, assignee_user_id=uuid4())

        assert result is new_task
        assert deps["task_service"].deactivate.await_count == 2  # both attempted
        # finalized despite the partial failure
        deps["merge_request_storage"].mark_applied.assert_awaited_once_with(mr.id, new_task.id)

    asyncio.run(go())
