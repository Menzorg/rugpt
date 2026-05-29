"""Резолв шаблона роута движка через стандартный матчинг Starlette.

Источник истины для route-id — сам роут-тейбл приложения, отдельного
реестра нет. Фронт зеркалит те же шаблоны (см. План C, тест парности).
"""
from typing import Iterable, Optional
from starlette.routing import Route, Match


def resolve_route_template(routes: Iterable, method: str, path: str) -> Optional[str]:
    """Вернуть .path шаблона роута, совпавшего по path+method, иначе None."""
    method = method.upper()
    for route in routes:
        if not isinstance(route, Route):
            continue
        scope = {"type": "http", "method": method, "path": path}
        match, _ = route.matches(scope)
        if match == Match.FULL:
            if route.methods and method not in route.methods:
                continue
            return route.path
    return None
