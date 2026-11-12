import sqlite3
import time
from contextlib import contextmanager
from decimal import Decimal

from shopify import parse_date


@contextmanager
def connect(path):
    connection = sqlite3.connect(path, timeout=2)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def enqueue(path, delivery_id, order_id, topic):
    with connect(path) as db:
        cursor = db.execute(
            "INSERT INTO webhook_jobs (delivery_id, order_id, topic) VALUES (?, ?, ?) "
            "ON CONFLICT(delivery_id) DO NOTHING", (delivery_id, order_id, topic),
        )
        return cursor.rowcount == 1


def next_job(path):
    with connect(path) as db:
        row = db.execute(
            "SELECT * FROM webhook_jobs WHERE status = 'pending' AND next_attempt <= ? "
            "ORDER BY next_attempt, rowid LIMIT 1", (time.time(),),
        ).fetchone()
        return dict(row) if row else None


def retry_job(path, job, error_code):
    delay = min(60, 2 ** min(job["attempts"] + 1, 6))
    with connect(path) as db:
        db.execute(
            "UPDATE webhook_jobs SET attempts = attempts + 1, next_attempt = ?, "
            "last_error = ? WHERE delivery_id = ?",
            (time.time() + delay, error_code, job["delivery_id"]),
        )


def save_order(path, job_id, order):
    """Pedido, líneas y finalización del trabajo se confirman juntos."""
    updated = parse_date(order["updatedAt"])
    created = parse_date(order["createdAt"])
    money = order["currentTotalPriceSet"]["shopMoney"]
    if not isinstance(money["amount"], str) or not Decimal(money["amount"]).is_finite():
        raise ValueError("Importe decimal inválido")
    with connect(path) as db:
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute(
            "SELECT updated_at FROM orders WHERE shopify_order_id = ?", (order["id"],),
        ).fetchone()
        saved = not existing or updated >= parse_date(existing["updated_at"])
        if saved:
            db.execute(
                "INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(shopify_order_id) DO UPDATE SET "
                "name=excluded.name, created_at=excluded.created_at, updated_at=excluded.updated_at, "
                "note=excluded.note, financial_status=excluded.financial_status, "
                "fulfillment_status=excluded.fulfillment_status, "
                "current_total_amount=excluded.current_total_amount, currency_code=excluded.currency_code",
                (order["id"], order["name"], created.isoformat(), updated.isoformat(), order["note"],
                 order["displayFinancialStatus"], order["displayFulfillmentStatus"],
                 money["amount"], money["currencyCode"]),
            )
            # La respuesta ya contiene TODAS las páginas. Reemplazo atómico local.
            db.execute("DELETE FROM order_line_items WHERE order_id = ?", (order["id"],))
            db.executemany(
                "INSERT INTO order_line_items VALUES (?, ?, ?, ?, ?)",
                [(line["id"], order["id"], line["title"], line["quantity"], line["currentQuantity"])
                 for line in order["lineItems"]],
            )
        if job_id is not None:
            db.execute(
                "UPDATE webhook_jobs SET status = 'done', last_error = NULL WHERE delivery_id = ?",
                (job_id,),
            )
        return saved
