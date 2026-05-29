"""Каноническая сборка payload для проверки Zero Trust-подписи.

Контракт с фронтом (packages/frontend): payload == фронтовый payload байт-в-байт.
clean_body == json.dumps(separators=(",",":"), sort_keys=True, ensure_ascii=False).
"""
import json
from typing import Optional, Dict, Any

SIGNATURE_FIELDS = {"signature", "nonce", "sig_timestamp", "route_id"}


def _clean_body_str(body: Optional[Dict[str, Any]]) -> str:
    if not body:
        return ""
    clean = {k: v for k, v in body.items() if k not in SIGNATURE_FIELDS}
    return json.dumps(clean, separators=(",", ":"), sort_keys=True, ensure_ascii=False)


def build_payload(
    method: str,
    route_id: str,
    body: Optional[Dict[str, Any]],
    nonce: str,
    timestamp: int,
) -> str:
    """payload = METHOD:route_id:clean_body:nonce:timestamp. Для GET body=None → пустой."""
    clean = "" if method.upper() == "GET" else _clean_body_str(body)
    return f"{method.upper()}:{route_id}:{clean}:{nonce}:{timestamp}"
