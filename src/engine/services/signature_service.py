"""SignatureService — единая точка проверки Zero Trust-подписи запроса.

route/middleware → SignatureService → storage/redis. Middleware НЕ оркестрирует
crypto/nonce сам, а делает один вызов verify_request_signature (как
game_service.verify_request_signature в rptext).
"""
from typing import Tuple, Dict, Any
from uuid import UUID

from .crypto_service import verify_device_signature, check_timestamp
from .nonce_store import NonceStore, NonceStoreUnavailable
from ..storage.device_storage import DeviceStorage
from ..unified_logger import get_logger

logger = get_logger("services")


class SignatureService:
    def __init__(self, device_storage: DeviceStorage, nonce_store: NonceStore, timestamp_tolerance: int):
        self._devices = device_storage
        self._nonce = nonce_store
        self._tolerance = timestamp_tolerance

    async def verify_request_signature(
        self, user_id: UUID, payload: str, signature: str, nonce: str, timestamp: int,
    ) -> Tuple[bool, Dict[str, Any]]:
        """Полная проверка: timestamp → device-ключи → ECDSA → nonce-replay.

        Возвращает (True, {}) при успехе, (False, {"error": <code>}) при отказе.
        Кидает NonceStoreUnavailable если Redis недоступен (middleware → 503).
        """
        if not check_timestamp(timestamp, self._tolerance):
            return False, {"error": "Request timestamp expired"}

        public_keys = await self._devices.get_all_public_keys(user_id)
        if not public_keys:
            return False, {"error": "No device keys registered"}

        if not any(verify_device_signature(pk, payload, signature) for pk in public_keys):
            logger.warning("invalid signature", operation="verify_request_signature", user_id=str(user_id))
            return False, {"error": "Invalid device signature"}

        # nonce-replay — последним, уже после успешной подписи (NonceStoreUnavailable пробрасывается)
        if not await self._nonce.check_and_store(str(user_id), nonce):
            return False, {"error": "Nonce already used"}

        return True, {}

    async def has_device_key(self, user_id: UUID) -> bool:
        return bool(await self._devices.get_all_public_keys(user_id))
