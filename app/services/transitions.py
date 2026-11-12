"""Flujo de demostración explícito: consultar Shopify, avanzar y refrescar SQLite."""

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from app.shopify.client import ShopifyError
from app.services.sync import sync_order_snapshot


STATES = ("BORRADOR", "CONFIRMADO", "PAGADO", "PREPARADO")

STATE_QUERY = """
query InspectOrderState($id: ID!) {
  node(id: $id) {
    __typename
    ... on DraftOrder {
      id status order { id }
      totalPriceSet { shopMoney { amount } }
    }
    ... on Order {
      id cancelledAt closed displayFinancialStatus displayFulfillmentStatus canMarkAsPaid
    }
  }
}
"""

CONFIRM_MUTATION = """
mutation ConfirmDraft($id: ID!) {
  draftOrderComplete(id: $id, paymentPending: true) {
    draftOrder { id order { id } }
    userErrors { field message }
  }
}
"""

PAY_MUTATION = """
mutation MarkOrderPaid($input: OrderMarkAsPaidInput!) {
  orderMarkAsPaid(input: $input) {
    order { id }
    userErrors { field message }
  }
}
"""

FULFILLMENT_QUERY = """
query InspectFulfillmentOrders($id: ID!) {
  order(id: $id) {
    fulfillmentOrders(first: 100) {
      nodes {
        id status requestStatus supportedActions { action }
      }
      pageInfo { hasNextPage }
    }
  }
}
"""

FULFILL_MUTATION = """
mutation PrepareOrder($fulfillment: FulfillmentInput!) {
  fulfillmentCreate(fulfillment: $fulfillment) {
    fulfillment { id }
    userErrors { field message }
  }
}
"""


class TransitionError(ValueError):
    """Mensaje controlado para el operador, sin respuestas remotas sensibles."""


@dataclass
class TransitionResult:
    gid: str
    state: str
    changed: bool
    local_synced: bool = False


def normalize_state(value):
    state = value.strip().upper()
    state = {"NUEVO": "BORRADOR", "ENVIADO": "PREPARADO"}.get(state, state)
    if state not in STATES:
        raise TransitionError("Estado no admitido. Usa BORRADOR, CONFIRMADO, PAGADO o PREPARADO (alias ENVIADO).")
    return state


def validate_gid(gid):
    if not isinstance(gid, str) or not re.fullmatch(r"gid://shopify/(DraftOrder|Order)/[1-9]\d*", gid):
        raise TransitionError("Usa el GID completo: gid://shopify/DraftOrder/123 o gid://shopify/Order/123; no el número # del pedido.")


def classify_state(node):
    """Mismas reglas para filtrar candidatos y validar el comando individual."""
    if node.get("__typename") == "DraftOrder":
        if node["status"] not in {"OPEN", "INVOICE_SENT"}:
            raise TransitionError("Estado de borrador fuera del flujo de demostración.")
        return "BORRADOR"
    if node.get("__typename") != "Order":
        raise TransitionError("El GID no corresponde a un pedido ni a un borrador.")
    if node["cancelledAt"] is not None:
        raise TransitionError("Pedido cancelado: no se permiten avances en este flujo.")
    financial = node["displayFinancialStatus"]
    fulfillment = node["displayFulfillmentStatus"]
    if financial == "PAID" and fulfillment == "FULFILLED":
        return "PREPARADO"
    if node["closed"]:
        raise TransitionError("Pedido cerrado fuera del estado final admitido.")
    if fulfillment == "UNFULFILLED":
        if financial == "PENDING":
            return "CONFIRMADO"
        if financial == "PAID":
            return "PAGADO"
    raise TransitionError("Estado de pago/preparación fuera del flujo simple; no se modifican autorizaciones, parciales, reembolsos ni pedidos retenidos.")


