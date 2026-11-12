-- GID completos de Shopify; fechas ISO 8601 normalizadas a UTC por la integración.
-- Los importes se conservan como texto decimal, sin conversión a float.
CREATE TABLE IF NOT EXISTS orders (
    shopify_order_id TEXT PRIMARY KEY NOT NULL,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    note TEXT,
    financial_status TEXT,
    fulfillment_status TEXT NOT NULL,
    current_total_amount TEXT NOT NULL,
    currency_code TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS order_line_items (
    shopify_line_item_id TEXT PRIMARY KEY NOT NULL,
    order_id TEXT NOT NULL,
    title TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity >= 0),
    current_quantity INTEGER NOT NULL CHECK (current_quantity >= 0),
    FOREIGN KEY (order_id) REFERENCES orders (shopify_order_id)
);

CREATE INDEX IF NOT EXISTS idx_order_line_items_order_id
    ON order_line_items (order_id);

-- Cola durable local: no almacena el cuerpo del webhook ni datos personales.
CREATE TABLE IF NOT EXISTS webhook_jobs (
    delivery_id TEXT PRIMARY KEY NOT NULL,
    order_id TEXT NOT NULL,
    topic TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'done')),
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt REAL NOT NULL DEFAULT 0,
    last_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_webhook_jobs_pending
    ON webhook_jobs (status, next_attempt);

-- Punto de control de cargas completas/incrementales correctas. Los webhooks
-- no lo avanzan: recibir un pedido no demuestra que no falten otros.
CREATE TABLE IF NOT EXISTS sync_state (
    shop_domain TEXT PRIMARY KEY NOT NULL,
    last_successful_sync_at TEXT NOT NULL
);
