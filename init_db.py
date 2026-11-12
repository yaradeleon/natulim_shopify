"""Crea las tablas SQLite sin borrar los datos existentes."""

import argparse
import sqlite3
from pathlib import Path

from app.paths import DEFAULT_DB_PATH
from app.storage.database import init_db


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
