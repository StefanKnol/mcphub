"""Secret handling: encryption at rest, password hashing, token hashing."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**15, 8, 1
# scrypt needs 128 * N * r bytes = exactly 32 MiB at these parameters, which is
# also OpenSSL's default `maxmem`, so the call fails on the boundary unless the
# limit is raised explicitly.
_SCRYPT_MAXMEM = 96 * 1024 * 1024


def load_or_create_key(path: Path) -> bytes:
    """Return the Fernet master key, generating it on first run.

    Written 0600 before any content reaches it, so the key is never briefly
    world-readable on a shared host.
    """
    if path.exists():
        return path.read_bytes().strip()

    key = Fernet.generate_key()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(key)
    return key


class SecretBox:
    """Encrypts the secret half of a backend's config (router passwords, API keys).

    Secrets live in their own column rather than inside config_json so that the
    non-secret config stays queryable and greppable, and so a UI bug cannot
    render a password into a form field by accident.
    """

    def __init__(self, key: bytes) -> None:
        self._fernet = Fernet(key)

    def seal(self, data: dict[str, Any]) -> bytes:
        return self._fernet.encrypt(json.dumps(data).encode())

    def open(self, blob: bytes | None) -> dict[str, Any]:
        if not blob:
            return {}
        try:
            return json.loads(self._fernet.decrypt(blob))
        except InvalidToken as exc:
            raise ValueError(
                "Stored secrets could not be decrypted. The master key in "
                "master.key does not match the one used to write hub.db."
            ) from exc


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P,
                        dklen=32, maxmem=_SCRYPT_MAXMEM)
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, dk_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        dk = hashlib.scrypt(
            password.encode(),
            salt=base64.b64decode(salt_b64),
            n=int(n), r=int(r), p=int(p),
            dklen=len(base64.b64decode(dk_b64)),
            maxmem=_SCRYPT_MAXMEM,
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk, base64.b64decode(dk_b64))


def hash_token(token: str) -> str:
    """Tokens are stored hashed, so a database leak does not hand over live sessions.

    Plain SHA-256 is right here where a password KDF is not: the input is
    128 bits of CSPRNG output, so there is no dictionary to attack, and the
    verification path runs on every single MCP request.
    """
    return hashlib.sha256(token.encode()).hexdigest()


def new_token() -> str:
    return secrets.token_urlsafe(32)
