"""Тест-хелперы Zero Trust: переопределение личности в обход подписи для юнит-тестов роутов."""
from src.engine.routes.auth import get_current_user


def override_identity(app, *, user_id, org_id, is_admin=False, is_head=False, department_id=None):
    """app.dependency_overrides[get_current_user] = фиксированная личность (для юнит-тестов логики роута)."""
    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": user_id, "org_id": org_id, "is_admin": is_admin,
        "is_head": is_head, "department_id": department_id,
    }


def clear_identity(app):
    app.dependency_overrides.pop(get_current_user, None)
