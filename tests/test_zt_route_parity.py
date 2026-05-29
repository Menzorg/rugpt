"""Zero Trust Plan C — empirical route-id parity check.

The signed `route_id` (a template string in the webclient's zt-routes.ts) MUST
byte-match the engine's registered `route.path` for the route that the engine's
OWN matcher (`resolve_route_template`) resolves a concrete request to. This is
the SAME matcher used at runtime by WebSignatureMiddleware, so this test catches
the whole bug class: trailing-slash drift AND route-registration-order
collisions (a static route shadowed by a dynamic `/{param}` route declared
before it). Static TS-only tests cannot catch either.

How it works:
- Parse ZT_ROUTES from zt-routes.ts with a regex over the flat `as const` object,
  extracting each (method, template) pair.
- For each pair, substitute every `{param}` placeholder with a dummy value, then
  run `resolve_route_template(app.routes, method, concrete_path)` — the engine's
  real matcher against the real `app.routes`.
- Assert the matched `route.path` template EQUALS the ZT template. Collect ALL
  mismatches, fail listing every one.
"""
import re
from pathlib import Path

from src.engine.app import app
from src.engine.security.route_match import resolve_route_template

ZT_ROUTES_TS = Path(
    "/root/webclient_rugpt/packages/common/src/zt-routes.ts"
)

# Matches: KEY: { method: "X", template: "Y" }   (whitespace/newlines tolerant)
_ENTRY_RE = re.compile(
    r'(?P<key>[A-Z_][A-Z0-9_]*)\s*:\s*\{\s*'
    r'method\s*:\s*"(?P<method>[A-Z]+)"\s*,\s*'
    r'template\s*:\s*"(?P<template>[^"]+)"\s*,?\s*'
    r'\}',
    re.DOTALL,
)

# Matches the same shape but with method/template in the opposite order, just in
# case the source ever reorders the keys.
_ENTRY_RE_ALT = re.compile(
    r'(?P<key>[A-Z_][A-Z0-9_]*)\s*:\s*\{\s*'
    r'template\s*:\s*"(?P<template>[^"]+)"\s*,\s*'
    r'method\s*:\s*"(?P<method>[A-Z]+)"\s*,?\s*'
    r'\}',
    re.DOTALL,
)

_PARAM_RE = re.compile(r"\{[^}]+\}")


def _parse_zt_routes():
    """Parse zt-routes.ts → list of (key, method, template)."""
    text = ZT_ROUTES_TS.read_text(encoding="utf-8")
    entries = {}
    for rx in (_ENTRY_RE, _ENTRY_RE_ALT):
        for m in rx.finditer(text):
            entries[m.group("key")] = (m.group("method"), m.group("template"))
    return [(k, v[0], v[1]) for k, v in entries.items()]


def _concrete(template: str) -> str:
    """Substitute every {param} with a dummy value to build a request path."""
    return _PARAM_RE.sub("x", template)


def test_zt_parse_nonempty():
    """Sanity: the regex actually found the ZT entries."""
    routes = _parse_zt_routes()
    assert len(routes) >= 100, f"expected ~113 ZT routes, parsed {len(routes)}"


def test_zt_route_parity():
    """Every ZT (method, template) must resolve, via the engine's own matcher,
    to a route whose `.path` equals the ZT template byte-for-byte."""
    mismatches = []
    for key, method, template in _parse_zt_routes():
        concrete = _concrete(template)
        matched = resolve_route_template(app.routes, method, concrete)
        if matched != template:
            mismatches.append((key, method, template, matched))

    if mismatches:
        lines = ["ZT route parity mismatches (key | method | ZT-template | engine-matched):"]
        for key, method, template, matched in mismatches:
            lines.append(f"  {key} | {method} | {template} | {matched}")
        raise AssertionError("\n".join(lines))
