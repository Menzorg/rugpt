"""
Crypto Service

ECDSA P-256 signature verification for Zero Trust.
"""
import base64
import logging

logger = logging.getLogger("rugpt.services.crypto")


def verify_device_signature(
    device_public_key_pem: str,
    payload: str,
    signature_b64: str,
) -> bool:
    """
    Verify request signature using device's public key.

    Args:
        device_public_key_pem: PEM-encoded ECDSA P-256 public key
        payload: Signed data (method:body:nonce:timestamp)
        signature_b64: Base64-encoded signature

    Returns:
        True if signature is valid
    """
    try:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
        from cryptography.hazmat.primitives.serialization import load_pem_public_key
        from cryptography.exceptions import InvalidSignature

        public_key = load_pem_public_key(device_public_key_pem.encode("utf-8"))

        signature_raw = base64.b64decode(signature_b64)

        # Web Crypto API returns IEEE P1363 format (r || s, 64 bytes for P-256)
        # Python cryptography expects DER format — convert
        if len(signature_raw) == 64:
            r = int.from_bytes(signature_raw[:32], "big")
            s = int.from_bytes(signature_raw[32:], "big")
            signature = encode_dss_signature(r, s)
        else:
            signature = signature_raw

        public_key.verify(
            signature,
            payload.encode("utf-8"),
            ec.ECDSA(hashes.SHA256()),
        )
        return True

    except InvalidSignature:
        logger.warning("Invalid device signature")
        return False
    except Exception as e:
        logger.error(f"Signature verification error: {e}")
        return False
