"""Public-key verification only; this module deliberately has no signing capability."""

import base64
import hashlib
import secrets


def decode_canonical_base64(value: str, *, length: int) -> bytes:
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise ValueError("invalid base64") from exc
    if len(decoded) != length or base64.b64encode(decoded).decode("ascii") != value:
        raise ValueError("invalid base64")
    return decoded


def verify_public_key(public_key_base64: str, expected_digest: str) -> bytes:
    public = decode_canonical_base64(public_key_base64, length=32)
    if not secrets.compare_digest(hashlib.sha256(public).hexdigest(), expected_digest):
        raise ValueError("public key digest mismatch")
    return public


def verify_ed25519(public: bytes, signature_base64: str, message: bytes) -> None:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    signature = decode_canonical_base64(signature_base64, length=64)
    try:
        Ed25519PublicKey.from_public_bytes(public).verify(signature, message)
    except InvalidSignature as exc:
        raise ValueError("invalid signature") from exc
