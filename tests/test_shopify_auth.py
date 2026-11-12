import asyncio
import json
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs

import httpx

from app.config import Settings
from app.shopify.client import ShopifyClient, ShopifyError


class AuthTests(unittest.IsolatedAsyncioTestCase):
    async def test_token_is_cached_and_renewed_before_expiry(self):
        calls = []

        def respond(request):
            calls.append(request)
            return httpx.Response(
                200, json={"access_token": f"token-{len(calls)}", "expires_in": 120}
            )

        settings = Settings("prueba.myshopify.com", "id-ficticio", "secreto-ficticio")
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            shopify = ShopifyClient(settings, client)
            # Un reloj propio evita alterar los tiempos internos del event loop.
            with patch("app.shopify.client.time") as clock:
                clock.monotonic.return_value = 1000
                self.assertEqual(await shopify.get_token(), "token-1")
                clock.monotonic.return_value = 1100
                self.assertEqual(await shopify.get_token(), "token-1")
                clock.monotonic.return_value = 1109
                self.assertEqual(await shopify.get_token(), "token-2")
        self.assertEqual(len(calls), 2)
        self.assertEqual(
            parse_qs(calls[0].content.decode()),
            {
                "grant_type": ["client_credentials"],
                "client_id": ["id-ficticio"],
                "client_secret": ["secreto-ficticio"],
            },
        )

    async def test_401_renews_once_and_retries_same_query(self):
        tokens = []
        queries = []

        def respond(request):
            if request.url.path.endswith("/oauth/access_token"):
                token = f"token-{len(tokens) + 1}"
                tokens.append(token)
                return httpx.Response(
                    200, json={"access_token": token, "expires_in": 3600}
                )
            queries.append(request)
            if len(queries) == 1:
                return httpx.Response(401)
            return httpx.Response(200, json={"data": {"test": True}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            shopify = ShopifyClient(
                Settings("prueba.myshopify.com", "id", "secret"), client
            )
            self.assertEqual(
                await shopify.graphql("query { test }", {}), {"test": True}
            )
        self.assertEqual(tokens, ["token-1", "token-2"])
        self.assertEqual(queries[1].headers["X-Shopify-Access-Token"], "token-2")
        self.assertEqual(json.loads(queries[0].content), json.loads(queries[1].content))

    async def test_token_errors_do_not_expose_response_or_credentials(self):
        for status, payload in [
            (400, {"error": "secret-from-server"}),
            (200, {"access_token": "secret-from-server", "expires_in": 0}),
            (200, {"access_token": "secret-from-server"}),
        ]:
            with self.subTest(status=status, payload=payload):
                async with httpx.AsyncClient(
                    transport=httpx.MockTransport(
                        lambda request: httpx.Response(status, json=payload)
                    )
                ) as client:
                    shopify = ShopifyClient(
                        Settings("prueba.myshopify.com", "id", "private-secret"), client
                    )
                    with self.assertRaises(ShopifyError) as caught:
                        await shopify.get_token()
                    self.assertNotIn("secret-from-server", str(caught.exception))
                    self.assertNotIn("private-secret", str(caught.exception))

    async def test_concurrent_requests_share_token(self):
        count = 0

        async def respond(request):
            nonlocal count
            count += 1
            await asyncio.sleep(0)
            return httpx.Response(
                200, json={"access_token": "fake", "expires_in": 3600}
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            shopify = ShopifyClient(
                Settings("prueba.myshopify.com", "id", "secret"), client
            )
            self.assertEqual(
                await asyncio.gather(shopify.get_token(), shopify.get_token()),
                ["fake", "fake"],
            )
        self.assertEqual(count, 1)