async def inspect_state(shopify, gid):
    validate_gid(gid)
    node = (await shopify.graphql(STATE_QUERY, {"id": gid})).get("node")
    if not node or node.get("id") != gid:
        raise TransitionError("Pedido o borrador no disponible; comprueba el GID y los permisos de lectura.")
    if node.get("__typename") == "DraftOrder" and node["status"] == "COMPLETED":
        order_id = (node.get("order") or {}).get("id")
        if not order_id or not order_id.startswith("gid://shopify/Order/"):
            raise TransitionError("El borrador está completado pero no se pudo obtener el GID del pedido.")
        return await inspect_state(shopify, order_id)
    return node, classify_state(node)


async def mutate(shopify, query, variables, field):
    payload = (await shopify.graphql(query, variables)).get(field)
    if not isinstance(payload, dict) or not isinstance(payload.get("userErrors"), list):
        raise TransitionError("Respuesta de operación inválida; consulta el estado remoto antes de repetir.")
    if payload["userErrors"]:
        # Solo nombres de operaciones y campos conocidos; nunca mensajes remotos
        # que puedan incluir datos personales o valores de entrada sensibles.
        operations = {"draftOrderComplete", "orderMarkAsPaid", "fulfillmentCreate"}
        fields = {"id", "input", "fulfillment", "lineItemsByFulfillmentOrder",
                  "fulfillmentOrderId", "fulfillmentOrderLineItems", "quantity",
                  "notifyCustomer", "paymentPending"}
        paths = []
        for error in payload["userErrors"]:
            path = error.get("field") if isinstance(error, dict) else None
            if isinstance(path, list) and path and len(path) <= 8 and all(
                isinstance(part, str) and (part in fields or (part.isdigit() and len(part) <= 6))
                for part in path
            ):
                paths.append(".".join(path))
        operation = field if field in operations else "la operación"
        details = ", ".join(sorted(set(paths))) or "no especificados"
        raise TransitionError(f"Shopify rechazó {operation}. Campos: {details}. "
                              "Comprueba permisos, saldo, inventario y estado actual; no se muestran valores ni mensajes remotos.")
    return payload


async def prepare_input(shopify, gid):
    order = (await shopify.graphql(FULFILLMENT_QUERY, {"id": gid})).get("order")
    if not order:
        raise TransitionError("No se pudieron consultar las órdenes de preparación.")
    connection = order["fulfillmentOrders"]
    if connection["pageInfo"]["hasNextPage"] is not False or len(connection["nodes"]) != 1:
        raise TransitionError("Esta versión requiere exactamente una orden de preparación visible; no admite pedidos divididos entre ubicaciones.")
    fulfillment_order = connection["nodes"][0]
    if (fulfillment_order["status"] != "OPEN"
            or fulfillment_order["requestStatus"] != "UNSUBMITTED"
            or "CREATE_FULFILLMENT" not in {a["action"] for a in fulfillment_order["supportedActions"]}):
        raise TransitionError("La orden de preparación no está disponible para preparación manual; no se modifican retenciones ni servicios externos.")
    # Omitir fulfillmentOrderLineItems prepara todas las unidades pendientes,
    # sin truncar el pedido por límites de paginación de líneas.
    return {"notifyCustomer": False,
            "lineItemsByFulfillmentOrder": [{"fulfillmentOrderId": fulfillment_order["id"]}]}


def validate_draft_amount(node):
    try:
        amount = Decimal(node["totalPriceSet"]["shopMoney"]["amount"])
    except (InvalidOperation, TypeError, KeyError):
        raise TransitionError("No se pudo validar el importe del borrador.") from None
    if not amount.is_finite() or amount <= 0:
        raise TransitionError("Usa un borrador con importe positivo para demostrar pago pendiente y pago posterior.")


