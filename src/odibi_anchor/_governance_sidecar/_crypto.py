"""Process-local sidecar handshake key material."""

from __future__ import annotations

import base64
import hashlib
import secrets
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any

from odibi_anchor._governance_protocol._verification import (
    decode_canonical_base64,  # noqa: F401 -- compatibility export
    verify_ed25519,  # noqa: F401 -- compatibility export
    verify_public_key,  # noqa: F401 -- compatibility export
)

from ._canonical import domain_digest, domain_preimage

JS_SAFE_INTEGER = 9_007_199_254_740_991


@dataclass
class ProcessIdentity:
    """Handshake plus deliberately private closure key."""

    handshake: dict[str, object]
    _private_key: Any
    _challenge_lock: threading.Lock
    _challenge_consumed: bool = False

    def _consume_authentic_challenge(self, challenge: str) -> None:
        """Atomically consume this process's challenge; deliberately no reset operation."""
        with self._challenge_lock:
            if challenge != self.handshake["challenge"]:
                raise ValueError("challenge mismatch")
            if self._challenge_consumed:
                raise ValueError("challenge already consumed")
            self._challenge_consumed = True


def sign_deployment_binding(identity: ProcessIdentity, binding_without_signature: dict[str, object]) -> dict[str, object]:
    detached = dict(binding_without_signature)
    signature = identity._private_key.sign(
        domain_preimage("anchor-amp/deployment-binding-signature/v1", detached)
    )
    detached["sidecar_signature_base64"] = base64.b64encode(signature).decode("ascii")
    return detached


def handshake_digest(handshake: dict[str, object]) -> str:
    return domain_digest("anchor-amp/sidecar-handshake/v1", handshake)


def create_process_identity() -> ProcessIdentity:
    """Create fresh process-local Ed25519 identity before any output is emitted."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private_key = Ed25519PrivateKey.generate()
    public = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    issued_at_ms = time.time_ns() // 1_000_000
    if not 0 <= issued_at_ms <= JS_SAFE_INTEGER:
        raise RuntimeError("system clock is outside the supported range")
    handshake: dict[str, object] = {
        "handshake_version": 1,
        "sidecar_instance_id": uuid.uuid4().hex,
        "challenge": base64.b64encode(secrets.token_bytes(32)).decode("ascii"),
        "closure_public_key_base64": base64.b64encode(public).decode("ascii"),
        "closure_public_key_digest": hashlib.sha256(public).hexdigest(),
        "issued_at_ms": issued_at_ms,
    }
    return ProcessIdentity(handshake, private_key, threading.Lock())
