import copy
import json
import tempfile
import unittest
from pathlib import Path

import httpx

from app.config import Settings
from app.storage.database import init_db
from app.storage.repository import connect, save_order
from app.shopify.client import ShopifyClient
from app.services.import_orders import import_orders
from test_sync import example


class BulkTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, *, status="COMPLETED", truncated=False, invalid=False,
                       stale=False, download_status=200, start_error=False):
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings("prueba.myshopify.com", "fake", "fake", Path(directory) / "db.sqlite")
            init_db(settings.db_path)
            old = example()
            old["note"] = "Conservar"
            if stale:
                old["updatedAt"] = "2026-12-01T00:00:00Z"
            save_order(settings.db_path, None, old)
            order = copy.deepcopy(example())
            order["note"] = "Importado"
            lines = []
            for index in range(301):
                line = copy.deepcopy(order["lineItems"][0])
                line.update(id=f"gid://shopify/LineItem/{index + 1}", __parentId=order["id"])
                if invalid:
                    line["quantity"] = -1
                lines.append(line)
            del order["lineItems"]
            other = copy.deepcopy(order)
            other["id"] = "gid://shopify/Order/2"
            rows = [order, other] + lines  # No depender de hijos contiguos al padre.
            content = "\n".join(map(json.dumps, rows[:-1] if truncated else rows))
            requests = []

            def respond(request):
                requests.append(request)
                if request.method == "GET":
                    self.assertNotIn("X-Shopify-Access-Token", request.headers)
                    return httpx.Response(download_status, text=content)
                if request.url.path.endswith("/oauth/access_token"):
                    return httpx.Response(200, json={"access_token": "fake", "expires_in": 3600})
                query = json.loads(request.content)["query"]
                if "StartOrderExport" in query:
                    return httpx.Response(200, json={"data": {"bulkOperationRunQuery": {
                        "bulkOperation": {"id": "gid://shopify/BulkOperation/1"},
                        "userErrors": [{"message": "secret"}] if start_error else [],
                    }}})
                return httpx.Response(200, json={"data": {"bulkOperation": {
                    "status": status, "objectCount": str(len(rows)), "url": "https://example.com/export",
                }}})

            messages = []
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                shopify = ShopifyClient(settings, client)
                result = await import_orders(settings, shopify, messages.append)
                if not (truncated or invalid or stale or start_error or status != "COMPLETED" or download_status != 200):
                    result = await import_orders(settings, shopify, messages.append)
            with connect(settings.db_path) as db:
                orders = [dict(row) for row in db.execute("SELECT * FROM orders ORDER BY shopify_order_id")]
                count = db.execute("SELECT COUNT(*) FROM order_line_items").fetchone()[0]
            self.assertNotIn("secret", " ".join(messages))
            return result, orders, count

    async def test_large_order_all_lines_and_repeat_no_duplicates(self):
        result, orders, count = await self.exercise()
        self.assertEqual((result.saved, result.failed, count, len(orders)), (2, 0, 301, 2))

    async def test_truncated_download_does_not_write_any_orders(self):
        result, orders, count = await self.exercise(truncated=True)
        self.assertTrue(result.listing_failed)
        self.assertEqual((len(orders), orders[0]["note"], count), (1, "Conservar", 1))

    async def test_failed_export_download_or_start_preserves_local(self):
        for kwargs in ({"status": "FAILED"}, {"status": "CANCELED"},
                       {"download_status": 503}, {"start_error": True}):
            with self.subTest(kwargs=kwargs):
                result, orders, count = await self.exercise(**kwargs)
                self.assertTrue(result.listing_failed)
                self.assertEqual((len(orders), orders[0]["note"], count), (1, "Conservar", 1))

    async def test_invalid_lines_rollback_order_and_continue(self):
        result, orders, count = await self.exercise(invalid=True)
        self.assertEqual((result.saved, result.failed, count), (1, 1, 1))
        self.assertEqual(orders[0]["note"], "Conservar")

    async def test_newer_local_order_survives_bulk_export(self):
        result, orders, count = await self.exercise(stale=True)
        self.assertEqual((result.saved, result.skipped, count), (1, 1, 1))
        self.assertEqual(orders[0]["note"], "Conservar")