async def confirm_draft(shopify, node):
    validate_draft_amount(node)
    # Disponible en la API 2026-10, aunque obsoleto: confirmar sin cobrar ni
    # crear/modificar paymentTerms. No quitar este argumento: su defecto es false.
    payload = await mutate(shopify, CONFIRM_MUTATION, {"id": node["id"]}, "draftOrderComplete")
    order_id = ((payload.get("draftOrder") or {}).get("order") or {}).get("id")
    validate_gid(order_id)
    if not order_id.startswith("gid://shopify/Order/"):
        raise TransitionError("Shopify no devolvió el GID del pedido confirmado.")
    return order_id


async def transition_order(shopify, db_path, gid, target, *, dry_run=False, report=None):
    if report is None:
        report = print
    target = normalize_state(target)
    node, current = await inspect_state(shopify, gid)
    gid = node["id"]
    report(f"GID actual: {gid}; estado actual del flujo: {current}.")
    if STATES.index(target) < STATES.index(current):
        raise TransitionError(f"No se puede pasar a {target}: el pedido ya ha avanzado a {current}.")
    if STATES.index(target) > STATES.index(current) + 1:
        raise TransitionError(f"No se puede saltar de {current} a {target}. Siguiente paso: {STATES[STATES.index(current) + 1]}.")
    if target == "PAGADO" and current == "CONFIRMADO" and node["canMarkAsPaid"] is not True:
        raise TransitionError("Shopify no permite marcar este pedido como pagado.")
    if target == "CONFIRMADO" and current == "BORRADOR":
        validate_draft_amount(node)
    fulfillment_input = None
    if target == "PREPARADO" and current == "PAGADO":
        fulfillment_input = await prepare_input(shopify, gid)
    if dry_run:
        report(f"Simulación: {current} -> {target}. No se modifica Shopify ni SQLite; Shopify validará la operación al ejecutarla.")
        return TransitionResult(gid, current, False)
    changed = current != target
    if changed:
        report("Ejecutando cambio en Shopify. Si se interrumpe, consulta el estado antes de repetir.")
        try:
            if target == "CONFIRMADO":
                gid = await confirm_draft(shopify, node)
            elif target == "PAGADO":
                await mutate(shopify, PAY_MUTATION, {"input": {"id": gid}}, "orderMarkAsPaid")
            elif target == "PREPARADO":
                await mutate(shopify, FULFILL_MUTATION, {"fulfillment": fulfillment_input}, "fulfillmentCreate")
        except TransitionError:
            raise
        except (ShopifyError, ValueError, KeyError, TypeError, AttributeError):
            raise TransitionError("No se pudo confirmar el resultado remoto. Shopify puede haber aplicado parte del cambio; consulta con --dry-run antes de repetir. No se revierte Shopify.") from None
    else:
        report("Ya está en el estado solicitado; no se repite ninguna mutation.")
    if gid.startswith("gid://shopify/DraftOrder/"):
        return TransitionResult(gid, current, False)
    # Shopify y SQLite no comparten transacción. Leer/guardar después de la mutation
    # también permite recuperar SQLite al repetir un comando ya aplicado.
    report(f"GID del pedido para siguientes pasos y consultas: {gid}.")
    try:
        snapshot = await sync_order_snapshot(shopify, db_path, gid)
    except Exception:
        raise TransitionError(f"Revisa Shopify para {gid}: el cambio puede estar aplicado, pero no se pudo verificar/refrescar SQLite. Repite la consulta o sincroniza; no se revierte el cambio remoto.") from None
    if not snapshot.saved:
        raise TransitionError("No se confirma el resultado local: SQLite conserva una versión más reciente. Consulta el estado y sincroniza antes de continuar.")
    try:
        actual = classify_state(snapshot.order)
    except TransitionError:
        raise TransitionError("SQLite refrescada con la respuesta de Shopify, pero el pedido está fuera del flujo solicitado. No se registra como éxito ni se aplican más cambios.") from None
    if actual != target:
        raise TransitionError(f"SQLite refrescada, pero Shopify indica {actual}, no {target}. No se aplican más cambios automáticamente.")
    report(f"Estado verificado: {actual}. SQLite refrescada.")
    return TransitionResult(gid, actual, changed, True)
