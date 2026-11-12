import asyncio
import copy
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import httpx

from app.shopify.bulk import build_bulk_query
from app.config import Settings
from app.storage.database import init_db
from app.storage.repository import (
    advance_sync_checkpoint, connect, get_sync_checkpoint, save_order,
)
from app.shopify.client import ShopifyClient
from app.services.import_orders import import_orders
from app.cli.sync_orders import run
from test_sync import example


BASELINE = "2026-11-12T10:00:00+00:00"
NEXT_RUN = "2026-11-12T10:20:00+00:00"


class IncrementalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.settings = Settings("prueba.myshopify.com", "fake", "fake",
                                 Path(directory.name) / "orders.sqlite")
        init_db(self.settings.db_path)

    def checkpoint(self):
        return get_sync_checkpoint(self.settings.db_path, self.settings.shop_domain)

    def seed(self):
        save_order(self.settings.db_path, None, example())
        advance_sync_checkpoint(self.settings.db_path, self.settings.shop_domain, None, BASELINE)

    async def export(self, orders=(), *, now=NEXT_RUN, incremental=True,
                     status="COMPLETED", truncated=False, concurrent_checkpoint=None,
                     cancel=False):
        rows = []
        for snapshot in orders:
            order = copy.deepcopy(snapshot)
            lines = order.pop("lineItems")
            rows.append(order)
            rows.extend(dict(line, __parentId=order["id"]) for line in lines)
        content = "\n".join(map(json.dumps, rows[:-1] if truncated else rows))
        requests = []

        def respond(request):
            requests.append(request)
            if request.method == "GET":
                if cancel:
                    raise asyncio.CancelledError()
                return httpx.Response(200, text=content)
            if request.url.path.endswith("/oauth/access_token"):
                return httpx.Response(200, json={"access_token": "fake", "expires_in": 3600})
            payload = json.loads(request.content)
            if "StartOrderExport" in payload["query"]:
                if concurrent_checkpoint:
                    advance_sync_checkpoint(
                        self.settings.db_path, self.settings.shop_domain,
                        self.checkpoint(), concurrent_checkpoint,
                    )
                return httpx.Response(200, json={"data": {"bulkOperationRunQuery": {
                    "bulkOperation": {"id": "gid://shopify/BulkOperation/1"}, "userErrors": [],
                }}})
            if "OrderExportStatus" not in payload["query"]:
                raise AssertionError("No debe consultar pedidos individualmente")
            return httpx.Response(200, json={"data": {"bulkOperation": {
                "status": status, "objectCount": str(len(rows)),
                "url": "https://example.com/export" if rows else None,
            }}})

        messages = []
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            with patch("app.services.import_orders.datetime") as clock:
                clock.now.return_value = datetime.fromisoformat(now)
                result = await import_orders(
                    self.settings, ShopifyClient(self.settings, client), messages.append,
                    incremental=incremental,
                )
        return result, requests, messages

    def order(self):
        with connect(self.settings.db_path) as db:
            return dict(db.execute("SELECT * FROM orders").fetchone())

    async def test_full_load_sets_start_time_not_latest_order_date(self):
        order = example()
        order["updatedAt"] = "2026-11-12T10:25:00Z"
        result, requests, _ = await self.export([order], incremental=False)
        self.assertEqual(result.saved, 1)
        self.assertEqual(self.checkpoint(), NEXT_RUN)
        query = json.loads(requests[1].content)["variables"]["query"]
        self.assertNotIn("updated_at:", query)

    async def test_missing_checkpoint_fails_fast_without_network_or_full_reload(self):
        save_order(self.settings.db_path, None, example())  # Base anterior con pedidos, sin marcador.
        result, requests, messages = await self.export()
        self.assertTrue(result.listing_failed)
        self.assertEqual(requests, [])
        self.assertIsNone(self.checkpoint())
        self.assertTrue(any("sin --incremental" in line for line in messages))

    async def test_recovers_missed_webhook_using_filtered_bulk_and_all_lines(self):
        self.seed()
        changed = example()
        changed.update(note="Cambio sin webhook", updatedAt="2026-11-12T10:15:00Z")
        changed["lineItems"] = [
            dict(changed["lineItems"][0], id=f"gid://shopify/LineItem/{i + 1}")
            for i in range(301)
        ]
        result, requests, _ = await self.export([changed])
        self.assertEqual((result.saved, result.failed), (1, 0))
        query = json.loads(requests[1].content)["variables"]["query"]
        self.assertIn("updated_at:>='2026-11-12T09:55:00Z'", query)
        self.assertIn("updated_at:<='2026-11-12T10:20:00Z'", query)
        self.assertIn("sortKey: UPDATED_AT", query)
        self.assertEqual(len(requests), 4)  # Token, inicio Bulk, estado y archivo.
        self.assertEqual(self.order()["note"], "Cambio sin webhook")
        self.assertEqual(self.checkpoint(), NEXT_RUN)
        with connect(self.settings.db_path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM order_line_items").fetchone()[0], 301)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM webhook_jobs").fetchone()[0], 0)

    async def test_overlap_repeated_export_does_not_duplicate(self):
        self.seed()
        await self.export([example()])
        result, requests, _ = await self.export([example()], now="2026-11-12T10:30:00+00:00")
        self.assertEqual(result.saved, 1)
        query = json.loads(requests[1].content)["variables"]["query"]
        self.assertIn("updated_at:>='2026-11-12T10:15:00Z'", query)
        with connect(self.settings.db_path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM orders").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM order_line_items").fetchone()[0], 1)

    async def test_empty_interval_advances_checkpoint(self):
        self.seed()
        result, _, _ = await self.export()
        self.assertFalse(result.listing_failed)
        self.assertEqual(result.saved, 0)
        self.assertEqual(self.checkpoint(), NEXT_RUN)

    async def test_export_failure_or_truncation_preserves_checkpoint_and_order(self):
        self.seed()
        changed = example()
        changed["note"] = "No guardar"
        for kwargs in ({"status": "FAILED"}, {"truncated": True}):
            with self.subTest(kwargs=kwargs):
                result, _, _ = await self.export([changed], **kwargs)
                self.assertTrue(result.listing_failed)
                self.assertEqual(self.checkpoint(), BASELINE)
                self.assertIsNone(self.order()["note"])

    async def test_write_failure_keeps_checkpoint_and_retry_recovers(self):
        self.seed()
        good = example()
        good.update(id="gid://shopify/Order/2", note="Confirmado")
        good["lineItems"][0]["id"] = "gid://shopify/LineItem/2"
        bad = example()
        bad["lineItems"][0]["quantity"] = -1
        result, _, _ = await self.export([good, bad])
        self.assertEqual((result.saved, result.failed), (1, 1))
        self.assertEqual(self.checkpoint(), BASELINE)
        with connect(self.settings.db_path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM orders").fetchone()[0], 2)
        result, _, _ = await self.export([good, example()])
        self.assertEqual((result.saved, result.failed), (2, 0))
        self.assertEqual(self.checkpoint(), NEXT_RUN)

    async def test_cancellation_preserves_checkpoint(self):
        self.seed()
        with self.assertRaises(asyncio.CancelledError):
            await self.export([example()], cancel=True)
        self.assertEqual(self.checkpoint(), BASELINE)

    async def test_checkpoint_write_failure_returns_error_for_retry(self):
        self.seed()
        with patch("app.services.import_orders.advance_sync_checkpoint", side_effect=sqlite3.OperationalError):
            result, _, _ = await self.export()
        self.assertTrue(result.listing_failed)
        self.assertEqual(self.checkpoint(), BASELINE)

    async def test_recent_webhook_version_survives_export(self):
        self.seed()
        latest = example()
        latest.update(note="Estado nuevo", updatedAt="2026-11-12T10:25:00Z")
        save_order(self.settings.db_path, None, latest)
        result, _, _ = await self.export([example()])
        self.assertEqual(result.skipped, 1)
        self.assertEqual(self.order()["note"], "Estado nuevo")
        self.assertEqual(self.checkpoint(), NEXT_RUN)

    async def test_concurrent_completion_cannot_overwrite_checkpoint(self):
        self.seed()
        later = "2026-11-12T10:30:00+00:00"
        await self.export(concurrent_checkpoint=later)
        self.assertEqual(self.checkpoint(), later)

    async def test_future_checkpoint_fails_without_network(self):
        self.seed()
        result, requests, _ = await self.export(now="2026-11-12T09:00:00+00:00")
        self.assertTrue(result.listing_failed)
        self.assertEqual(requests, [])
        self.assertEqual(self.checkpoint(), BASELINE)

    async def test_reinitialize_preserves_checkpoint_and_shop_isolation(self):
        self.seed()
        init_db(self.settings.db_path)
        self.assertEqual(self.checkpoint(), BASELINE)
        other = Settings("otra.myshopify.com", "fake", "fake", self.settings.db_path)
        self.assertIsNone(get_sync_checkpoint(other.db_path, other.shop_domain))

    async def test_command_uses_incremental_option_and_returns_failure(self):
        with patch("app.cli.sync_orders.Settings.from_env", return_value=self.settings), \
             patch("builtins.print"):
            self.assertEqual(await run(incremental=True), 1)

    def test_filter_normalizes_timezone_and_rejects_naive_date(self):
        query = build_bulk_query(datetime.fromisoformat("2026-11-12T11:00:00+01:00"))
        self.assertIn("updated_at:>='2026-11-12T10:00:00Z'", query)
        with self.assertRaises(ValueError):
            build_bulk_query(datetime(2026, 11, 12))
