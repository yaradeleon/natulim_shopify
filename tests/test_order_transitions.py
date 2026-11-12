import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

import httpx

from app.cli import change_order
from app.config import Settings
from app.storage.database import init_db
from app.services.transitions import TransitionError, transition_order, mutate, PAY_MUTATION
from app.storage.repository import connect
from app.shopify.client import ShopifyClient
from test_sync import example


DRAFT = "gid://shopify/DraftOrder/11"
ORDER = "gid://shopify/Order/1"


class Store:
    def __init__(self):
        self.draft = {"__typename": "DraftOrder", "id": DRAFT, "status": "OPEN",
                      "order": None, "paymentTerms": None,
                      "totalPriceSet": {"shopMoney": {"amount": "19.90"}}}
        self.order = {"__typename": "Order", "id": ORDER, "cancelledAt": None,
                      "closed": False, "displayFinancialStatus": "PENDING",
                      "displayFulfillmentStatus": "UNFULFILLED", "canMarkAsPaid": True}
        self.fulfillment = {"id": "gid://shopify/FulfillmentOrder/1", "status": "OPEN",
                            "requestStatus": "UNSUBMITTED",
                            "supportedActions": [{"action": "CREATE_FULFILLMENT"}]}
        self.mutations = []
        self.calls = []
        self.errors = set()
        self.missing = False
        self.more_fulfillments = False
        self.multiple_locations = False
        self.ambiguous_pay = False
        self.fail_refresh = False
        self.keep_pending_after_pay = False
        self.change_on_snapshot = False

    def respond(self, request):
        if request.url.path.endswith("/oauth/access_token"):
            return httpx.Response(200, json={"access_token": "fake-token", "expires_in": 3600})
        data = json.loads(request.content)
        query, variables = data["query"], data["variables"]
        self.calls.append(data)
        field = None
        if query.lstrip().startswith("mutation"):
            self.mutations.append(data)
            for operation, result in (("ConfirmDraft", "draftOrderComplete"),
                                      ("MarkOrderPaid", "orderMarkAsPaid"),
                                      ("PrepareOrder", "fulfillmentCreate")):
                if operation in query:
                    field = result
            if field in self.errors:
                return httpx.Response(200, json={"data": {field: {
                    "userErrors": [{"field": ["id"], "message": "private-server-detail"}],
                }}})
        if "InspectOrderState" in query:
            if self.fail_refresh and self.mutations:
                return httpx.Response(200, json={"errors": [{"message": "private-server-detail"}]})
            result = {"node": None if self.missing else copy.deepcopy(
                self.draft if variables["id"] == DRAFT else self.order)}
        elif "ConfirmDraft" in query:
            self.draft.update(status="COMPLETED", order={"id": ORDER})
            result = {field: {"draftOrder": copy.deepcopy(self.draft), "userErrors": []}}
        elif "MarkOrderPaid" in query:
            if not self.keep_pending_after_pay:
                self.order["displayFinancialStatus"] = "PAID"
            if self.ambiguous_pay:
                raise httpx.ReadError("private-server-detail")
            result = {field: {"order": {"id": ORDER}, "userErrors": []}}
        elif "InspectFulfillmentOrders" in query:
            result = {"order": {"fulfillmentOrders": {
                "nodes": [self.fulfillment] * (2 if self.multiple_locations else 1),
                "pageInfo": {"hasNextPage": self.more_fulfillments},
            }}}
        elif "PrepareOrder" in query:
            self.order["displayFulfillmentStatus"] = "FULFILLED"
            result = {field: {"fulfillment": {"id": "gid://shopify/Fulfillment/1"}, "userErrors": []}}
        elif "SyncOrder" in query:
            if self.change_on_snapshot:
                self.order["displayFinancialStatus"] = "REFUNDED"
            snapshot = example()
            snapshot.update(self.order)
            snapshot["displayFinancialStatus"] = self.order["displayFinancialStatus"]
            snapshot["displayFulfillmentStatus"] = self.order["displayFulfillmentStatus"]
            snapshot["lineItems"] = {"nodes": snapshot["lineItems"],
                                     "pageInfo": {"hasNextPage": False, "endCursor": None}}
            result = {"order": snapshot}
        else:
            raise AssertionError(query)
        return httpx.Response(200, json={"data": result})


class TransitionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.settings = Settings("prueba.myshopify.com", "fake-id", "fake-secret",
                                 Path(directory.name) / "local.sqlite")
        init_db(self.settings.db_path)
        self.store = Store()
        self.messages = []

    async def change(self, gid, target, **kwargs):
        async with httpx.AsyncClient(transport=httpx.MockTransport(self.store.respond)) as client:
            return await transition_order(ShopifyClient(self.settings, client), self.settings.db_path,
                                          gid, target, report=self.messages.append, **kwargs)

    def rows(self):
        with connect(self.settings.db_path) as db:
            return [dict(row) for row in db.execute("SELECT * FROM orders")]

    async def test_complete_flow_and_repeat_never_repeats_mutations(self):
        confirmed = await self.change(DRAFT, "CONFIRMADO")
        self.assertEqual((confirmed.gid, confirmed.state, confirmed.local_synced), (ORDER, "CONFIRMADO", True))
        self.assertEqual(self.rows()[0]["financial_status"], "PENDING")
        self.assertIn("paymentPending: true", self.store.mutations[0]["query"])
        self.assertIsNone(self.store.draft["paymentTerms"])
        self.assertFalse((await self.change(DRAFT, "CONFIRMADO")).changed)
        await self.change(ORDER, "PAGADO")
        self.assertEqual(self.rows()[0]["financial_status"], "PAID")
        self.assertFalse((await self.change(ORDER, "PAGADO")).changed)
        await self.change(ORDER, "ENVIADO")
        self.assertEqual(self.rows()[0]["fulfillment_status"], "FULFILLED")
        fulfillment = self.store.mutations[-1]["variables"]["fulfillment"]
        self.assertIs(fulfillment["notifyCustomer"], False)
        self.assertEqual(fulfillment["lineItemsByFulfillmentOrder"], [
            {"fulfillmentOrderId": "gid://shopify/FulfillmentOrder/1"},
        ])
        self.store.order["closed"] = True  # El pedido final puede archivarse.
        self.assertFalse((await self.change(ORDER, "PREPARADO")).changed)
        self.assertEqual(len(self.store.mutations), 3)  # Confirmar, pagar, preparar.
        self.assertEqual(len(self.rows()), 1)

    async def test_existing_terms_are_preserved(self):
        self.store.draft["paymentTerms"] = {"id": "gid://shopify/PaymentTerms/99"}
        await self.change(DRAFT, "CONFIRMADO")
        self.assertEqual(len(self.store.mutations), 1)
        self.assertIn("ConfirmDraft", self.store.mutations[0]["query"])

    async def test_confirmation_never_reads_or_writes_payment_terms(self):
        await self.change(DRAFT, "CONFIRMADO")
        self.assertEqual(len(self.store.mutations), 1)
        self.assertIsNone(self.store.draft["paymentTerms"])
        for call in self.store.calls:
            self.assertNotIn("paymentTerms", call["query"])
            self.assertNotIn("draftOrderUpdate", call["query"])

    async def test_noop_draft_and_dry_run_do_not_write(self):
        await self.change(DRAFT, "BORRADOR")
        before = self.settings.db_path.read_bytes()
        await self.change(DRAFT, "CONFIRMADO", dry_run=True)
        self.assertEqual(self.store.mutations, [])
        self.assertEqual(before, self.settings.db_path.read_bytes())

    async def test_backward_and_skipped_steps_are_rejected(self):
        for gid, target in ((ORDER, "NUEVO"), (DRAFT, "PAGADO"), (DRAFT, "ENVIADO")):
            with self.subTest(gid=gid, target=target), self.assertRaises(TransitionError):
                await self.change(gid, target)
        self.store.order["displayFinancialStatus"] = "PAID"
        with self.assertRaisesRegex(TransitionError, "ya ha avanzado"):
            await self.change(ORDER, "CONFIRMADO")
        self.assertEqual(self.store.mutations, [])

    async def test_bad_id_or_state_does_not_call_shopify(self):
        for gid, state in (("#6501", "PAGADO"), ("1", "PAGADO"), (ORDER, "REEMBOLSADO")):
            with self.subTest(gid=gid, state=state), self.assertRaises(TransitionError):
                await self.change(gid, state)
        self.assertEqual(self.store.calls, [])

    async def test_missing_order_cancelled_and_unsupported_states(self):
        self.store.missing = True
        with self.assertRaises(TransitionError):
            await self.change(ORDER, "PAGADO")
        self.store.missing = False
        self.store.order["cancelledAt"] = "2026-11-12T10:00:00Z"
        with self.assertRaises(TransitionError):
            await self.change(ORDER, "PAGADO")
        self.store.order["cancelledAt"] = None
        for status in ("AUTHORIZED", "PARTIALLY_PAID", "REFUNDED", "PARTIALLY_REFUNDED", "VOIDED"):
            self.store.order["displayFinancialStatus"] = status
            with self.subTest(status=status), self.assertRaises(TransitionError):
                await self.change(ORDER, "PAGADO")
        self.assertEqual(self.store.mutations, [])

    async def test_can_mark_paid_must_be_true(self):
        self.store.order["canMarkAsPaid"] = False
        with self.assertRaises(TransitionError):
            await self.change(ORDER, "PAGADO")
        self.assertEqual(self.store.mutations, [])

    async def test_partial_or_held_fulfillment_is_out_of_scope(self):
        self.store.order["displayFinancialStatus"] = "PAID"
        for status in ("PARTIALLY_FULFILLED", "ON_HOLD", "IN_PROGRESS"):
            self.store.order["displayFulfillmentStatus"] = status
            with self.subTest(status=status), self.assertRaises(TransitionError):
                await self.change(ORDER, "ENVIADO")
        self.assertEqual(self.store.mutations, [])

    async def test_fulfillment_pagination_or_multiple_locations_fail_before_write(self):
        self.store.order["displayFinancialStatus"] = "PAID"
        self.store.more_fulfillments = True
        with self.assertRaises(TransitionError):
            await self.change(ORDER, "ENVIADO")
        self.store.more_fulfillments = False
        self.store.multiple_locations = True
        with self.assertRaises(TransitionError):
            await self.change(ORDER, "ENVIADO")
        self.assertEqual(self.store.mutations, [])

    async def test_fulfillment_must_support_manual_creation(self):
        self.store.order["displayFinancialStatus"] = "PAID"
        self.store.fulfillment["supportedActions"] = []
        with self.assertRaises(TransitionError):
            await self.change(ORDER, "PREPARADO")
        self.assertEqual(self.store.mutations, [])

    async def test_zero_value_draft_is_rejected(self):
        self.store.draft["totalPriceSet"]["shopMoney"]["amount"] = "0"
        with self.assertRaises(TransitionError):
            await self.change(DRAFT, "CONFIRMADO")
        self.assertEqual(self.store.mutations, [])

    async def test_mutation_user_error_does_not_update_local_or_expose_detail(self):
        self.store.errors.add("orderMarkAsPaid")
        with self.assertRaises(TransitionError) as caught:
            await self.change(ORDER, "PAGADO")
        self.assertNotIn("private-server-detail", str(caught.exception))
        self.assertEqual(self.rows(), [])

    async def test_confirmation_failure_does_not_modify_terms_or_local_order(self):
        self.store.errors.add("draftOrderComplete")
        with self.assertRaises(TransitionError):
            await self.change(DRAFT, "CONFIRMADO")
        self.assertIsNone(self.store.draft["paymentTerms"])
        self.assertEqual(self.store.draft["status"], "OPEN")
        self.assertEqual(self.rows(), [])

    async def test_uncertain_payment_is_not_retried_and_next_run_refreshes_local(self):
        self.store.ambiguous_pay = True
        with self.assertRaisesRegex(TransitionError, "resultado remoto"):
            await self.change(ORDER, "PAGADO")
        self.assertEqual(len(self.store.mutations), 1)
        self.assertEqual(self.rows(), [])
        result = await self.change(ORDER, "PAGADO")
        self.assertFalse(result.changed)
        self.assertTrue(result.local_synced)
        self.assertEqual(len(self.store.mutations), 1)
        self.assertEqual(self.rows()[0]["financial_status"], "PAID")

    async def test_sqlite_failure_does_not_claim_remote_rollback(self):
        with patch("app.services.transitions.sync_order_snapshot", side_effect=sqlite3.OperationalError):
            with self.assertRaisesRegex(TransitionError, "no se revierte"):
                await self.change(ORDER, "PAGADO")
        self.assertEqual(self.store.order["displayFinancialStatus"], "PAID")
        await self.change(ORDER, "PAGADO")
        self.assertEqual(len(self.store.mutations), 1)
        self.assertEqual(self.rows()[0]["financial_status"], "PAID")

    async def test_post_mutation_must_verify_target(self):
        self.store.keep_pending_after_pay = True
        with self.assertRaisesRegex(TransitionError, "no PAGADO"):
            await self.change(ORDER, "PAGADO")
        self.assertEqual(self.rows()[0]["financial_status"], "PENDING")

    async def test_dry_run_rejects_zero_amount_like_real_execution(self):
        self.store.draft["totalPriceSet"]["shopMoney"]["amount"] = "0"
        for dry_run in (True, False):
            with self.subTest(dry_run=dry_run), self.assertRaisesRegex(TransitionError, "importe positivo"):
                await self.change(DRAFT, "CONFIRMADO", dry_run=dry_run)
        self.assertEqual(self.store.mutations, [])

    async def test_verifies_saved_snapshot_and_does_not_claim_paid_after_refund(self):
        self.store.change_on_snapshot = True
        with self.assertRaisesRegex(TransitionError, "fuera del flujo"):
            await self.change(ORDER, "PAGADO")
        self.assertEqual(self.rows()[0]["financial_status"], "REFUNDED")
        self.assertEqual(sum("InspectOrderState" in call["query"] for call in self.store.calls), 1)

    async def test_newer_local_copy_does_not_claim_synchronization_success(self):
        from app.storage.repository import save_order
        newer = example()
        newer.update(updatedAt="2026-12-01T00:00:00Z", displayFinancialStatus="REFUNDED")
        save_order(self.settings.db_path, None, newer)
        with self.assertRaisesRegex(TransitionError, "versión más reciente"):
            await self.change(ORDER, "PAGADO")
        self.assertEqual(self.rows()[0]["financial_status"], "REFUNDED")

    async def test_mutation_error_identifies_operation_and_safe_fields(self):
        self.store.errors.add("orderMarkAsPaid")
        with self.assertRaises(TransitionError) as caught:
            await self.change(ORDER, "PAGADO")
        self.assertIn("orderMarkAsPaid", str(caught.exception))
        self.assertIn("Campos: id", str(caught.exception))
        self.assertNotIn("private-server-detail", str(caught.exception))

    async def test_mutation_error_does_not_echo_unknown_fields_or_messages(self):
        shopify = SimpleNamespace(graphql=AsyncMock(return_value={"orderMarkAsPaid": {
            "userErrors": [{"field": ["input", "private-secret"], "message": "private-secret"}],
        }}))
        with self.assertRaises(TransitionError) as caught:
            await mutate(shopify, PAY_MUTATION, {"input": {"id": ORDER}}, "orderMarkAsPaid")
        self.assertIn("orderMarkAsPaid", str(caught.exception))
        self.assertNotIn("private-secret", str(caught.exception))

    async def test_cli_dry_run_does_not_create_database_and_returns_success(self):
        settings = Settings("prueba.myshopify.com", "fake", "fake", self.settings.db_path.parent / "missing.sqlite")
        client = httpx.AsyncClient(transport=httpx.MockTransport(self.store.respond))
        with patch("app.cli.change_order.Settings.from_env", return_value=settings), \
             patch("app.cli.change_order.httpx.AsyncClient", return_value=client), patch("builtins.print"):
            self.assertEqual(await change_order.run(ORDER, "PAGADO", dry_run=True), 0)
        self.assertFalse(settings.db_path.exists())
        self.assertEqual(self.store.mutations, [])
