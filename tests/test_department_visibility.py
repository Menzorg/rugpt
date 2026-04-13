"""
Tests for DepartmentService.get_visible_user_ids()

Mocks storages, tests all visibility branches:
- admin sees everyone
- regular user sees own dept + connected depts
- is_head sees same + other heads + admins
- users without department visible to all
- system users visible to all
- self always visible
- user not found returns empty set
"""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4, UUID

from src.engine.models.user import User
from src.engine.services.department_service import DepartmentService


# --- Fixtures ---

def make_user(
    user_id=None, org_id=None, department_id=None,
    is_admin=False, is_head=False, is_system=False,
    name="Test User",
):
    return User(
        id=user_id or uuid4(),
        org_id=org_id or uuid4(),
        name=name,
        username=name.lower().replace(" ", "_"),
        email=f"{name.lower().replace(' ', '_')}@test.com",
        is_admin=is_admin,
        is_head=is_head,
        is_system=is_system,
        department_id=department_id,
    )


def make_service(users, visible_dept_ids=None):
    """Create DepartmentService with mocked storages."""
    user_storage = AsyncMock()
    dept_storage = AsyncMock()

    users_by_id = {u.id: u for u in users}
    user_storage.get_by_id = AsyncMock(side_effect=lambda uid: users_by_id.get(uid))
    user_storage.list_by_org = AsyncMock(return_value=users)

    if visible_dept_ids is not None:
        dept_storage.get_visible_department_ids = AsyncMock(return_value=visible_dept_ids)
    else:
        dept_storage.get_visible_department_ids = AsyncMock(return_value=set())

    return DepartmentService(dept_storage, user_storage)


# --- Tests ---

ORG = uuid4()
DEPT_A = uuid4()
DEPT_B = uuid4()
DEPT_C = uuid4()


@pytest.mark.asyncio
async def test_admin_sees_everyone():
    admin = make_user(org_id=ORG, is_admin=True, department_id=DEPT_A, name="Admin")
    user1 = make_user(org_id=ORG, department_id=DEPT_B, name="User1")
    user2 = make_user(org_id=ORG, department_id=DEPT_C, name="User2")

    svc = make_service([admin, user1, user2])
    visible = await svc.get_visible_user_ids(admin.id, ORG)

    assert visible == {admin.id, user1.id, user2.id}


@pytest.mark.asyncio
async def test_regular_user_sees_own_department():
    user1 = make_user(org_id=ORG, department_id=DEPT_A, name="User1")
    user2 = make_user(org_id=ORG, department_id=DEPT_A, name="User2")
    user3 = make_user(org_id=ORG, department_id=DEPT_B, name="User3")

    svc = make_service([user1, user2, user3], visible_dept_ids={DEPT_A})
    visible = await svc.get_visible_user_ids(user1.id, ORG)

    assert user1.id in visible
    assert user2.id in visible
    assert user3.id not in visible


@pytest.mark.asyncio
async def test_regular_user_sees_connected_department():
    user1 = make_user(org_id=ORG, department_id=DEPT_A, name="User1")
    user2 = make_user(org_id=ORG, department_id=DEPT_B, name="User2")
    user3 = make_user(org_id=ORG, department_id=DEPT_C, name="User3")

    # DEPT_A connected to DEPT_B, not to DEPT_C
    svc = make_service([user1, user2, user3], visible_dept_ids={DEPT_A, DEPT_B})
    visible = await svc.get_visible_user_ids(user1.id, ORG)

    assert user1.id in visible
    assert user2.id in visible
    assert user3.id not in visible


@pytest.mark.asyncio
async def test_head_sees_other_heads():
    head_a = make_user(org_id=ORG, department_id=DEPT_A, is_head=True, name="HeadA")
    head_b = make_user(org_id=ORG, department_id=DEPT_B, is_head=True, name="HeadB")
    regular_b = make_user(org_id=ORG, department_id=DEPT_B, name="RegularB")

    # DEPT_A not connected to DEPT_B
    svc = make_service([head_a, head_b, regular_b], visible_dept_ids={DEPT_A})
    visible = await svc.get_visible_user_ids(head_a.id, ORG)

    assert head_a.id in visible
    assert head_b.id in visible      # head sees other head
    assert regular_b.id not in visible  # but not regular user in unconnected dept


@pytest.mark.asyncio
async def test_head_sees_admins():
    head = make_user(org_id=ORG, department_id=DEPT_A, is_head=True, name="Head")
    admin = make_user(org_id=ORG, department_id=DEPT_B, is_admin=True, name="Admin")

    # DEPT_A not connected to DEPT_B
    svc = make_service([head, admin], visible_dept_ids={DEPT_A})
    visible = await svc.get_visible_user_ids(head.id, ORG)

    assert head.id in visible
    assert admin.id in visible  # head sees admin


