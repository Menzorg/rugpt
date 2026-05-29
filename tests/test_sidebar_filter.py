"""
Tests for sidebar chat visibility filters (item 11).

Tests the pure helpers filter_sidebar_task_chats / filter_sidebar_project_chats
that encode the spec's visibility rules (can_see_task + status != done + active).
"""
from uuid import uuid4

from src.engine.models.chat import Chat, ChatType
from src.engine.models.project import Project
from src.engine.models.task import Task
from src.engine.models.user import User
from src.engine.services.chat_service import (
    filter_sidebar_task_chats,
    filter_sidebar_project_chats,
)


def make_user(is_admin=False, org_id=None, uid=None):
    return User(
        id=uid or uuid4(), org_id=org_id or uuid4(),
        name="u", username="u", email="u@u",
        is_admin=is_admin,
    )


def make_task_chat(task_id, participants, is_active=True, org_id=None):
    return Chat(
        id=uuid4(), org_id=org_id or uuid4(),
        type=ChatType.TASK, task_id=task_id,
        participants=list(participants), is_active=is_active,
    )


def make_task(assignee, creator, status="in_progress", is_active=True, org_id=None, tid=None):
    return Task(
        id=tid or uuid4(),
        org_id=org_id or uuid4(),
        title="T",
        status=status,
        assignee_user_id=assignee,
        created_by_user_id=creator,
        is_active=is_active,
    )


# === task chat sidebar ===

def test_sidebar_excludes_done_task():
    org = uuid4()
    user = make_user(org_id=org)
    task = make_task(user.id, uuid4(), status="done", org_id=org)
    chat = make_task_chat(task.id, [user.id], org_id=org)
    out = filter_sidebar_task_chats([chat], {task.id: task}, user)
    assert out == []


def test_sidebar_excludes_inactive_task():
    org = uuid4()
    user = make_user(org_id=org)
    task = make_task(user.id, uuid4(), is_active=False, org_id=org)
    chat = make_task_chat(task.id, [user.id], org_id=org)
    out = filter_sidebar_task_chats([chat], {task.id: task}, user)
    assert out == []


def test_sidebar_excludes_archived_chat():
    org = uuid4()
    user = make_user(org_id=org)
    task = make_task(user.id, uuid4(), org_id=org)
    chat = make_task_chat(task.id, [user.id], is_active=False, org_id=org)
    out = filter_sidebar_task_chats([chat], {task.id: task}, user)
    assert out == []


def test_sidebar_excludes_non_visible_viewer():
    """Participant left in chat.participants (audit), but no longer creator/assignee:
    should be hidden from sidebar via can_see_task."""
    org = uuid4()
    old_assignee = make_user(org_id=org)
    new_assignee = make_user(org_id=org)
    creator = make_user(org_id=org)
    task = make_task(new_assignee.id, creator.id, org_id=org)
    chat = make_task_chat(task.id, [creator.id, old_assignee.id, new_assignee.id], org_id=org)
    out = filter_sidebar_task_chats([chat], {task.id: task}, old_assignee)
    assert out == []
    # Sanity: new assignee still sees it
    out2 = filter_sidebar_task_chats([chat], {task.id: task}, new_assignee)
    assert len(out2) == 1


def test_sidebar_includes_admin_same_org():
    org = uuid4()
    admin = make_user(is_admin=True, org_id=org)
    someone = make_user(org_id=org)
    task = make_task(someone.id, someone.id, org_id=org)
    chat = make_task_chat(task.id, [someone.id], org_id=org)
    out = filter_sidebar_task_chats([chat], {task.id: task}, admin)
    assert len(out) == 1


def test_sidebar_admin_cross_org_excluded():
    admin = make_user(is_admin=True, org_id=uuid4())
    other_org = uuid4()
    someone = make_user(org_id=other_org)
    task = make_task(someone.id, someone.id, org_id=other_org)
    chat = make_task_chat(task.id, [someone.id], org_id=other_org)
    out = filter_sidebar_task_chats([chat], {task.id: task}, admin)
    assert out == []


def test_sidebar_missing_task_excluded():
    org = uuid4()
    user = make_user(org_id=org)
    tid = uuid4()
    chat = make_task_chat(tid, [user.id], org_id=org)
    out = filter_sidebar_task_chats([chat], {}, user)
    assert out == []


# === project chat sidebar ===

def test_project_sidebar_includes_participant():
    org = uuid4()
    user = make_user(org_id=org)
    project = Project(org_id=org, name="P", is_active=True)
    chat = Chat(
        org_id=org, type=ChatType.PROJECT, project_id=project.id,
        participants=[user.id], is_active=True,
    )
    out = filter_sidebar_project_chats([chat], {project.id: project}, user)
    assert len(out) == 1


def test_project_sidebar_excludes_archived_project():
    org = uuid4()
    user = make_user(org_id=org)
    project = Project(org_id=org, name="P", is_active=False)
    chat = Chat(
        org_id=org, type=ChatType.PROJECT, project_id=project.id,
        participants=[user.id], is_active=True,
    )
    out = filter_sidebar_project_chats([chat], {project.id: project}, user)
    assert out == []


def test_project_sidebar_excludes_non_participant():
    org = uuid4()
    user = make_user(org_id=org)
    project = Project(org_id=org, name="P", is_active=True)
    chat = Chat(
        org_id=org, type=ChatType.PROJECT, project_id=project.id,
        participants=[uuid4()], is_active=True,
    )
    out = filter_sidebar_project_chats([chat], {project.id: project}, user)
    assert out == []
