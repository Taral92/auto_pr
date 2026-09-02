import sqlite3
from pathlib import Path

from config import ROOT, get_settings

SCHEMA = Path(__file__).with_name("schema.sql").read_text()
MIN_SQLITE = (3, 35, 0)


def connect() -> sqlite3.Connection:
    settings = get_settings()
    path = Path(settings.db_path)
    if not path.is_absolute():
        path = ROOT / path
    conn = sqlite3.connect(str(path), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def sqlite_version(conn: sqlite3.Connection) -> str:
    return conn.execute("SELECT sqlite_version()").fetchone()[0]


def _parse_version(raw: str) -> tuple[int, int, int]:
    parts = [int(p) for p in raw.split(".")[:3]]
    while len(parts) < 3:
        parts.append(0)
    return parts[0], parts[1], parts[2]


def require_sqlite(conn: sqlite3.Connection) -> None:
    ver = sqlite_version(conn)
    if _parse_version(ver) < MIN_SQLITE:
        raise RuntimeError(
            f"SQLite {ver} is too old; need >= {'.'.join(map(str, MIN_SQLITE))} for RETURNING"
        )


def init_db(conn: sqlite3.Connection) -> None:
    require_sqlite(conn)
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()


def _migrate(conn) -> None:
    """Additive-only migrations. Safe to run on every startup."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(runs)")}
    if cols and "started_at" not in cols:
        conn.execute("ALTER TABLE runs ADD COLUMN started_at REAL")
        conn.commit()
