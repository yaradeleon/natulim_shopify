import asyncio
import base64
import hashlib
import hmac
import json
import logging
import re
import sqlite3
from contextlib import asynccontextmanager, suppress

import httpx
from fastapi import FastAPI, HTTPException, Request

from app.config import ConfigurationError, Settings
from app.storage.database import init_db
from app.storage.repository import enqueue, next_job, retry_job
from app.shopify.client import ShopifyClient
from app.services.sync import sync_order

logger = logging.getLogger(__name__)


async def process_one(settings, shopify):
    job = next_job(settings.db_path)
    if not job:
        return False
    try:
        await asyncio.wait_for(
            sync_order(
                shopify,
                settings.db_path,
                job["order_id"],
                job["delivery_id"],
            ),
            timeout=60,
        )
    except Exception as exc:
        # Solo la clase: no registrar cuerpos de pedidos, tokens ni respuestas.
        code = type(exc).__name__
        retry_job(settings.db_path, job, code)
        logger.warning("Sincronización pendiente; causa=%s", code)
    return True


async def worker(settings, shopify):
    while True:
        try:
            processed = await process_one(settings, shopify)
        except sqlite3.Error:
            logger.error("Base de datos temporalmente no disponible")
            processed = False
        await asyncio.sleep(0.1 if processed else 1)


def create_app(settings=None, shopify_client=None, start_worker=True):
    @asynccontextmanager
    async def lifespan(app):
        try:
            config = settings or Settings.from_env()
        except ConfigurationError as exc:
            # El mensaje agrupa todos los problemas y nunca contiene sus valores.
            raise RuntimeError(str(exc)) from None
        init_db(config.db_path)
        app.state.settings = config
        async with httpx.AsyncClient(timeout=15) as client:
            shopify = shopify_client or ShopifyClient(config, client)
            task = (
                asyncio.create_task(worker(config, shopify)) if start_worker else None
            )
            try:
                yield
            finally:
                if task:
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task

    app = FastAPI(title="Natulim Shopify Sync", lifespan=lifespan)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.post("/shopify/webhook/orders/create")
    @app.post("/shopify/webhook/orders/updated")
    @app.post("/webhooks/orders")
    async def orders_webhook(request: Request):
        config = request.app.state.settings
        body = await request.body()
        expected = base64.b64encode(
            hmac.new(
                config.signing_secret.encode(),
                body,
                hashlib.sha256,
            ).digest()
        ).decode("ascii")
        supplied = request.headers.get("X-Shopify-Hmac-Sha256", "")
        if not hmac.compare_digest(expected.encode(), supplied.encode()):
            raise HTTPException(401, "Firma inválida")
        if (
            request.headers.get("X-Shopify-Shop-Domain", "").lower()
            != config.shop_domain
        ):
            raise HTTPException(403, "Tienda no autorizada")
        topic = request.headers.get("X-Shopify-Topic", "")
        if topic not in {"orders/create", "orders/updated"}:
            raise HTTPException(400, "Tema no admitido")
        route_topics = {
            "/shopify/webhook/orders/create": "orders/create",
            "/shopify/webhook/orders/updated": "orders/updated",
        }
        if request.url.path in route_topics and topic != route_topics[request.url.path]:
            raise HTTPException(400, "El tema no coincide con la ruta")
        delivery_id = request.headers.get("X-Shopify-Webhook-Id", "")
        if not delivery_id or len(delivery_id) > 255:
            raise HTTPException(400, "Falta un identificador de entrega válido")
        try:
            payload = json.loads(body)
            order_id = payload.get("admin_graphql_api_id")
            if not order_id:
                numeric = payload.get("id")
                if isinstance(numeric, bool) or not re.fullmatch(
                    r"[1-9]\d*", str(numeric)
                ):
                    raise ValueError()
                order_id = f"gid://shopify/Order/{numeric}"
            if not isinstance(order_id, str) or not re.fullmatch(
                r"gid://shopify/Order/[1-9]\d*", order_id
            ):
                raise ValueError()
        except (ValueError, AttributeError, UnicodeDecodeError):
            raise HTTPException(400, "JSON o identificador de pedido inválido")
        try:
            added = enqueue(config.db_path, delivery_id, order_id, topic)
        except sqlite3.Error:
            raise HTTPException(503, "No se pudo guardar la notificación")
        return {"status": "queued" if added else "duplicate"}

    return app


app = create_app()
