"""Importa todos los pedidos accesibles de Shopify sin escribir en Shopify."""

import asyncio
import sqlite3
from dataclasses import dataclass
from decimal import InvalidOperation

import httpx

from config import ConfigurationError, Settings
from init_db import init_db
from shopify import ShopifyClient, ShopifyError
from sync_service import sync_order
from bulk_orders import iter_bulk_orders
from repository import save_order


@dataclass
class SyncResult:
    saved: int = 0
    skipped: int = 0
    failed: int = 0
    listing_failed: bool = False


async def import_orders(settings, shopify, report=None, *, bulk=True):
    if report is None:
        report = print
    result = SyncResult()
    try:
        async def snapshots():
            if bulk:
                async for order in iter_bulk_orders(shopify, report):
                    yield order["id"], order
            else:
                async for order_id in shopify.iter_order_ids():
                    yield order_id, None

        async for order_id, order in snapshots():
            try:
                saved = (save_order(settings.db_path, None, order) if bulk
                         else await sync_order(shopify, settings.db_path, order_id))
            except (ShopifyError, httpx.HTTPError, sqlite3.Error, ValueError, KeyError,
                    TypeError, AttributeError, InvalidOperation) as exc:
                result.failed += 1
                # Nunca imprimir la respuesta ni la excepción: podrían contener secretos.
                report(f"No se guardó {order_id}; causa={type(exc).__name__}. Copia local conservada.")
                continue
            if saved:
                result.saved += 1
            else:
                result.skipped += 1
            if (result.saved + result.skipped + result.failed) % 100 == 0:
                report(f"Progreso: {result.saved} guardados; {result.skipped} omitidos; {result.failed} fallidos.")
    except (ShopifyError, httpx.HTTPError, ValueError, KeyError, TypeError,
            AttributeError, sqlite3.Error, OSError) as exc:
        result.listing_failed = True
        report(f"Listado de pedidos interrumpido; causa={type(exc).__name__}. Vuelve a ejecutar la carga.")
    report(f"Pedidos guardados: {result.saved}; omitidos por versión local más reciente: "
           f"{result.skipped}; pedidos fallidos: {result.failed}; "
           f"fallos de listado: {int(result.listing_failed)}.")
    return result


async def run():
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
        result = await import_orders(settings, ShopifyClient(settings, client))
    return int(bool(result.failed or result.listing_failed))


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(run()))
    except KeyboardInterrupt:
        print("Carga interrumpida; los pedidos ya confirmados se conservan. Puedes repetirla.")
        raise SystemExit(130)
