"""Shared runtime paths for NEXUS ERP.

Set NEXUS_DATA_DIR before importing application modules to isolate test/demo data
or to place runtime files outside the source tree. Production defaults remain
backward-compatible with the repository's local data/ directory.
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("NEXUS_DATA_DIR", str(BASE_DIR / "data"))).expanduser().resolve()
DB_PATH = DATA_DIR / "nexus.db"
BACKUP_DIR = Path(os.environ.get("NEXUS_BACKUP_DIR", str(Path.home() / "NEXUS ERP" / "Backups"))).expanduser().resolve()


def ensure_data_directory(path=None):
    """Create the configured data directory, including missing parent folders."""
    target = DATA_DIR if path is None else Path(path).expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)
    return target
