"""Seleccionar GID por estado y avanzar lotes reutilizando transition_order."""

import asyncio
import json
import math
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from order_transitions import STATES, TransitionError, classify_state, normalize_state, transition_order, validate_gid


LIST_DRAFTS_QUERY = """
query SelectDraftBatch($after: String, $query: String!) {
  draftOrders(first: 100, after: $after, sortKey: ID, query: $query) {
    nodes { __typename id status }
    pageInfo { hasNextPage endCursor }
  }
}
"""

LIST_ORDERS_QUERY = """
query SelectOrderBatch($after: String, $query: String!) {
  orders(first: 100, after: $after, sortKey: ID, query: $query) {
    nodes { __typename id cancelledAt closed displayFinancialStatus displayFulfillmentStatus }
    pageInfo { hasNextPage endCursor }
  }
}
"""

FILTERS = {
    "BORRADOR": "status:OPEN OR status:INVOICE_SENT",
    "CONFIRMADO": "status:open AND financial_status:pending AND fulfillment_status:unfulfilled",
    "PAGADO": "status:open AND financial_status:paid AND fulfillment_status:unfulfilled",
    "PREPARADO": "status:any AND financial_status:paid AND fulfillment_status:fulfilled",
}


def manifest(shop_domain, state, gids, **extra):
    return {"version": 1, "shop_domain": shop_domain, "state": state,
            "created_at": datetime.now(timezone.utc).isoformat(), "gids": gids, **extra}


def write_manifest(path, data, *, create=False):
    """No sobrescribir otro lote al crearlo; resultados posteriores, atómicos."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if create:
        with path.open("x", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        return
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=path.name + ".", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def read_manifest(path, shop_domain):
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1:
        raise TransitionError("Archivo de lote inválido o versión no admitida.")
    if data.get("shop_domain") != shop_domain:
        raise TransitionError("El lote pertenece a otra tienda; no se procesa.")
    if data.get("dry_run"):
        raise TransitionError("Una simulación no es una lista de pedidos actualizados. Usa el archivo de entrada original.")
    state = normalize_state(data.get("state", ""))
    gids = data.get("gids")
    if not isinstance(gids, list):
        raise TransitionError("El lote necesita una lista gids.")
    unique = []
    seen = set()
    for gid in gids:
        validate_gid(gid)
        expected = "DraftOrder" if state == "BORRADOR" else "Order"
        if not gid.startswith(f"gid://shopify/{expected}/"):
            raise TransitionError("El tipo de GID no coincide con el estado del lote.")
        if gid not in seen:
            unique.append(gid)
            seen.add(gid)
    return state, unique


async def select_batch(shopify, state, *, limit=20):
    """Lista remota paginada, sin una petición de estado por cada GID."""
    state = normalize_state(state)
    if type(limit) is not int or limit < 0:
        raise TransitionError("El límite debe ser un entero >= 0; 0 recorre todas las páginas.")
    query = LIST_DRAFTS_QUERY if state == "BORRADOR" else LIST_ORDERS_QUERY
    field = "draftOrders" if state == "BORRADOR" else "orders"
    gids, seen_ids, seen_cursors = [], set(), set()
    after = None
    while True:
        connection = (await shopify.graphql(query, {"after": after, "query": FILTERS[state]}))[field]
        nodes, page = connection["nodes"], connection["pageInfo"]
        if not isinstance(nodes, list) or type(page["hasNextPage"]) is not bool:
            raise TransitionError("Página del listado inválida; no se guarda una selección parcial.")
        for index, node in enumerate(nodes):
            gid = node["id"]
            validate_gid(gid)
            if gid in seen_ids:
                continue
            seen_ids.add(gid)
            try:
                matches = classify_state(node) == state
            except TransitionError:
                matches = False
            if matches:
                gids.append(gid)
                if limit and len(gids) >= limit:
                    return manifest(shopify.settings.shop_domain, state, gids,
                                    limited=page["hasNextPage"] or index + 1 < len(nodes))
        if not page["hasNextPage"]:
            return manifest(shopify.settings.shop_domain, state, gids, limited=False)
        after = page["endCursor"]
        if not isinstance(after, str) or not after or after in seen_cursors:
            raise TransitionError("Cursor del listado inválido; no se guarda una selección parcial.")
        seen_cursors.add(after)


async def apply_batch(shopify, db_path, source_path, target, output_path, *,
                      dry_run=False, delay=0.5, report=None):
    if report is None:
        report = print
    if not math.isfinite(delay) or delay < 0:
        raise TransitionError("La pausa debe ser un número finito >= 0.")
    state, gids = read_manifest(source_path, shopify.settings.shop_domain)
    target = normalize_state(target)
    if STATES.index(state) == len(STATES) - 1 or target != STATES[STATES.index(state) + 1]:
        raise TransitionError("El lote solo puede avanzar al siguiente estado: BORRADOR -> CONFIRMADO -> PAGADO -> PREPARADO.")
    if Path(source_path).resolve() == Path(output_path).resolve():
        raise TransitionError("La salida debe ser distinta del archivo de entrada.")
    output = manifest(shopify.settings.shop_domain, target, [], source_state=state,
                      dry_run=dry_run, complete=False, results=[])
    # Comprobar que la salida se puede crear ANTES de cualquier mutation.
    write_manifest(output_path, output, create=True)
    for index, gid in enumerate(gids):
        report(f"[{index + 1}/{len(gids)}] {gid}")
        try:
            result = await transition_order(shopify, db_path, gid, target,
                                            dry_run=dry_run, report=report)
        except Exception as exc:
            # Mensajes propios controlados; nunca imprimir respuestas remotas.
            detail = str(exc) if isinstance(exc, TransitionError) else type(exc).__name__
            row = {"input_gid": gid, "status": "failed", "error": detail}
            report(f"Fallo en {gid}: {detail}")
        else:
            status = "simulated" if dry_run else ("updated" if result.changed else "unchanged")
            row = {"input_gid": gid, "gid": result.gid, "state": result.state, "status": status}
            if not dry_run and result.state == target:
                if result.gid not in output["gids"]:
                    output["gids"].append(result.gid)
        output["results"].append(row)
        write_manifest(output_path, output)
        if delay and index + 1 < len(gids):
            await asyncio.sleep(delay)
    output["complete"] = True
    write_manifest(output_path, output)
    counts = {status: sum(r["status"] == status for r in output["results"])
              for status in ("updated", "unchanged", "simulated", "failed")}
    report(f"Resumen: {counts['updated']} actualizados; {counts['unchanged']} ya estaban; "
           f"{counts['simulated']} simulados; {counts['failed']} fallidos. Resultados: {output_path}")
    return output
