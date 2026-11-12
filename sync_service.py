"""Un único flujo de lectura y guardado para carga inicial y webhooks."""

from repository import save_order


async def sync_order(shopify, db_path, order_id, job_id=None):
    order = await shopify.fetch_order(order_id)
    return save_order(db_path, job_id, order)
