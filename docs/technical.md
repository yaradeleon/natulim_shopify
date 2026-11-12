# Diseño técnico

## Flujo

`sync_orders.py` carga `.env` con `Settings`, inicializa la base e inicia una
exportación con `bulkOperationRunQuery` de pedidos y todas sus líneas. `bulk_orders.py`
consulta `bulkOperation(id:)` cada cinco segundos hasta `COMPLETED` (límite local de
una hora). Descarga JSONL sin token Admin API, valida estructura, IDs duplicados,
relaciones padre/hijo y número de objetos frente a `objectCount`, usando una base
temporal en disco. Solo entonces entrega los pedidos completos a `save_order`, el
mismo guardado que utiliza `sync_service.sync_order` para los webhooks. No usa
`partialDataUrl`. No requiere suscripción a `bulk_operations/finish`.
Cada pedido se confirma por separado; un fallo de escritura no revierte los ya
importados. Exportación/descarga incompleta no modifica la base de destino.
El recorrido paginado anterior se conserva para pruebas mediante `bulk=False`;
el comando usa Bulk por defecto. Interrumpir el comando no cancela la operación
remota; una nueva ejecución inicia otra exportación.

`ShopifyClient` intercambia Client ID y Client Secret en
`/admin/oauth/access_token` con `grant_type=client_credentials`. Conserva el token
en memoria hasta `expires_in` menos un margen máximo de 60 segundos. Un lock evita
renovaciones concurrentes; cada proceso tiene su propia caché. Un 401 renueva y
repite una vez. No se persisten tokens ni se modifica `.env`. Para webhooks se
usa el Client Secret salvo que `SHOPIFY_WEBHOOK_SECRET` configure otro secreto.

`POST /webhooks/orders` valida HMAC-SHA256 sobre los bytes originales, dominio,
tema e identificador de entrega. Obtiene el GID de `admin_graphql_api_id` o, como
alternativa, de `id`. No toma los importes ni las líneas del webhook como estado
definitivo: consulta `order(id: ...)` en GraphQL Admin.

El receptor confirma HTTP 200 solo después de persistir `webhook_jobs`. El GID,
tema y entrega son los únicos datos del evento guardados. Una entrega duplicada
no crea otro trabajo. Si SQLite falla, responde 503 para permitir reentrega.
Una firma incorrecta devuelve 401; dominio distinto, 403; tema/cuerpo inválido, 400.

Un consumidor en el lifespan de FastAPI consulta trabajos pendientes cada segundo.
Ante fallo, conserva el trabajo y reintenta con espera exponencial de 2 a 60 segundos.
No hay máximo de intentos: un error permanente necesita intervención. `last_error`
almacena solo la clase del error, sin respuestas, cuerpos ni secretos. Una operación
GraphQL completa tiene un límite de 60 segundos y cada petición HTTP de 15.

La consulta está en `shopify.py`, `ORDER_QUERY`. Recorre todas las páginas de
`lineItems` con 100 elementos por página. Rechaza errores GraphQL incluso con datos
parciales, pedido nulo, versión efectiva distinta si se informa en la cabecera,
cursores repetidos, líneas duplicadas y cambios de `updatedAt` entre páginas.
Una respuesta inconsistente se vuelve a intentar desde el principio.

## Consistencia en SQLite

`save_order` abre una transacción `BEGIN IMMEDIATE`, compara fechas con zona
horaria y actualiza por GID. Una versión anterior no sobrescribe la guardada.
Pedido, reemplazo de sus líneas y estado `done` del trabajo se confirman juntos.
Un fallo en cualquier línea revierte toda la operación. Reprocesar tras un cierre
inesperado es seguro: un trabajo queda pendiente hasta confirmar su transacción.
Los identificadores de línea se conservan aunque se reemplace su representación
local. No hay otras tablas que referencien esas filas en esta versión.

`init_db.py` es repetible; añade tablas ausentes sin borrar datos, pero no migra
columnas existentes. Cada conexión habilita claves foráneas. Los importes se guardan
como texto decimal y las fechas se normalizan a UTC. No se usan floats para dinero.

## Alcance y límites

- Una tienda configurada y un proceso Uvicorn por base. La cola no implementa
  reclamación de trabajos entre múltiples consumidores.
- El alcance incluye carga inicial accesible, alta y modificación. No incluye
  borrado, reconciliación periódica ni garantía de recuperar eventos que nunca lleguen.
  La carga manual puede repetirse. El acceso histórico depende de los permisos.
- La paginación no crea una instantánea global de la tienda. Ante cambios durante
  la carga, los webhooks y una nueva ejecución permiten converger al estado actual.
  En consultas individuales se descarta un pedido si cambia entre páginas.
  Bulk no proporciona una instantánea transaccional global de la tienda.
- La carga inicial informa throttling/errores para repetir manualmente; los
  webhooks conservan sus reintentos. No se implementa un planificador de cuotas.
- Los trabajos terminados se conservan para deduplicar; falta una política de purga.
- `/health` es una comprobación de disponibilidad HTTP, no monitorización del
  consumidor ni de credenciales. Inspeccionar trabajos pendientes durante la prueba.
- SQLite y consultas breves de cola se ejecutan en el proceso web; para mayor
  volumen se separaría el consumidor y se añadirían métricas y una cola adecuada.
- Las pruebas automatizadas simulan Shopify. En la prueba manual se recibió
  un webhook de actualización con HTTP 200 y el usuario confirmó el update.
  Falta registrar la inspección local del pedido nuevo y el resumen de carga.
  La introspección aportada describe `Order`; no se adjuntó una respuesta de
  pedido de Postman que valide todos los valores almacenados.

## Evolución: clientes

El siguiente diseño puede añadir `customers` con `Customer.id` como clave primaria
y `orders.customer_id` nullable como clave foránea. Al consultar un pedido se
obtendría su cliente y se haría un UPSERT local dentro de la misma transacción.
No se identificaría por email. Deben contemplarse pedidos sin cliente y permisos
de datos protegidos. Esto significa crear el cliente en SQLite si falta; crear
clientes en Shopify sería una funcionalidad distinta que habría que definir.

## Referencias

- [Verificación de webhooks](https://shopify.dev/docs/apps/build/webhooks/verify-deliveries).
- [Consulta order, API 2026-10](https://shopify.dev/docs/api/admin-graphql/2026-10/queries/order).
- [LineItem](https://shopify.dev/docs/api/admin-graphql/2026-10/objects/LineItem).
- [Lifespan de FastAPI](https://fastapi.tiangolo.com/advanced/events/).
- [Client credentials](https://shopify.dev/docs/apps/build/authentication-authorization/client-credentials-grant).
- [Listado orders](https://shopify.dev/docs/api/admin-graphql/2026-10/queries/orders).
