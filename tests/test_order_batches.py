import asyncio
import json
import io
import tempfile
import unittest
from pathlib import Path
from contextlib import redirect_stdout, redirect_stderr
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from app.cli import batch_orders
from app.config import Settings
from app.storage.database import init_db
from app.services.batches import (
    apply_batch,
    manifest,
    read_manifest,
    select_batch,
    write_manifest,
)
from app.services.transitions import TransitionError, TransitionResult
from app.storage.repository import connect
from app.shopify.client import ShopifyClient, ShopifyError
from test_order_transitions import DRAFT, ORDER, Store


def draft(number, status="OPEN"):
    return {
        "__typename": "DraftOrder",
        "id": f"gid://shopify/DraftOrder/{number}",
        "status": status,
    }


def page(nodes, *, more=False, cursor=None, field="draftOrders"):
    return {
        field: {"nodes": nodes, "pageInfo": {"hasNextPage": more, "endCursor": cursor}}
    }


class BatchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.settings = Settings(
            "prueba.myshopify.com", "fake", "fake", self.root / "local.sqlite"
        )
        init_db(self.settings.db_path)
        self.source = self.root / "source.json"
        self.output = self.root / "output.json"
        self.messages = []

    def source_file(self, state="BORRADOR", gids=None):
        write_manifest(
            self.source,
            manifest(
                self.settings.shop_domain, state, [DRAFT] if gids is None else gids
            ),
            create=True,
        )

    def shopify(self, *responses):
        return SimpleNamespace(
            settings=self.settings, graphql=AsyncMock(side_effect=list(responses))
        )

    async def test_lists_all_pages_and_filters_deduplicates_without_per_gid_calls(self):
        shopify = self.shopify(
            page([draft(11), draft(12, "INVOICE_SENT")], more=True, cursor="next"),
            page([draft(11), draft(13, "COMPLETED"), draft(14)]),
        )
        result = await select_batch(shopify, "BORRADOR", limit=0)
        self.assertEqual(
            result["gids"],
            [DRAFT, "gid://shopify/DraftOrder/12", "gid://shopify/DraftOrder/14"],
        )
        self.assertEqual(shopify.graphql.await_count, 2)
        self.assertEqual(shopify.graphql.call_args_list[1].args[1]["after"], "next")
        self.assertFalse(result["limited"])

    async def test_limit_is_reported_without_fetching_remaining_pages(self):
        shopify = self.shopify(page([draft(11), draft(12)], more=True, cursor="next"))
        result = await select_batch(shopify, "NUEVO", limit=1)
        self.assertEqual(result["gids"], [DRAFT])
        self.assertTrue(result["limited"])
        self.assertEqual(shopify.graphql.await_count, 1)

    async def test_invalid_pagination_or_query_failure_does_not_return_partial_selection(
        self,
    ):
        for second in (
            page([draft(12)], more=True, cursor="next"),
            ShopifyError("private"),
        ):
            shopify = self.shopify(page([draft(11)], more=True, cursor="next"), second)
            with (
                self.subTest(second=type(second).__name__),
                self.assertRaises((TransitionError, ShopifyError)),
            ):
                await select_batch(shopify, "BORRADOR", limit=0)

    async def test_order_filters_use_same_state_rules(self):
        for state, financial, fulfillment in (
            ("CONFIRMADO", "PENDING", "UNFULFILLED"),
            ("PAGADO", "PAID", "UNFULFILLED"),
            ("PREPARADO", "PAID", "FULFILLED"),
        ):
            good = dict(
                Store().order,
                displayFinancialStatus=financial,
                displayFulfillmentStatus=fulfillment,
            )
            cancelled = dict(
                good, id="gid://shopify/Order/2", cancelledAt="2026-11-12T10:00:00Z"
            )
            other = dict(
                good, id="gid://shopify/Order/3", displayFinancialStatus="REFUNDED"
            )
            shopify = self.shopify(page([good, cancelled, other], field="orders"))
            result = await select_batch(shopify, state)
            self.assertEqual(result["gids"], [ORDER])
            self.assertIn(
                f"financial_status:{financial.lower()}",
                shopify.graphql.call_args.args[1]["query"],
            )

    async def test_partial_failure_keeps_success_gids_and_mapping_for_next_step(self):
        self.source_file(
            gids=[DRAFT, "gid://shopify/DraftOrder/12", "gid://shopify/DraftOrder/13"]
        )
        results = [
            TransitionResult(ORDER, "CONFIRMADO", True, True),
            TransitionError("Ya ha avanzado"),
            TransitionResult("gid://shopify/Order/3", "CONFIRMADO", False, True),
        ]
        with patch(
            "app.services.batches.transition_order", new=AsyncMock(side_effect=results)
        ) as change:
            result = await apply_batch(
                self.shopify(),
                self.settings.db_path,
                self.source,
                "CONFIRMADO",
                self.output,
                delay=0,
                report=self.messages.append,
            )
        self.assertEqual(change.await_count, 3)
        self.assertEqual(result["gids"], [ORDER, "gid://shopify/Order/3"])
        self.assertEqual(
            [r["status"] for r in result["results"]], ["updated", "failed", "unchanged"]
        )
        self.assertEqual(result["results"][0]["input_gid"], DRAFT)
        self.assertEqual(
            read_manifest(self.output, self.settings.shop_domain),
            ("CONFIRMADO", result["gids"]),
        )

    async def test_chain_reuses_real_individual_flow_and_refreshes_sqlite(self):
        self.source_file()
        store = Store()
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(store.respond)
        ) as client:
            shopify = ShopifyClient(self.settings, client)
            confirmed = await apply_batch(
                shopify,
                self.settings.db_path,
                self.source,
                "CONFIRMADO",
                self.output,
                delay=0,
                report=self.messages.append,
            )
            self.assertEqual(confirmed["gids"], [ORDER])
            paid_file, prepared_file = (
                self.root / "paid.json",
                self.root / "prepared.json",
            )
            await apply_batch(
                shopify,
                self.settings.db_path,
                self.output,
                "PAGADO",
                paid_file,
                delay=0,
                report=self.messages.append,
            )
            await apply_batch(
                shopify,
                self.settings.db_path,
                paid_file,
                "ENVIADO",
                prepared_file,
                delay=0,
                report=self.messages.append,
            )
            # Reintentar el lote de preparación no crea otro fulfillment.
            await apply_batch(
                shopify,
                self.settings.db_path,
                paid_file,
                "PREPARADO",
                self.root / "retry.json",
                delay=0,
                report=self.messages.append,
            )
        self.assertEqual(len(store.mutations), 3)
        with connect(self.settings.db_path) as db:
            order = db.execute("SELECT * FROM orders").fetchone()
            self.assertEqual(
                (order["financial_status"], order["fulfillment_status"]),
                ("PAID", "FULFILLED"),
            )
            self.assertEqual(db.execute("SELECT COUNT(*) FROM orders").fetchone()[0], 1)

    async def test_changed_remote_state_is_rechecked_and_failure_does_not_stop_batch(
        self,
    ):
        self.source_file(state="CONFIRMADO", gids=[ORDER, "gid://shopify/Order/2"])
        store = Store()
        store.order["displayFinancialStatus"] = "REFUNDED"
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(store.respond)
        ) as client:
            result = await apply_batch(
                ShopifyClient(self.settings, client),
                self.settings.db_path,
                self.source,
                "PAGADO",
                self.output,
                delay=0,
                report=self.messages.append,
            )
        self.assertEqual([r["status"] for r in result["results"]], ["failed", "failed"])
        self.assertEqual(store.mutations, [])
        self.assertEqual(result["gids"], [])

    async def test_change_during_refresh_is_not_exported_as_a_success_gid(self):
        self.source_file(state="CONFIRMADO", gids=[ORDER])
        store = Store()
        store.change_on_snapshot = True
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(store.respond)
        ) as client:
            result = await apply_batch(
                ShopifyClient(self.settings, client),
                self.settings.db_path,
                self.source,
                "PAGADO",
                self.output,
                delay=0,
                report=self.messages.append,
            )
        self.assertEqual(result["gids"], [])
        self.assertEqual(result["results"][0]["status"], "failed")
        with connect(self.settings.db_path) as db:
            self.assertEqual(
                db.execute("SELECT financial_status FROM orders").fetchone()[0],
                "REFUNDED",
            )

    async def test_dry_run_no_mutations_or_sqlite_writes_and_not_reusable_as_success(
        self,
    ):
        self.source_file()
        store = Store()
        before = self.settings.db_path.read_bytes()
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(store.respond)
        ) as client:
            result = await apply_batch(
                ShopifyClient(self.settings, client),
                self.settings.db_path,
                self.source,
                "CONFIRMADO",
                self.output,
                dry_run=True,
                delay=0,
                report=self.messages.append,
            )
        self.assertEqual(store.mutations, [])
        self.assertEqual(before, self.settings.db_path.read_bytes())
        self.assertEqual(result["results"][0]["status"], "simulated")
        self.assertEqual(result["gids"], [])
        with self.assertRaises(TransitionError):
            read_manifest(self.output, self.settings.shop_domain)

    async def test_cancellation_preserves_completed_items(self):
        self.source_file(gids=[DRAFT, "gid://shopify/DraftOrder/12"])
        with patch(
            "app.services.batches.transition_order",
            new=AsyncMock(
                side_effect=[
                    TransitionResult(ORDER, "CONFIRMADO", True, True),
                    asyncio.CancelledError(),
                ]
            ),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await apply_batch(
                    self.shopify(),
                    self.settings.db_path,
                    self.source,
                    "CONFIRMADO",
                    self.output,
                    delay=0,
                    report=self.messages.append,
                )
        result = json.loads(self.output.read_text(encoding="utf-8"))
        self.assertFalse(result["complete"])
        self.assertEqual(result["gids"], [ORDER])
        self.assertEqual(len(result["results"]), 1)

    async def test_existing_output_or_same_path_prevents_mutations(self):
        self.source_file()
        self.output.write_text("keep", encoding="utf-8")
        with patch("app.services.batches.transition_order", new=AsyncMock()) as change:
            for output in (self.source, self.output):
                with (
                    self.subTest(output=output),
                    self.assertRaises((TransitionError, FileExistsError)),
                ):
                    await apply_batch(
                        self.shopify(),
                        self.settings.db_path,
                        self.source,
                        "CONFIRMADO",
                        output,
                        delay=0,
                        report=self.messages.append,
                    )
            change.assert_not_awaited()
        self.assertEqual(self.output.read_text(encoding="utf-8"), "keep")

    async def test_wrong_shop_invalid_gid_or_state_jump_fails_before_processing(self):
        self.source_file()
        data = json.loads(self.source.read_text(encoding="utf-8"))
        with patch("app.services.batches.transition_order", new=AsyncMock()) as change:
            for changes in (
                {"shop_domain": "otra.myshopify.com"},
                {"gids": ["#D12"]},
                {"gids": [ORDER]},
                {"version": 5},
            ):
                invalid = dict(data, **changes)
                write_manifest(self.source, invalid)
                with self.subTest(changes=changes), self.assertRaises(TransitionError):
                    await apply_batch(
                        self.shopify(),
                        self.settings.db_path,
                        self.source,
                        "CONFIRMADO",
                        self.output,
                        delay=0,
                        report=self.messages.append,
                    )
            write_manifest(self.source, data)
            with self.assertRaises(TransitionError):
                await apply_batch(
                    self.shopify(),
                    self.settings.db_path,
                    self.source,
                    "PAGADO",
                    self.output,
                    delay=0,
                    report=self.messages.append,
                )
            change.assert_not_awaited()

    async def test_duplicate_input_ids_processed_once(self):
        self.source_file(gids=[DRAFT, DRAFT])
        with patch(
            "app.services.batches.transition_order",
            new=AsyncMock(
                return_value=TransitionResult(ORDER, "CONFIRMADO", True, True),
            ),
        ) as change:
            await apply_batch(
                self.shopify(),
                self.settings.db_path,
                self.source,
                "CONFIRMADO",
                self.output,
                delay=0,
                report=self.messages.append,
            )
            self.assertEqual(change.await_count, 1)

    async def test_unexpected_error_detail_is_not_exposed(self):
        self.source_file()
        with patch(
            "app.services.batches.transition_order",
            new=AsyncMock(side_effect=RuntimeError("private-secret")),
        ):
            result = await apply_batch(
                self.shopify(),
                self.settings.db_path,
                self.source,
                "CONFIRMADO",
                self.output,
                delay=0,
                report=self.messages.append,
            )
        self.assertEqual(result["results"][0]["error"], "RuntimeError")
        self.assertNotIn("private-secret", self.output.read_text(encoding="utf-8"))
        self.assertNotIn("private-secret", " ".join(self.messages))

    async def test_empty_selection_is_valid_noop(self):
        self.source_file(gids=[])
        with patch("app.services.batches.transition_order", new=AsyncMock()) as change:
            result = await apply_batch(
                self.shopify(),
                self.settings.db_path,
                self.source,
                "CONFIRMADO",
                self.output,
                delay=0,
                report=self.messages.append,
            )
        change.assert_not_awaited()
        self.assertTrue(result["complete"])
        self.assertEqual(result["gids"], [])

    async def test_list_command_outputs_json_without_creating_sqlite(self):
        settings = Settings(
            "prueba.myshopify.com", "fake", "fake", self.root / "missing.sqlite"
        )

        def respond(request):
            if request.url.path.endswith("/oauth/access_token"):
                return httpx.Response(
                    200, json={"access_token": "fake", "expires_in": 3600}
                )
            return httpx.Response(200, json={"data": page([draft(11)])})

        client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        args = SimpleNamespace(
            command="list", state="BORRADOR", limit=20, output=self.output
        )
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            patch("app.cli.batch_orders.Settings.from_env", return_value=settings),
            patch("app.cli.batch_orders.httpx.AsyncClient", return_value=client),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            self.assertEqual(await batch_orders.run(args), 0)
        self.assertEqual(json.loads(stdout.getvalue())["gids"], [DRAFT])
        self.assertEqual(
            json.loads(stdout.getvalue()),
            json.loads(self.output.read_text(encoding="utf-8")),
        )
        self.assertIn("GID seleccionados: 1", stderr.getvalue())
        self.assertEqual(
            read_manifest(self.output, settings.shop_domain), ("BORRADOR", [DRAFT])
        )
        self.assertFalse(settings.db_path.exists())

    async def test_list_refresh_replaces_same_file_only_after_successful_query(self):
        original = manifest(self.settings.shop_domain, "BORRADOR", [DRAFT])
        write_manifest(self.output, original, create=True)
        args = SimpleNamespace(
            command="list", state="BORRADOR", limit=20, output=self.output
        )

        def respond(request):
            if request.url.path.endswith("/oauth/access_token"):
                return httpx.Response(
                    200, json={"access_token": "fake", "expires_in": 3600}
                )
            return httpx.Response(200, json={"data": page([draft(12)])})

        async def run_once():
            client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
            with (
                patch(
                    "app.cli.batch_orders.Settings.from_env", return_value=self.settings
                ),
                patch("app.cli.batch_orders.httpx.AsyncClient", return_value=client),
                patch("builtins.print"),
            ):
                return await batch_orders.run(args)

        with patch(
            "app.cli.batch_orders.select_batch",
            new=AsyncMock(side_effect=ShopifyError("private")),
        ):
            self.assertEqual(await run_once(), 1)
        self.assertEqual(json.loads(self.output.read_text(encoding="utf-8")), original)
        self.assertEqual(await run_once(), 0)
        self.assertEqual(
            json.loads(self.output.read_text(encoding="utf-8"))["gids"],
            ["gid://shopify/DraftOrder/12"],
        )

    async def test_change_command_returns_failure_when_an_item_fails(self):
        self.source_file(state="CONFIRMADO", gids=[ORDER])
        store = Store()
        store.errors.add("orderMarkAsPaid")
        client = httpx.AsyncClient(transport=httpx.MockTransport(store.respond))
        args = SimpleNamespace(
            command="change",
            target="PAGADO",
            input=self.source,
            output=self.output,
            dry_run=False,
            delay=0,
        )
        with (
            patch("app.cli.batch_orders.Settings.from_env", return_value=self.settings),
            patch("app.cli.batch_orders.httpx.AsyncClient", return_value=client),
            patch("builtins.print"),
        ):
            self.assertEqual(await batch_orders.run(args), 1)
        result = json.loads(self.output.read_text(encoding="utf-8"))
        self.assertEqual(result["results"][0]["status"], "failed")
        self.assertEqual(result["gids"], [])
