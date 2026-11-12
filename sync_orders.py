"""Importa todos los pedidos accesibles de Shopify sin escribir en Shopify."""

import argparse
import asyncio
import sqlite3
from dataclasses import dataclass
from decimal import InvalidOperation
from datetime import datetime, timedelta, timezone

import httpx

from config import ConfigurationError, Settings
from init_db import init_db
from shopify import ShopifyClient, ShopifyError, parse_date
from sync_service import sync_order
from bulk_orders import iter_bulk_orders
from repository import save_order, get_sync_checkpoint, advance_sync_checkpoint


OVERLAP = timedelta(minutes=5)


@dataclass
class SyncResult:
    saved: int = 0
    skipped: int = 0
    failed: int = 0
    listing_failed: bool = False


async def import_orders(settings, shopify, report=None, *, bulk=True, incremental=False):
    if report is None:
        report = print
    result = SyncResult()
    try:
        # Inicio, nunca fin: los cambios durante la descarga deben entrar en la
        # siguiente ejecución. Redondear hacia abajo mantiene el límite inclusivo.
        started_at = datetime.now(timezone.utc).replace(microsecond=0)
        checkpoint = get_sync_checkpoint(settings.db_path, settings.shop_domain) if bulk else None
        since = None
        if checkpoint is not None and parse_date(checkpoint) > started_at:
            raise ShopifyError("Fecha de control futura; comprueba el reloj del sistema")
        if incremental:
            if not bulk:
                raise ShopifyError("La carga incremental requiere Bulk")
            if checkpoint is None:
                report("No hay fecha de control. Ejecuta primero sync_orders.py sin --incremental "
                       "para completar una carga Bulk y establecerla; no se inicia otra carga automáticamente.")
                raise ShopifyError("Falta una carga completa correcta")
            since = parse_date(checkpoint) - OVERLAP
            report(f"Carga incremental por updated_at: desde {since.isoformat()} "
                   f"hasta {started_at.isoformat()} (solapamiento: 5 minutos).")

        async def snapshots():
            if bulk:
                async for order in iter_bulk_orders(
                    shopify, report, updated_since=since,
                    updated_before=started_at if incremental else None,
                ):
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
        if bulk and not result.failed:
            advanced = advance_sync_checkpoint(
                settings.db_path, settings.shop_domain, checkpoint, started_at.isoformat(),
            )
            if advanced:
                report(f"Fecha de control guardada: {started_at.isoformat()}.")
            else:
                report("Otra carga actualizó la fecha de control; se conserva su valor. "
                       "Puedes repetir --incremental para comprobar el intervalo restante.")
        elif bulk:
            report("Fecha de control conservada por fallos; repite la carga para recuperarlos.")
    except (ShopifyError, httpx.HTTPError, ValueError, KeyError, TypeError,
            AttributeError, sqlite3.Error, OSError) as exc:
        result.listing_failed = True
        report(f"Carga interrumpida; causa={type(exc).__name__}. "
               "La fecha de control no se avanza. Vuelve a ejecutar la carga.")
    report(f"Pedidos guardados: {result.saved}; omitidos por versión local más reciente: "
           f"{result.skipped}; pedidos fallidos: {result.failed}; "
           f"fallos de listado: {int(result.listing_failed)}.")
    return result


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


if __name__ == "__main__":
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
