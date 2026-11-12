"""Rutas absolutas ancladas a la raíz del repositorio, no al directorio de trabajo."""

from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = PROJECT_DIR / "data" / "shopify.db"
SCHEMA_PATH = PROJECT_DIR / "schema.sql"
