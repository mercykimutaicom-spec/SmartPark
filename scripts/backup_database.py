"""Create a timestamped ParkFlow database backup and prune old backups."""
import argparse
import os
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BACKUP_DIR = ROOT / "backups"
# Backups written before the ParkFlow rebrand keep their old prefix, so both
# are matched when the retention window is applied.
BACKUP_PATTERNS = ("parkflow-*.backup", "smartpark-*.backup")


def timestamp():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def sqlite_source():
    """Locate the SQLite file the app is actually using (rebrand-aware)."""
    import db

    return Path(os.environ.get("SQLITE_DB_PATH") or db.resolve_sqlite_path())


def backup_sqlite(destination):
    source = sqlite_source()
    if not source.exists():
        raise FileNotFoundError(f"SQLite database not found: {source}")
    target = sqlite3.connect(destination)
    try:
        with sqlite3.connect(source) as source_connection:
            source_connection.backup(target)
    finally:
        target.close()


def backup_postgres(destination):
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is not configured.")
    command = ["pg_dump", "--format=custom", "--file", str(destination), database_url]
    subprocess.run(command, check=True, capture_output=True, text=True)


def prune(directory, keep):
    backups = sorted(
        (item for pattern in BACKUP_PATTERNS for item in directory.glob(pattern)),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    for old_backup in backups[keep:]:
        old_backup.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_BACKUP_DIR)
    parser.add_argument("--keep", type=int, default=int(os.environ.get("BACKUP_RETENTION_COUNT", "14")))
    args = parser.parse_args()
    if args.keep < 1:
        raise ValueError("--keep must be at least 1")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    destination = args.output_dir / f"parkflow-{timestamp()}.backup"
    if os.environ.get("DATABASE_URL"):
        backup_postgres(destination)
        backend = "PostgreSQL"
    else:
        backup_sqlite(destination)
        backend = "SQLite"
    prune(args.output_dir, args.keep)
    print(f"Created {backend} backup: {destination}")


if __name__ == "__main__":
    main()
