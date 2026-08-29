"""Generate an `ADMIN_PASSWORD_HASH` value for `.env`.

    python -m app.hash_password
    python -m app.hash_password 'my password'

Reads from a prompt when no argument is given, so the password does not end up
in the shell history. Deliberately imports nothing from `app.config`: it must
work before the environment is configured, which is precisely when it is needed.
"""
from __future__ import annotations

import getpass
import sys

from app.passwords import hash_password


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv

    if args:
        password = args[0]
    else:
        password = getpass.getpass("Password: ")
        if password != getpass.getpass("Confirm:  "):
            print("Passwords did not match.", file=sys.stderr)
            return 1

    if not password:
        print("Refusing to hash an empty password.", file=sys.stderr)
        return 1

    print()
    print("Add this to your .env (and remove ADMIN_PASSWORD):")
    print()
    print(f"ADMIN_PASSWORD_HASH={hash_password(password)}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
