"""Fechas compartidas por la persistencia y el cliente Shopify."""

from datetime import datetime, timezone


def parse_date(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Fecha sin zona horaria")
    return parsed.astimezone(timezone.utc)
