import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.storage.database import init_db


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.path = Path(self.temp_dir.name) / "nested" / "test.db"
        init_db(self.path)
        self.connection = sqlite3.connect(self.path)
        self.addCleanup(self.connection.close)
        self.connection.execute("PRAGMA foreign_keys = ON")

    def insert_order(self):
        self.connection.execute(
            "INSERT INTO orders (shopify_order_id, name, created_at, updated_at, note, "
            "financial_status, fulfillment_status, current_total_amount, currency_code) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "gid://shopify/Order/1",
                "#PRUEBA",
                "2026-11-11T10:00:00Z",
                "2026-11-11T10:00:00Z",
                None,
                None,
                "UNFULFILLED",
                "19.90",
                "EUR",
            ),
        )

    def test_new_database_is_empty(self):
        for table in ("orders", "order_line_items"):
            self.assertEqual(
                self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],
                0,
            )

    def test_reinitialization_preserves_orders_and_lines(self):
        self.insert_order()
        self.connection.execute(
            "INSERT INTO order_line_items VALUES (?, ?, ?, ?, ?)",
            (
                "gid://shopify/LineItem/1",
                "gid://shopify/Order/1",
                "Producto ficticio",
                2,
                2,
            ),
        )
        self.connection.commit()
        before = {
            table: self.connection.execute(f"SELECT * FROM {table}").fetchall()
            for table in ("orders", "order_line_items")
        }
        init_db(self.path)
        init_db(self.path)
        for table, rows in before.items():
            self.assertEqual(
                self.connection.execute(f"SELECT * FROM {table}").fetchall(), rows
            )

    def test_same_order_can_be_updated_without_duplication(self):
        self.insert_order()
        self.connection.execute(
            "UPDATE orders SET note = ?, updated_at = ? WHERE shopify_order_id = ?",
            ("Prueba actualización 2", "2026-11-11T11:00:00Z", "gid://shopify/Order/1"),
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT shopify_order_id, note, updated_at FROM orders"
            ).fetchall(),
            [
                (
                    "gid://shopify/Order/1",
                    "Prueba actualización 2",
                    "2026-11-11T11:00:00Z",
                )
            ],
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_order()

    def test_line_requires_existing_order(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "INSERT INTO order_line_items VALUES (?, ?, ?, ?, ?)",
                ("gid://shopify/LineItem/1", "missing", "Producto ficticio", 1, 1),
            )

    def test_negative_quantity_is_rejected(self):
        self.insert_order()
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "INSERT INTO order_line_items VALUES (?, ?, ?, ?, ?)",
                (
                    "gid://shopify/LineItem/1",
                    "gid://shopify/Order/1",
                    "Producto ficticio",
                    1,
                    -1,
                ),
            )


if __name__ == "__main__":
    unittest.main()
