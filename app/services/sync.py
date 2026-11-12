"""Un único flujo de lectura y guardado para carga inicial y webhooks."""

from dataclasses import dataclass

from app.storage.repository import save_order


@dataclass
class SyncedOrder:
    order: dict
    saved: bool


async def sync_order_snapshot(shopify, db_path, order_id, job_id=None):
    """Devuelve exactamente la respuesta completa que se intentó guardar."""
    order = await shopify.fetch_order(order_id)
    return SyncedOrder(order, save_order(db_path, job_id, order))


async def sync_order(shopify, db_path, order_id, job_id=None):
    return (await sync_order_snapshot(shopify, db_path, order_id, job_id)).saved
