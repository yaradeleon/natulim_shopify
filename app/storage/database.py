"""Initialize SQLite without deleting existing records."""

import sqlite3
from contextlib import closing
from pathlib import Path

from app.paths import DEFAULT_DB_PATH, SCHEMA_PATH


def init_db(db_path: str | Path = DEFAULT_DB_PATH) -> Path:
    """Inicializa una base local; repetirlo conserva tablas y registros."""
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    target = Path(db_path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)

    with closing(sqlite3.connect(target)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            connection.executescript("BEGIN;\n" + schema + "\nCOMMIT;")
        except sqlite3.Error:
            connection.rollback()
            raise

    return target
