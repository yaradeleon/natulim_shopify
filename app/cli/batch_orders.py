"""Listar GID por estado y aplicar una transición a una lista revisable."""

import argparse
import asyncio
import json
import sys

import httpx

from app.config import ConfigurationError, Settings
from app.storage.database import init_db
from app.services.batches import (
    apply_batch,
    read_manifest,
    select_batch,
    write_manifest,
)
from app.services.transitions import TransitionError
from app.shopify.client import ShopifyClient


async def run(args):
    try:
        settings = Settings.from_env()
        if args.command == "change":
            read_manifest(args.input, settings.shop_domain)
            if not args.dry_run:
                init_db(settings.db_path)
        async with httpx.AsyncClient(timeout=15) as client:
            shopify = ShopifyClient(settings, client)
            if args.command == "list":
                data = await select_batch(shopify, args.state, limit=args.limit)
                write_manifest(args.output, data)
                print(json.dumps(data, ensure_ascii=False, indent=2))
                print(
                    f"GID seleccionados: {len(data['gids'])}. Archivo: {args.output}",
                    file=sys.stderr,
                )
                if data["limited"]:
                    print(
                        "Se alcanzó el límite; puede haber más candidatos. Usa --limit 0 para recorrer todas las páginas.",
                        file=sys.stderr,
                    )
                return 0
            result = await apply_batch(
                shopify,
                settings.db_path,
                args.input,
                args.target,
                args.output,
                dry_run=args.dry_run,
                delay=args.delay,
            )
            return int(any(row["status"] == "failed" for row in result["results"]))
    except (ConfigurationError, TransitionError) as exc:
        print(str(exc))
    except FileExistsError:
        print(
            "El archivo de resultados ya existe. Elige otro nombre de salida para change; los listados sí actualizan el mismo archivo.",
            file=sys.stderr,
        )
    except Exception as exc:
        print(
            f"Lote interrumpido; causa={type(exc).__name__}. Consulta la salida parcial y el estado remoto antes de repetir."
        )
    return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser(
        "list",
        help="Consulta Shopify por estado y guarda los GID sin modificar pedidos",
    )
    listing.add_argument("state", help="BORRADOR, CONFIRMADO, PAGADO o PREPARADO")
    listing.add_argument(
        "--output",
        required=True,
        help="Archivo JSON que se actualiza con la selección nueva; también se imprime en stdout",
    )
    listing.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Máximo de candidatos; por defecto 20; 0 recorre todas las páginas",
    )
    changing = commands.add_parser(
        "change",
        help="Avanza al siguiente estado usando transition_order para cada GID",
    )
    changing.add_argument(
        "target", help="CONFIRMADO, PAGADO o PREPARADO (alias ENVIADO)"
    )
    changing.add_argument(
        "--input",
        required=True,
        help="Archivo JSON de selección o de resultados anteriores",
    )
    changing.add_argument(
        "--output",
        required=True,
        help="Archivo JSON nuevo con resultados y GID correctos para el siguiente paso",
    )
    changing.add_argument(
        "--dry-run",
        action="store_true",
        help="Consulta cada transición sin modificar Shopify ni SQLite",
    )
    changing.add_argument(
        "--delay",
        type=float,
        default=0.5,
        help="Pausa en segundos entre GID; por defecto 0.5",
    )
    try:
        raise SystemExit(asyncio.run(run(parser.parse_args())))
    except KeyboardInterrupt:
        print(
            "Lote interrumpido. La salida conserva resultados ya registrados; el último cambio remoto puede haberse aplicado. Revisa antes de repetir."
        )
        raise SystemExit(130)