@pytest.mark.asyncio
async def test_regular_user_does_not_see_other_heads():
    regular = make_user(org_id=ORG, department_id=DEPT_A, name="Regular")
    head_b = make_user(org_id=ORG, department_id=DEPT_B, is_head=True, name="HeadB")

    # DEPT_A not connected to DEPT_B
    svc = make_service([regular, head_b], visible_dept_ids={DEPT_A})
    visible = await svc.get_visible_user_ids(regular.id, ORG)

    assert regular.id in visible
    assert head_b.id not in visible  # regular cannot see heads in other depts


@pytest.mark.asyncio
async def test_user_without_department_visible_to_all():
    user1 = make_user(org_id=ORG, department_id=DEPT_A, name="User1")
    no_dept = make_user(org_id=ORG, department_id=None, name="NoDept")
    user_other = make_user(org_id=ORG, department_id=DEPT_B, name="Other")

    svc = make_service([user1, no_dept, user_other], visible_dept_ids={DEPT_A})
    visible = await svc.get_visible_user_ids(user1.id, ORG)

    assert no_dept.id in visible  # no dept = visible to everyone
    assert user_other.id not in visible


@pytest.mark.asyncio
async def test_system_users_visible_to_all():
    user1 = make_user(org_id=ORG, department_id=DEPT_A, name="User1")
    ai_user = make_user(org_id=ORG, department_id=None, is_system=True, name="AI")

    svc = make_service([user1, ai_user], visible_dept_ids={DEPT_A})
    visible = await svc.get_visible_user_ids(user1.id, ORG)

    assert ai_user.id in visible


@pytest.mark.asyncio
async def test_self_always_visible():
    """Even if user has no department and no visibility rules, they see themselves."""
    loner = make_user(org_id=ORG, department_id=None, name="Loner")

    svc = make_service([loner])
    visible = await svc.get_visible_user_ids(loner.id, ORG)

    assert loner.id in visible


@pytest.mark.asyncio
async def test_viewer_not_found_returns_empty():
    svc = make_service([])
    visible = await svc.get_visible_user_ids(uuid4(), ORG)

    assert visible == set()


@pytest.mark.asyncio
async def test_user_without_department_sees_other_no_dept_users():
    no_dept1 = make_user(org_id=ORG, department_id=None, name="NoDept1")
    no_dept2 = make_user(org_id=ORG, department_id=None, name="NoDept2")
    in_dept = make_user(org_id=ORG, department_id=DEPT_A, name="InDept")

    svc = make_service([no_dept1, no_dept2, in_dept])
    visible = await svc.get_visible_user_ids(no_dept1.id, ORG)

    assert no_dept1.id in visible
    assert no_dept2.id in visible
    # user without dept has no visible_dept_ids, so cannot see users in depts
    assert in_dept.id not in visible


@pytest.mark.asyncio
async def test_check_visible_true():
    user1 = make_user(org_id=ORG, department_id=DEPT_A, name="User1")
    user2 = make_user(org_id=ORG, department_id=DEPT_A, name="User2")

    svc = make_service([user1, user2], visible_dept_ids={DEPT_A})
    result = await svc.check_visible(user1.id, user2.id, ORG)

    assert result is True


@pytest.mark.asyncio
async def test_check_visible_false():
    user1 = make_user(org_id=ORG, department_id=DEPT_A, name="User1")
    user2 = make_user(org_id=ORG, department_id=DEPT_B, name="User2")

    svc = make_service([user1, user2], visible_dept_ids={DEPT_A})
    result = await svc.check_visible(user1.id, user2.id, ORG)

    assert result is False


@pytest.mark.asyncio
async def test_complex_scenario():
    """
    Org with 3 departments. A connected to B, not to C.
    Head of A, regular in B, regular in C, admin, system user, no-dept user.
    """
    admin = make_user(org_id=ORG, department_id=DEPT_C, is_admin=True, name="Admin")
    head_a = make_user(org_id=ORG, department_id=DEPT_A, is_head=True, name="HeadA")
    head_c = make_user(org_id=ORG, department_id=DEPT_C, is_head=True, name="HeadC")
    regular_a = make_user(org_id=ORG, department_id=DEPT_A, name="RegularA")
    regular_b = make_user(org_id=ORG, department_id=DEPT_B, name="RegularB")
    regular_c = make_user(org_id=ORG, department_id=DEPT_C, name="RegularC")
    ai = make_user(org_id=ORG, is_system=True, name="AI")
    no_dept = make_user(org_id=ORG, department_id=None, name="NoDept")

    all_users = [admin, head_a, head_c, regular_a, regular_b, regular_c, ai, no_dept]

    # HeadA: dept A connected to B
    svc = make_service(all_users, visible_dept_ids={DEPT_A, DEPT_B})
    visible = await svc.get_visible_user_ids(head_a.id, ORG)

    assert head_a.id in visible      # self
    assert regular_a.id in visible   # same dept
    assert regular_b.id in visible   # connected dept
    assert regular_c.id not in visible  # unconnected dept, not head/admin
    assert head_c.id in visible      # head sees other heads
    assert admin.id in visible       # head sees admins
    assert ai.id in visible          # system user
    assert no_dept.id in visible     # no dept = visible
