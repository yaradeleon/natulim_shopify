import copy
import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

import httpx

from app.config import Settings
from app.storage.database import init_db
from app.storage.repository import connect, save_order
from app.shopify.client import ShopifyClient
from app.services.import_orders import import_orders
from app.cli.sync_orders import run
from test_sync import example


class ImportTests(unittest.IsolatedAsyncioTestCase):
    async def test_command_initializes_configured_database_and_returns_success(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(
                "prueba.myshopify.com",
                "id",
                "secret",
                Path(directory) / "nested" / "db.sqlite",
            )

            def respond(request):
                if request.url.path.endswith("/oauth/access_token"):
                    return httpx.Response(
                        200, json={"access_token": "fake", "expires_in": 3600}
                    )
                payload = json.loads(request.content)
                if "StartOrderExport" in payload["query"]:
                    return httpx.Response(
                        200,
                        json={
                            "data": {
                                "bulkOperationRunQuery": {
                                    "bulkOperation": {
                                        "id": "gid://shopify/BulkOperation/1"
                                    },
                                    "userErrors": [],
                                }
                            }
                        },
                    )
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "bulkOperation": {
                                "status": "COMPLETED",
                                "objectCount": "0",
                                "url": None,
                            }
                        }
                    },
                )

            client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
            with (
                patch("app.cli.sync_orders.Settings.from_env", return_value=settings),
                patch("app.cli.sync_orders.httpx.AsyncClient", return_value=client),
                patch("builtins.print") as output,
            ):
                self.assertEqual(await run(), 0)
            self.assertTrue(settings.db_path.is_file())
            self.assertIn("Pedidos guardados: 0", output.call_args.args[0])

    async def test_command_returns_failure_for_token_error_without_leaking_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(
                "prueba.myshopify.com",
                "id",
                "private-secret",
                Path(directory) / "db.sqlite",
            )
            client = httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(
                        403, json={"error": "private-secret"}
                    )
                )
            )
            with (
                patch("app.cli.sync_orders.Settings.from_env", return_value=settings),
                patch("app.cli.sync_orders.httpx.AsyncClient", return_value=client),
                patch("builtins.print") as output,
            ):
                self.assertEqual(await run(), 1)
            self.assertNotIn("private-secret", str(output.call_args_list))

    async def exercise(
        self, fail_line=False, fail_list=False, invalid_write=False, stale=False
    ):
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(
                "prueba.myshopify.com",
                "fake-id",
                "fake-secret",
                Path(directory) / "db.sqlite",
            )
            init_db(settings.db_path)
            original = example()
            original["note"] = "Copia que debe conservarse"
            if stale:
                original["updatedAt"] = "2026-11-12T10:00:00Z"
            save_order(settings.db_path, None, original)
            requests = []

            def respond(request):
                if request.url.path.endswith("/oauth/access_token"):
                    return httpx.Response(
                        200, json={"access_token": "fake-token", "expires_in": 3600}
                    )
                payload = json.loads(request.content)
                requests.append(payload)
                variables = payload["variables"]
                if "ListOrders" in payload["query"]:
                    second = variables["after"] is not None
                    if second and fail_list:
                        return httpx.Response(429)
                    return httpx.Response(
                        200,
                        json={
                            "data": {
                                "orders": {
                                    "nodes": [
                                        {
                                            "id": f"gid://shopify/Order/{2 if second else 1}"
                                        }
                                    ],
                                    "pageInfo": {
                                        "hasNextPage": not second,
                                        "endCursor": "orders-cursor",
                                    },
                                }
                            }
                        },
                    )
                order = copy.deepcopy(example())
                order["id"] = variables["id"]
                first_order = order["id"].endswith("/1")
                second_line_page = variables["after"] is not None
                if first_order and second_line_page and fail_line:
                    return httpx.Response(
                        200,
                        json={
                            "data": {"order": order},
                            "errors": [{"message": "fake failure"}],
                        },
                    )
                line = order["lineItems"][0]
                line["id"] = f"gid://shopify/LineItem/{'11' if first_order else '21'}"
                if second_line_page:
                    line["id"] = "gid://shopify/LineItem/12"
                if first_order and invalid_write:
                    line["quantity"] = -1
                order["lineItems"] = {
                    "nodes": [line],
                    "pageInfo": {
                        "hasNextPage": first_order and not second_line_page,
                        "endCursor": "line-cursor",
                    },
                }
                return httpx.Response(200, json={"data": {"order": order}})

            messages = []
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(respond)
            ) as client:
                shopify = ShopifyClient(settings, client)
                result = await import_orders(
                    settings, shopify, messages.append, bulk=False
                )
                if not (fail_line or fail_list or invalid_write or stale):
                    # Repetir la carga no duplica ni pedidos ni líneas.
                    result = await import_orders(
                        settings, shopify, messages.append, bulk=False
                    )
            with connect(settings.db_path) as db:
                orders = [
                    dict(row)
                    for row in db.execute(
                        "SELECT * FROM orders ORDER BY shopify_order_id"
                    )
                ]
                lines = [
                    dict(row) for row in db.execute("SELECT * FROM order_line_items")
                ]
            return result, orders, lines, requests, messages

    async def test_paginates_orders_and_lines_and_is_repeatable(self):
        result, orders, lines, requests, messages = await self.exercise()
        self.assertEqual(
            (result.saved, result.failed, result.listing_failed), (2, 0, False)
        )
        self.assertEqual(len(orders), 2)
        self.assertEqual(len(lines), 3)
        self.assertTrue(
            any(r["variables"]["after"] == "orders-cursor" for r in requests)
        )
        self.assertTrue(any(r["variables"]["after"] == "line-cursor" for r in requests))
        self.assertIn("Pedidos guardados: 2", messages[-1])

    async def test_partial_lines_preserve_old_order_and_continue(self):
        result, orders, lines, _, _ = await self.exercise(fail_line=True)
        self.assertEqual((result.saved, result.failed), (1, 1))
        self.assertEqual(orders[0]["note"], "Copia que debe conservarse")
        self.assertEqual(len(lines), 2)

    async def test_write_failure_rolls_back_order_and_continues(self):
        result, orders, lines, _, _ = await self.exercise(invalid_write=True)
        self.assertEqual((result.saved, result.failed), (1, 1))
        self.assertEqual(orders[0]["note"], "Copia que debe conservarse")
        self.assertEqual(len(lines), 2)

    async def test_failed_listing_keeps_already_committed_orders(self):
        result, orders, lines, _, _ = await self.exercise(fail_list=True)
        self.assertTrue(result.listing_failed)
        self.assertEqual(result.saved, 1)
        self.assertEqual(len(orders), 1)
        self.assertEqual(len(lines), 2)

    async def test_newer_local_version_is_not_overwritten(self):
        result, orders, _, _, _ = await self.exercise(stale=True)
        self.assertEqual((result.saved, result.skipped), (1, 1))
        self.assertEqual(orders[0]["note"], "Copia que debe conservarse")
