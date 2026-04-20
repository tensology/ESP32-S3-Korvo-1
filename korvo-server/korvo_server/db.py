import shutil
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from korvo_server.config import REPO_ROOT
from korvo_server.config_gen import hydrate_wifi_db_from_external_sources


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS wifi_networks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ssid TEXT NOT NULL,
            password TEXT NOT NULL,
            is_active INTEGER DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME
        );
        """
    )
    for stmt in (
        "ALTER TABLE wifi_networks ADD COLUMN updated_at DATETIME",
        "ALTER TABLE wifi_networks ADD COLUMN is_active INTEGER DEFAULT 0",
    ):
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass
    conn.commit()


@contextmanager
def get_conn(db_path: Path):
    c = connect(db_path)
    try:
        yield c
    finally:
        c.close()


def migrate_legacy_sqlite_if_needed(target: Path) -> None:
    """If korvo-server/korvo.db is missing, copy from old korvo-config-server/korvo.db (same repo)."""
    if target.exists():
        return
    legacy = REPO_ROOT / "korvo-config-server" / "korvo.db"
    if legacy.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(legacy, target)
        print(f"[korvo] Migrated SQLite from {legacy}")


class KorvoDB:
    """Thread-safe wrapper: SQLite + single lock for sync routes."""

    def __init__(self, path: Path):
        migrate_legacy_sqlite_if_needed(path)
        self.conn = connect(path)
        init_db(self.conn)
        if hydrate_wifi_db_from_external_sources(self.conn):
            print("[korvo] WiFi networks were empty — restored from legacy korvo.db and/or korvo_config.h.")
        self.lock = threading.Lock()

    def close(self) -> None:
        self.conn.close()
