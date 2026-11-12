import asyncio
import math
import time
from datetime import datetime, timezone

import httpx


ORDER_QUERY = """
query SyncOrder($id: ID!, $after: String) {
  order(id: $id) {
    id name createdAt updatedAt note
    displayFinancialStatus displayFulfillmentStatus
    currentTotalPriceSet { shopMoney { amount currencyCode } }
    lineItems(first: 100, after: $after) {
      nodes { id title quantity currentQuantity }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""

ORDERS_QUERY = """
query ListOrders($after: String) {
  orders(first: 100, after: $after, sortKey: ID, query: "status:any") {
    nodes { id }
    pageInfo { hasNextPage endCursor }
  }
}
"""


class ShopifyError(Exception):
    """Error recuperable sin incluir respuestas ni credenciales en el mensaje."""


def parse_date(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Fecha sin zona horaria")
    return parsed.astimezone(timezone.utc)


class ShopifyClient:
    def __init__(self, settings, client: httpx.AsyncClient):
        self.settings = settings
        self.client = client
        self._token = None
        self._refresh_at = 0
        self._token_lock = asyncio.Lock()

    async def get_token(self):
        async with self._token_lock:
            if self._token and time.monotonic() < self._refresh_at:
                return self._token
            started = time.monotonic()
            try:
                response = await self.client.post(
                    f"https://{self.settings.shop_domain}/admin/oauth/access_token",
                    data={"grant_type": "client_credentials", "client_id": self.settings.client_id,
                          "client_secret": self.settings.client_secret},
                )
                if response.status_code != 200:
                    raise ShopifyError(f"Solicitud de token: HTTP {response.status_code}; comprueba app, tienda y credenciales")
                payload = response.json()
                token = payload["access_token"]
                expires_in = float(payload["expires_in"])
                if not isinstance(token, str) or not token or not math.isfinite(expires_in) or expires_in <= 0:
                    raise ValueError()
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                raise ShopifyError("No se pudo obtener un token válido de Shopify") from None
            self._token = token
            self._refresh_at = started + expires_in - min(60, expires_in * 0.1)
            return token

    async def graphql(self, query, variables):
        url = (f"https://{self.settings.shop_domain}/admin/api/"
               f"{self.settings.api_version}/graphql.json")
        for attempt in range(2):
            token = await self.get_token()
            try:
                response = await self.client.post(
                    url, headers={"X-Shopify-Access-Token": token},
                    json={"query": query, "variables": variables},
                )
            except httpx.HTTPError:
                raise ShopifyError("Fallo de conexión con Shopify") from None
            if response.status_code == 401 and attempt == 0:
                self._refresh_at = 0
                continue
            if response.status_code != 200:
                raise ShopifyError(f"Shopify HTTP {response.status_code}")
            effective = response.headers.get("X-Shopify-API-Version")
            if effective and effective != self.settings.api_version:
                raise ShopifyError("La versión efectiva de Shopify no coincide")
            try:
                payload = response.json()
                if payload.get("errors"):
                    raise ShopifyError("Shopify devolvió errores GraphQL; comprueba permisos o límites de consulta")
                data = payload["data"]
                if not isinstance(data, dict):
                    raise ValueError()
            except (ValueError, KeyError, TypeError, AttributeError):
                raise ShopifyError("Respuesta GraphQL inválida") from None
            return data

    async def iter_order_ids(self):
        after = None
        seen = set()
        while True:
            data = await self.graphql(ORDERS_QUERY, {"after": after})
            try:
                connection = data["orders"]
                ids = [node["id"] for node in connection["nodes"]]
                page = connection["pageInfo"]
                more = page["hasNextPage"]
                cursor = page["endCursor"]
                if not isinstance(more, bool) or any(not isinstance(gid, str) for gid in ids):
                    raise ValueError()
                if more and (not cursor or cursor in seen):
                    raise ValueError()
            except (ValueError, KeyError, TypeError):
                raise ShopifyError("Página de pedidos incompleta o cursor inválido") from None
            for order_id in ids:
                yield order_id
            if not more:
                break
            seen.add(cursor)
            after = cursor

    async def fetch_order(self, order_id):
        after = None
        snapshot = None
        lines = []
        seen = set()
        while True:
            data = await self.graphql(ORDER_QUERY, {"id": order_id, "after": after})
            order = data.get("order")
            if not order or order["id"] != order_id:
                raise ShopifyError("Pedido no disponible")
            if snapshot and parse_date(snapshot["updatedAt"]) != parse_date(order["updatedAt"]):
                raise ShopifyError("El pedido cambió durante la paginación")
            snapshot = order
            connection = order["lineItems"]
            lines.extend(connection["nodes"])
            page = connection["pageInfo"]
            if not isinstance(page["hasNextPage"], bool):
                raise ShopifyError("Paginación de líneas incompleta")
            if not page["hasNextPage"]:
                break
            after = page["endCursor"]
            if not after or after in seen:
                raise ShopifyError("Cursor de paginación inválido")
            seen.add(after)
        ids = [line["id"] for line in lines]
        if len(ids) != len(set(ids)):
            raise ShopifyError("Respuesta con líneas duplicadas")
        snapshot["lineItems"] = lines
        return snapshot
