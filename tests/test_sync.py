import asyncio
import base64
import copy
import hashlib
import hmac
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
from fastapi.testclient import TestClient

from config import Settings
from init_db import init_db
from main import create_app, process_one
from repository import connect, enqueue, next_job, save_order
from shopify import ShopifyClient, ShopifyError


def example():
    return {
        "id": "gid://shopify/Order/1", "name": "#FICTICIO",
        "createdAt": "2026-11-11T10:00:00Z", "updatedAt": "2026-11-11T10:00:00Z",
        "note": None, "displayFinancialStatus": None,
        "displayFulfillmentStatus": "UNFULFILLED",
        "currentTotalPriceSet": {"shopMoney": {"amount": "19.90", "currencyCode": "EUR"}},
        "lineItems": [{"id": "gid://shopify/LineItem/1", "title": "Producto ficticio",
                       "quantity": 2, "currentQuantity": 2}],
    }


class SyncTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.settings = Settings("prueba.myshopify.com", "ficticio", "secreto-ficticio",
                                 Path(temp.name) / "test.db")
        init_db(self.settings.db_path)

    def rows(self, table):
        with connect(self.settings.db_path) as db:
            return [dict(row) for row in db.execute(f"SELECT * FROM {table}")]

    def headers(self, body):
        return {
            "X-Shopify-Hmac-Sha256": base64.b64encode(hmac.new(
                self.settings.signing_secret.encode(), body, hashlib.sha256).digest()).decode(),
            "X-Shopify-Shop-Domain": self.settings.shop_domain,
            "X-Shopify-Topic": "orders/create", "X-Shopify-Webhook-Id": "delivery-1",
        }

    def test_signed_webhook_sync_update_duplicate_and_stale(self):
        shopify = AsyncMock()
        shopify.fetch_order.return_value = example()
        body = json.dumps({"admin_graphql_api_id": example()["id"]}).encode()
        with TestClient(create_app(self.settings, start_worker=False)) as client:
            self.assertEqual(client.post("/webhooks/orders", content=body,
                                         headers=self.headers(body)).json()["status"], "queued")
            # La entrega ya está en disco antes de contestar.
            self.assertIsNotNone(next_job(self.settings.db_path))
            self.assertEqual(client.post("/webhooks/orders", content=body,
                                         headers=self.headers(body)).json()["status"], "duplicate")
        asyncio.run(process_one(self.settings, shopify))
        updated = example()
        updated["note"] = "Nota actualizada"
        updated["updatedAt"] = "2026-11-11T12:00:00Z"
        updated["lineItems"][0]["currentQuantity"] = 0
        enqueue(self.settings.db_path, "delivery-2", updated["id"], "orders/updated")
        shopify.fetch_order.return_value = updated
        asyncio.run(process_one(self.settings, shopify))
        self.assertEqual(len(self.rows("orders")), 1)
        self.assertEqual(self.rows("orders")[0]["note"], "Nota actualizada")
        self.assertEqual(self.rows("order_line_items")[0]["current_quantity"], 0)
        save_order(self.settings.db_path, "delivery-1", example())
        self.assertEqual(self.rows("orders")[0]["note"], "Nota actualizada")
        self.assertTrue(all(row["status"] == "done" for row in self.rows("webhook_jobs")))

    def test_invalid_signature_and_shop_do_not_enqueue(self):
        body = b'{"id": 1}'
        with TestClient(create_app(self.settings, start_worker=False)) as client:
            headers = self.headers(body)
            self.assertEqual(client.post("/webhooks/orders", content=body + b' ',
                                         headers=headers).status_code, 401)
            headers["X-Shopify-Shop-Domain"] = "otra.myshopify.com"
            self.assertEqual(client.post("/webhooks/orders", content=body,
                                         headers=headers).status_code, 403)
        self.assertEqual(self.rows("webhook_jobs"), [])

    def test_existing_shopify_routes_validate_signature_topic_and_deduplicate(self):
        body = b'{"id": 1}'
        with TestClient(create_app(self.settings, start_worker=False)) as client:
            for index, topic in enumerate(("orders/create", "orders/updated")):
                route = f"/shopify/webhook/{topic}"
                headers = self.headers(body)
                headers["X-Shopify-Topic"] = topic
                headers["X-Shopify-Webhook-Id"] = f"route-{index}"
                self.assertEqual(client.post(route, content=body + b' ', headers=headers).status_code, 401)
                wrong = dict(headers, **{"X-Shopify-Topic": "orders/updated" if index == 0 else "orders/create"})
                self.assertEqual(client.post(route, content=body, headers=wrong).status_code, 400)
                self.assertEqual(client.post(route, content=body, headers=headers).json()["status"], "queued")
                self.assertEqual(client.post("/webhooks/orders", content=body, headers=headers).json()["status"], "duplicate")
        self.assertEqual(len(self.rows("webhook_jobs")), 2)

    def test_failure_retains_job_for_retry(self):
        enqueue(self.settings.db_path, "retry", example()["id"], "orders/create")
        shopify = AsyncMock()
        shopify.fetch_order.side_effect = ShopifyError("fallo ficticio")
        asyncio.run(process_one(self.settings, shopify))
        job = self.rows("webhook_jobs")[0]
        self.assertEqual((job["status"], job["attempts"]), ("pending", 1))
        self.assertEqual(self.rows("orders"), [])

    def test_worker_recovers_persisted_job_on_startup(self):
        enqueue(self.settings.db_path, "before-start", example()["id"], "orders/create")
        shopify = AsyncMock()
        shopify.fetch_order.return_value = example()
        with TestClient(create_app(self.settings, shopify_client=shopify)) as client:
            self.assertEqual(client.get("/health").status_code, 200)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if self.rows("webhook_jobs")[0]["status"] == "done":
                    break
                time.sleep(0.02)
            self.assertEqual(self.rows("webhook_jobs")[0]["status"], "done")
        self.assertEqual(len(self.rows("orders")), 1)

    def test_graphql_http_and_version_errors(self):
        async def run(status, version):
            def respond(request):
                if request.url.path.endswith("/oauth/access_token"):
                    return httpx.Response(200, json={"access_token": "fake-token", "expires_in": 3600})
                return httpx.Response(status, headers={"X-Shopify-API-Version": version},
                                      json={"data": {"order": None}})
            transport = httpx.MockTransport(respond)
            async with httpx.AsyncClient(transport=transport) as client:
                await ShopifyClient(self.settings, client).fetch_order(example()["id"])

        for status, version in [(429, "2026-10"), (200, "2026-07"), (200, "2026-10")]:
            with self.subTest(status=status, version=version), self.assertRaises(ShopifyError):
                asyncio.run(run(status, version))

    def test_invalid_line_rolls_back_order_and_job(self):
        order = example()
        enqueue(self.settings.db_path, "rollback", order["id"], "orders/create")
        order["lineItems"][0]["currentQuantity"] = -1
        with self.assertRaises(sqlite3.IntegrityError):
            save_order(self.settings.db_path, "rollback", order)
        self.assertEqual(self.rows("orders"), [])
        self.assertEqual(self.rows("webhook_jobs")[0]["status"], "pending")

    def test_complete_snapshot_removes_absent_lines(self):
        save_order(self.settings.db_path, "unused", example())
        order = example()
        order["updatedAt"] = "2026-11-11T13:00:00Z"
        order["lineItems"] = []
        save_order(self.settings.db_path, "unused", order)
        self.assertEqual(self.rows("order_line_items"), [])

    def test_graphql_pagination_and_partial_error(self):
        async def run(fail=False, changed=False):
            requests = []

            def respond(request):
                if request.url.path.endswith("/oauth/access_token"):
                    return httpx.Response(200, json={"access_token": "fake-token", "expires_in": 3600})
                variables = json.loads(request.content)["variables"]
                requests.append(variables)
                order = copy.deepcopy(example())
                second = variables["after"] is not None
                if second and fail:
                    return httpx.Response(200, json={"data": {"order": order}, "errors": [{"message": "failure"}]})
                if second and changed:
                    order["updatedAt"] = "2026-11-11T11:00:00Z"
                if second:
                    order["lineItems"][0]["id"] = "gid://shopify/LineItem/2"
                order["lineItems"] = {
                    "nodes": order["lineItems"],
                    "pageInfo": {"hasNextPage": not second, "endCursor": "cursor1"},
                }
                return httpx.Response(200, json={"data": {"order": order}})

            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                result = await ShopifyClient(self.settings, client).fetch_order(example()["id"])
            self.assertEqual(len(result["lineItems"]), 2)
            self.assertEqual(requests[1]["after"], "cursor1")

        asyncio.run(run())
        with self.assertRaises(ShopifyError):
            asyncio.run(run(fail=True))
        with self.assertRaises(ShopifyError):
            asyncio.run(run(changed=True))


if __name__ == "__main__":
    unittest.main()
