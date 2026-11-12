"""Avanza un pedido de prueba en Shopify y refresca su copia SQLite."""

import argparse
import asyncio
import sqlite3
from decimal import InvalidOperation

import httpx

from app.config import ConfigurationError, Settings
from app.storage.database import init_db
from app.services.transitions import (
    TransitionError,
    normalize_state,
    transition_order,
    validate_gid,
)
from app.shopify.client import ShopifyClient, ShopifyError


async def run(gid, state, *, dry_run=False):
    try:
        validate_gid(gid)
        state = normalize_state(state)
        settings = Settings.from_env()
        if not dry_run:
            init_db(settings.db_path)
        async with httpx.AsyncClient(timeout=15) as client:
            await transition_order(
                ShopifyClient(settings, client),
                settings.db_path,
                gid,
                state,
                dry_run=dry_run,
            )
        return 0
    except (ConfigurationError, TransitionError) as exc:
        print(str(exc))
    except (
        ShopifyError,
        httpx.HTTPError,
        sqlite3.Error,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        InvalidOperation,
    ):
        print(
            "No se pudo consultar/procesar el pedido. Comprueba credenciales, permisos y SQLite; no se muestran datos sensibles."
        )
    return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "gid", help="GID completo de DraftOrder o de Order, no el número # del pedido"
    )
    parser.add_argument(
        "estado",
        help="BORRADOR, CONFIRMADO, PAGADO, PREPARADO; aliases NUEVO y ENVIADO",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Consulta y muestra la transición sin modificar Shopify ni SQLite",
    )
    args = parser.parse_args()
    try:
        raise SystemExit(asyncio.run(run(args.gid, args.estado, dry_run=args.dry_run)))
    except KeyboardInterrupt:
        print(
            "Comando interrumpido. Shopify puede haber aplicado el cambio; consulta --dry-run antes de repetir."
        )
        raise SystemExit(130)
