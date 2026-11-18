"""Read-only local runtime preflight for NEXUS ERP.

This check never initializes, migrates, repairs, or writes to the company database.
"""
import importlib.util
import os
import sqlite3
import sys
from pathlib import Path

REQUIRED_MODULES = {
    "flask": "Flask",
    "openpyxl": "openpyxl",
    "cryptography": "cryptography",
    "flask_wtf": "Flask-WTF",
    "waitress": "waitress",
    "reportlab": "reportlab",
    "PIL": "Pillow",
    "arabic_reshaper": "arabic-reshaper",
    "bidi": "python-bidi",
    "psycopg": "psycopg",
}


def check_runtime():
    errors = []
    print("NEXUS ERP - Local readiness check")
    print(f"Python: {sys.version.split()[0]}")
    if sys.version_info < (3, 11):
        errors.append("Python 3.11 or newer is required.")

    missing = [
        package for module, package in REQUIRED_MODULES.items()
        if importlib.util.find_spec(module) is None
    ]
    if missing:
        errors.append("Missing Python packages: " + ", ".join(missing))
    else:
        print("Dependencies: OK")

    data_dir = Path(os.environ.get("NEXUS_DATA_DIR", Path(__file__).resolve().parents[1] / "data")).expanduser()
    database = data_dir / "nexus.db"
    if not database.exists():
        print(f"Company database: not created yet ({database})")
        print("First start will create a new company database; no existing database was changed.")
    else:
        try:
            uri = database.resolve().as_uri() + "?mode=ro"
            connection = sqlite3.connect(uri, uri=True, timeout=5)
            try:
                result = connection.execute("PRAGMA quick_check").fetchone()
                if not result or result[0] != "ok":
                    errors.append(f"Company database integrity check failed: {result}")
                else:
                    print(f"Company database: integrity OK ({database})")
            finally:
                connection.close()
        except (OSError, sqlite3.Error) as exc:
            errors.append(f"Company database could not be checked read-only: {exc}")

    backup_dir = Path(os.environ.get(
        "NEXUS_BACKUP_DIR", Path.home() / "NEXUS ERP" / "Backups"
    )).expanduser()
    writable_ancestor = backup_dir
    while not writable_ancestor.exists() and writable_ancestor != writable_ancestor.parent:
        writable_ancestor = writable_ancestor.parent
    if writable_ancestor.exists() and os.access(writable_ancestor, os.W_OK):
        print(f"Backup location: nearest existing parent is writable ({backup_dir})")
    else:
        errors.append(f"Backup location has no writable existing parent: {backup_dir}")

    if errors:
        for error in errors:
            print("ERROR: " + error)
        return 1
    print("Readiness check passed. No database writes or migrations were performed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(check_runtime())
