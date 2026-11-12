"""Crea las tablas SQLite sin borrar los datos existentes."""

import argparse
import sqlite3
from contextlib import closing
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = PROJECT_DIR / "data" / "shopify.db"
SCHEMA_PATH = PROJECT_DIR / "schema.sql"


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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db", type=Path, default=DEFAULT_DB_PATH,
        help="Ruta SQLite alternativa (por defecto: data/shopify.db junto al script).",
    )
    args = parser.parse_args()
    try:
        target = init_db(args.db)
    except (OSError, sqlite3.Error) as exc:
        parser.exit(1, f"No se pudo inicializar la base de datos: {exc}\n")
    print(f"Base de datos inicializada: {target}")


if __name__ == "__main__":
    main()
