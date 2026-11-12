"""Importa todos los pedidos accesibles de Shopify sin escribir en Shopify."""

import argparse
import asyncio
import sqlite3

import httpx

from app.config import ConfigurationError, Settings
from app.storage.database import init_db
from app.shopify.client import ShopifyClient
from app.services.import_orders import import_orders


async def run(*, incremental=False):
    try:
        settings = Settings.from_env()
        init_db(settings.db_path)
    except ConfigurationError as exc:
        print(str(exc))
        return 1
    except (OSError, sqlite3.Error):
        print("No se pudo inicializar SQLite; comprueba DATABASE_PATH y los permisos de escritura.")
        return 1
    async with httpx.AsyncClient(timeout=15) as client:
        result = await import_orders(settings, ShopifyClient(settings, client), incremental=incremental)
    return int(bool(result.failed or result.listing_failed))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--incremental", action="store_true",
        help="Exporta solo pedidos modificados desde la última carga correcta (con 5 minutos de solapamiento).",
    )
    args = parser.parse_args()
    try:
        raise SystemExit(asyncio.run(run(incremental=args.incremental)))
    except KeyboardInterrupt:
        print("Carga interrumpida; los pedidos ya confirmados se conservan. Puedes repetirla.")
        raise SystemExit(130)
