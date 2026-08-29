"""Encryption of door-station passwords at rest.

One Fernet key, from `FERNET_KEY`. Losing or rotating it makes every stored
device password unreadable, which is a survivable state -- the app should say
so and keep running -- rather than a crash.
"""
from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings

_fernet = Fernet(settings.fernet_key.encode())


class UndecryptableSecret(Exception):
    """A stored value cannot be decrypted with the configured FERNET_KEY.

    Almost always means the key changed, or the database was restored without
    the .env that goes with it. The device's password has to be re-entered.
    """


def encrypt(value: str) -> str:
    return _fernet.encrypt(value.encode()).decode()


def decrypt(token: str) -> str:
    """Decrypt a stored secret.

    Raises `UndecryptableSecret` for anything the current key cannot read,
    including values that are not valid Fernet tokens at all -- `Fernet` itself
    raises a bare `binascii.Error` for those, which is not something a caller
    can reasonably catch by name.
    """
    try:
        return _fernet.decrypt(token.encode()).decode()
    except (InvalidToken, ValueError, TypeError) as exc:
        raise UndecryptableSecret(
            "stored password could not be decrypted with the current FERNET_KEY "
            "-- if the key was changed or the database restored separately, "
            "re-enter this device's password on the Devices page"
        ) from exc
