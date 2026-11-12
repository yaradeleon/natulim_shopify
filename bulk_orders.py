"""Exportación masiva: validar el archivo completo antes de modificar la base local."""

import asyncio
import json
import sqlite3
import tempfile
import time
from contextlib import closing
from datetime import timezone

import httpx

from shopify import ShopifyError


BULK_QUERY = """
{
  orders(query: "status:any", sortKey: UPDATED_AT) {
    edges { node {
      id name createdAt updatedAt note
      displayFinancialStatus displayFulfillmentStatus
      currentTotalPriceSet { shopMoney { amount currencyCode } }
      lineItems { edges { node { id title quantity currentQuantity } } }
    } }
  }
}
"""

START_QUERY = """
mutation StartOrderExport($query: String!) {
  bulkOperationRunQuery(query: $query) {
    bulkOperation { id }
    userErrors { field message }
  }
}
"""

STATUS_QUERY = """
query OrderExportStatus($id: ID!) {
  bulkOperation(id: $id) { id status errorCode objectCount url }
}
"""


def build_bulk_query(updated_since=None, updated_before=None):
    filters = ["status:any"]
    for comparator, value in ((">=", updated_since), ("<=", updated_before)):
        if value is not None:
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("La fecha del filtro necesita zona horaria")
            timestamp = value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
            filters.append(f"updated_at:{comparator}'{timestamp}'")
    return BULK_QUERY.replace('"status:any"', json.dumps(" AND ".join(filters)))


async def iter_bulk_orders(shopify, report, poll_seconds=5, max_wait=3600, *,
                           updated_since=None, updated_before=None):
    query = build_bulk_query(updated_since, updated_before)
    data = await shopify.graphql(START_QUERY, {"query": query})
    started = data["bulkOperationRunQuery"]
    if started.get("userErrors") or not started.get("bulkOperation"):
        raise ShopifyError("No se pudo iniciar la exportación; comprueba permisos de bulk queries")
    operation_id = started["bulkOperation"]["id"]
    report(f"Exportación iniciada: {operation_id}. Esperando a Shopify...")
    deadline = time.monotonic() + max_wait
    while True:
        operation = (await shopify.graphql(STATUS_QUERY, {"id": operation_id}))["bulkOperation"]
        status = operation["status"]
        report(f"Shopify: {status}; objetos exportados (pedidos y líneas): {operation['objectCount']}.")
        if status == "COMPLETED":
            break
        if status not in {"CREATED", "RUNNING"}:
            raise ShopifyError("La exportación no terminó correctamente; copia local conservada")
        if time.monotonic() >= deadline:
            raise ShopifyError("Tiempo de espera agotado; la exportación puede seguir activa en Shopify")
        await asyncio.sleep(poll_seconds)

    expected = int(operation["objectCount"])
    url = operation["url"]
    if not url:
        if expected != 0:
            raise ShopifyError("Exportación sin archivo de resultados")
        return
    if not isinstance(url, str) or not url.startswith("https://"):
        raise ShopifyError("URL de exportación inválida")

    # Archivo temporal en disco: no acumular miles de pedidos/líneas en RAM.
    # La descarga no lleva el token Admin API ni registra la URL firmada.
    with tempfile.TemporaryDirectory(prefix="shopify-bulk-") as directory:
        with closing(sqlite3.connect(f"{directory}/export.sqlite")) as staging:
            staging.execute("CREATE TABLE orders (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            staging.execute("CREATE TABLE lines (id TEXT PRIMARY KEY, parent TEXT NOT NULL, payload TEXT NOT NULL)")
            staging.execute("CREATE INDEX lines_parent ON lines(parent)")
            count = 0
            report("Descargando y validando pedidos y líneas...")
            async with shopify.client.stream("GET", url, timeout=httpx.Timeout(120, connect=15)) as response:
                if response.status_code != 200:
                    raise ShopifyError("No se pudo descargar la exportación")
                async for raw in response.aiter_lines():
                    if not raw.strip():
                        continue
                    item = json.loads(raw)
                    gid = item["id"]
                    if gid.startswith("gid://shopify/Order/") and "__parentId" not in item:
                        staging.execute("INSERT INTO orders VALUES (?, ?)", (gid, raw))
                    elif gid.startswith("gid://shopify/LineItem/") and item.get("__parentId"):
                        staging.execute("INSERT INTO lines VALUES (?, ?, ?)", (gid, item["__parentId"], raw))
                    else:
                        raise ShopifyError("Objeto inesperado en la exportación")
                    count += 1
            if count != expected:
                raise ShopifyError("Archivo de exportación incompleto; copia local conservada")
            if staging.execute("SELECT 1 FROM lines LEFT JOIN orders ON lines.parent = orders.id WHERE orders.id IS NULL LIMIT 1").fetchone():
                raise ShopifyError("Exportación con líneas sin pedido")
            report("Exportación completa validada. Guardando en SQLite...")
            for gid, payload in staging.execute("SELECT id, payload FROM orders"):
                order = json.loads(payload)
                order["lineItems"] = [json.loads(row[0]) for row in staging.execute(
                    "SELECT payload FROM lines WHERE parent = ?", (gid,),
                )]
                yield order
