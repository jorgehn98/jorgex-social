"""AES-GCM encryption of stored credentials with a master key kept in a file outside the
database, so a copy of the database alone does not reveal them."""

import logging
import os
import secrets
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

log = logging.getLogger(__name__)

KEY_BYTES = 32
NONCE_BYTES = 12


class SecretKeyError(RuntimeError):
    pass


def ensure_key(path: Path) -> bool:
    """Creates the key file with mode 600 if it does not exist; returns whether it did."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        # O_EXCL: never overwrite an existing key, which would make stored credentials unreadable.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "wb") as fh:
        fh.write(secrets.token_bytes(KEY_BYTES))
    return True


def load_key(path: Path) -> bytes:
    try:
        key = path.read_bytes()
    except FileNotFoundError:
        raise SecretKeyError(
            f"secret key file {path} is missing; run `jorgex-social migrate` to create it, or "
            "restore the one the stored credential was encrypted with"
        ) from None
    if len(key) != KEY_BYTES:
        raise SecretKeyError(f"secret key file {path} must hold exactly {KEY_BYTES} bytes")
    if path.stat().st_mode & 0o077:
        log.warning(
            "secret key file is readable by others; chmod 600 it", extra={"path": str(path)}
        )
    return key


def encrypt(key: bytes, plaintext: str, context: str) -> bytes:
    """`context` is bound to the ciphertext, so a value copied to another row does not decrypt."""
    nonce = secrets.token_bytes(NONCE_BYTES)
    return nonce + AESGCM(key).encrypt(nonce, plaintext.encode(), context.encode())


def decrypt(key: bytes, blob: bytes, context: str) -> str:
    try:
        plaintext = AESGCM(key).decrypt(blob[:NONCE_BYTES], blob[NONCE_BYTES:], context.encode())
    except InvalidTag:
        raise SecretKeyError(
            "the stored credential does not decrypt with this secret key; restore the key it was "
            "encrypted with or connect again"
        ) from None
    return plaintext.decode()
