"""Password hashing via bcrypt directly.

Skipping passlib because its bcrypt backend is incompatible with bcrypt 5.x
(it relies on `bcrypt.__about__.__version__` which was removed and on the
pre-truncate behavior that bcrypt 5 stopped doing silently).

Bcrypt has a 72-byte input limit. We truncate explicitly here so a long
passphrase still works rather than blowing up at hash time.
"""

from __future__ import annotations

import bcrypt

_BCRYPT_MAX_BYTES = 72


def _prepare(plain: str) -> bytes:
    raw = plain.encode("utf-8")
    if len(raw) > _BCRYPT_MAX_BYTES:
        # Re-truncate at a UTF-8 boundary to avoid splitting a multibyte char.
        raw = raw[:_BCRYPT_MAX_BYTES]
        while raw and (raw[-1] & 0xC0) == 0x80:
            raw = raw[:-1]
    return raw


def hash_password(plain: str) -> str:
    """Return a bcrypt hash of the plaintext password (12 rounds, default cost)."""
    salt = bcrypt.gensalt(rounds=12)
    return bcrypt.hashpw(_prepare(plain), salt).decode("ascii")


def verify_password(plain: str, hashed: str) -> bool:
    """Constant-time verify. Returns False on any error (malformed hash, etc.)."""
    try:
        return bcrypt.checkpw(_prepare(plain), hashed.encode("ascii"))
    except (ValueError, TypeError):
        return False
