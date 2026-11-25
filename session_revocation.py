"""Persistent session revocation epoch stored outside the restorable company database."""
import os
import secrets
from pathlib import Path


def current_session_epoch(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="ascii") as handle:
            handle.write(secrets.token_urlsafe(32))
    except FileExistsError:
        pass
    return path.read_text(encoding="ascii").strip()


def rotate_session_epoch(path):
    """Atomically replace the epoch so every previously issued cookie is revoked."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    value = secrets.token_urlsafe(32)
    temporary = path.with_name(path.name + "." + secrets.token_hex(8) + ".tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    finally:
        temporary.unlink(missing_ok=True)
    return value
