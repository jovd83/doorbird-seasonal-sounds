"""Password hashing, on the standard library alone.

`hashlib.scrypt` (RFC 7914) is memory-hard, ships with CPython, and has no
input-length limit. It replaces the `passlib[bcrypt]` dependency this project
declared but never imported -- which turned out not to work anyway: passlib
1.7.4 was last released in 2020 and cannot drive bcrypt 5.x, the version an
unpinned `pip install` resolves today. Its backend probe fails to read
`bcrypt.__about__` and its self-test trips bcrypt's 72-byte input limit.

Hash format is self-describing, so the cost parameters can be raised later
without invalidating existing hashes:

    scrypt$<n>$<r>$<p>$<salt-hex>$<key-hex>
"""
from __future__ import annotations

import hashlib
import hmac
import os

SCHEME = "scrypt"

# ~16 MiB and roughly 50-100 ms per verify on a NAS-class CPU. The admin login
# is not a hot path, so the cost is bought back many times over.
DEFAULT_N = 2**14
DEFAULT_R = 8
DEFAULT_P = 1
SALT_BYTES = 16
KEY_BYTES = 32


class InvalidHash(ValueError):
    """The stored hash is not a well-formed scrypt hash."""


def _derive(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    # maxmem is set explicitly: OpenSSL's default cap is 32 MiB, which n=2**15
    # or above would exceed, and the resulting error is deeply unhelpful.
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=n,
        r=r,
        p=p,
        dklen=KEY_BYTES,
        maxmem=256 * 1024 * 1024,
    )


def hash_password(password: str, *, n: int = DEFAULT_N, r: int = DEFAULT_R,
                  p: int = DEFAULT_P) -> str:
    """Hash a password with a fresh random salt."""
    if not password:
        raise ValueError("refusing to hash an empty password")
    salt = os.urandom(SALT_BYTES)
    key = _derive(password, salt, n, r, p)
    return f"{SCHEME}${n}${r}${p}${salt.hex()}${key.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check of a password against a stored hash.

    Raises `InvalidHash` when the stored value is unusable, so a mistyped
    `ADMIN_PASSWORD_HASH` fails loudly at the point of use rather than
    silently rejecting every correct password.
    """
    parts = (stored or "").split("$")
    if len(parts) != 6 or parts[0] != SCHEME:
        raise InvalidHash(
            f"expected a {SCHEME}$n$r$p$salt$key hash, got {stored[:24]!r}...")
    try:
        n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
        salt = bytes.fromhex(parts[4])
        expected = bytes.fromhex(parts[5])
    except ValueError as exc:
        raise InvalidHash(f"malformed {SCHEME} hash: {exc}") from exc

    return hmac.compare_digest(_derive(password, salt, n, r, p), expected)


def looks_hashed(value: str) -> bool:
    """True if this reads as one of our hashes rather than a plaintext password."""
    return (value or "").startswith(f"{SCHEME}$")
