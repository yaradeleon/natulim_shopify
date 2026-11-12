"""Consulta un pedido local: python check_order.py '#6501'."""

import argparse
import sqlite3
from contextlib import closing
from pathlib import Path

from dotenv import load_dotenv
import os

from app.paths import PROJECT_DIR, DEFAULT_DB_PATH


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pedido", help="Número con o sin #, o GID completo de Shopify")
    args = parser.parse_args()
    load_dotenv(PROJECT_DIR / ".env")
    path = Path(os.environ.get("DATABASE_PATH", str(DEFAULT_DB_PATH))).expanduser()
    if not path.is_absolute():
        path = PROJECT_DIR / path
    identifier = args.pedido.strip()
    name = identifier if identifier.startswith("#") else f"#{identifier}"
    try:
        # mode=ro evita crear una base vacía o modificar la existente.
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            order = db.execute(
                "SELECT shopify_order_id, name, created_at, updated_at, note, financial_status, "
                "fulfillment_status, current_total_amount, currency_code FROM orders "
                "WHERE name = ? OR shopify_order_id = ?",
                (name, identifier),
            ).fetchone()
            if order is None:
                print("Pedido no encontrado en la base local.")
                return 1
            print("PEDIDO LOCAL")
            for key, value in dict(order).items():
                print(f"{key}: {value if value is not None else '(vacío)'}")
            print("\nLÍNEAS")
            lines = db.execute(
                "SELECT * FROM order_line_items WHERE order_id = ? ORDER BY shopify_line_item_id",
                (order["shopify_order_id"],),
            ).fetchall()
            for line in lines:
                print(f"{line['title']} | quantity={line['quantity']} | "
                      f"currentQuantity={line['current_quantity']} | {line['shopify_line_item_id']}")
            if not lines:
                print("Sin líneas.")
            print("\nÚLTIMOS WEBHOOKS DEL PEDIDO")
            jobs = db.execute(
                "SELECT topic, status, attempts, last_error FROM webhook_jobs "
                "WHERE order_id = ? ORDER BY rowid DESC LIMIT 5",
                (order["shopify_order_id"],),
            ).fetchall()
            for job in jobs:
                print(f"{job['topic']} | {job['status']} | intentos fallidos={job['attempts']} | "
                      f"error={job['last_error'] or '-'}")
            if not jobs:
                print("Sin avisos registrados; puede haberse importado mediante la carga inicial.")
    except (sqlite3.Error, OSError, ValueError):
        print("No se pudo consultar SQLite. Comprueba DATABASE_PATH y ejecuta primero la inicialización.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
