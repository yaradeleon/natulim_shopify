# Diseño técnico

## Flujo

`sync_orders.py` carga `.env` con `Settings`, inicializa la base e inicia una
exportación con `bulkOperationRunQuery` de pedidos y todas sus líneas. `app/shopify/bulk.py`
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

## Sincronización incremental

`sync_orders.py --incremental` reutiliza el mismo flujo Bulk. Construye el filtro
`status:any AND updated_at:>='DESDE' AND updated_at:<='HASTA'` con fechas UTC y
`sortKey: UPDATED_AT`. DESDE es el punto anterior menos cinco minutos; HASTA es
la hora de inicio actual, redondeada hacia abajo al segundo. El filtro se inserta
en la consulta GraphQL usando codificación JSON, sin concatenar entradas de usuario.

`sync_state(shop_domain, last_successful_sync_at)` persiste ese punto de control.
Una carga completa correcta también lo establece; una base anterior sin marcador
requiere repetir esa carga una vez. No se infiere de `MAX(orders.updated_at)` ni
de los webhooks. `init_db` añade la nueva tabla sin borrar ni alterar datos previos.

Se avanza tras validar la exportación y terminar todos los guardados sin fallos,
incluidas cargas vacías. Se guarda la hora de inicio, nunca la de fin: evita saltar
modificaciones producidas durante la descarga. Los errores dejan el marcador
anterior y permiten repetir; las confirmaciones por pedido se conservan. Un cierre
después de guardar pedidos pero antes del marcador provoca repetición segura.
El marcador se actualiza en transacción con comparación del valor leído al inicio
(compare-and-set): una carga concurrente no sobrescribe el marcador de otra.
Se rechaza un marcador futuro para no retroceder ante un reloj local incorrecto.

No se añade un segundo recorrido paginado ni pandas. El comando se ejecuta
manualmente y no necesita FastAPI. El solapamiento mitiga indexación tardía y
límites de tiempo; no garantiza cubrir retrasos superiores a cinco minutos,
desfases grandes de reloj, cambios de permisos ni pedidos borrados. La carga
completa sigue disponible para reconciliar todos los pedidos accesibles.
El validador local de la skill Shopify comprobó la consulta con su esquema
empaquetado 2026-04; el filtro se contrastó además con la documentación 2026-10.
No se ha ejecutado aún la recuperación incremental contra una tienda real.

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
`enqueue` asigna explícitamente la hora de recepción a `next_attempt`; al fallar,
se sustituye por la hora prevista del reintento. Entre trabajos disponibles se
ordena por `next_attempt` y después por `rowid`. Así los avisos posteriores no
adelantan permanentemente un reintento vencido. La deduplicación conserva su fecha.
Funciona también con bases cuyo esquema anterior tenía valor por defecto 0,
porque la inserción proporciona el valor explícitamente; no requiere migración.
Las bases nuevas usan la hora actual como valor por defecto. Los trabajos antiguos
con valor 0 permanecen disponibles y se procesan antes que los nuevos.
No hay máximo de intentos: un error permanente necesita intervención. `last_error`
almacena solo la clase del error, sin respuestas, cuerpos ni secretos. Una operación
GraphQL completa tiene un límite de 60 segundos y cada petición HTTP de 15.

La consulta está en `app/shopify/client.py`, `ORDER_QUERY`. Recorre todas las páginas de
`lineItems` con 100 elementos por página. Rechaza errores GraphQL incluso con datos
parciales, pedido nulo, versión efectiva distinta si se informa en la cabecera,
cursores repetidos, líneas duplicadas y cambios de `updatedAt` entre páginas.
Una respuesta inconsistente se vuelve a intentar desde el principio.

## Comando de escritura explícita

`change_order.py GID ESTADO [--dry-run]` usa `app/services/transitions.py` para consultar
Shopify y aplicar una transición del flujo `BORRADOR -> CONFIRMADO -> PAGADO ->
PREPARADO`. `NUEVO`/`ENVIADO` son aliases. Es un flujo propio, independiente de
`Order.confirmed`; combina tipo de objeto, estado financiero y preparación.
No observa ediciones de SQLite y no altera el funcionamiento de los webhooks.

`DraftOrder` se completa mediante una única `draftOrderComplete` con
`paymentPending: true`, sin consultar ni modificar `paymentTerms`. Se devuelve
y resuelve el nuevo GID `Order`. El argumento está disponible en la API 2026-10
(comprobado por introspección real), aunque obsoleto; es una dependencia explícita
de este flujo sencillo. Revisarlo al cambiar versión, sin eliminarlo y aceptar
accidentalmente el valor por defecto false. No requiere permisos payment_terms.
`orderMarkAsPaid`
solo se usa desde `PENDING` + `UNFULFILLED` con `canMarkAsPaid=true`; se excluyen
autorizaciones y parciales. `fulfillmentCreate` admite una única orden de
preparación visible, abierta, no solicitada y con acción soportada, y prepara
todas las unidades pendientes sin avisar al cliente. `FULFILLED` no equivale a
una entrega de transporte. No se admiten pedidos repartidos entre ubicaciones.

Consulta el estado remoto antes de cada comando y lo vuelve a comprobar después
de escribir. Repetir el estado actual no genera otra mutation, pero refresca el
pedido local. Saltos, retrocesos y estados fuera del alcance se rechazan. El modo
`--dry-run` no escribe ni crea SQLite. Las queries y mutations pasaron el validador
de la skill con su esquema 2026-04 y se contrastaron con documentación 2026-10.
No se han probado mutations contra la tienda real.

No existe atomicidad entre Shopify y SQLite ni entre leer el estado y mutarlo.
Un timeout puede ocurrir tras aplicar el cambio; no se reintenta automáticamente
por error de red. El operador consulta primero y vuelve a ejecutar para recuperar
la copia local. Se recomienda un solo operador por pedido durante la
prueba. Los permisos de escritura y las restricciones están en el README.

Correcciones del code review: el importe del borrador se valida antes de bifurcar
entre simulación y escritura. Después de una mutation, `sync_order_snapshot`
devuelve la respuesta completa y si se guardó; incluye `__typename`, `cancelledAt`
y `closed` para aplicar las mismas reglas sobre esa respuesta. No hay una lectura
de estado independiente seguida de otra para guardar. Si el resultado sale del
flujo, difiere del destino o la escritura fue omitida por una versión local nueva,
la transición no se considera un éxito. El snapshot se conserva en SQLite cuando
corresponde, aunque represente un reembolso fuera del flujo simple.
Los mensajes de errores remotos conservan el nombre de operación y campos de una
lista permitida, sin imprimir mensajes ni valores arbitrarios de `userErrors`.

## Estructura y puntos de entrada

`app/cli/` contiene la presentación por terminal; `app/services/`, la lógica de
importación, sincronización y cambios; `app/shopify/`, transporte y Bulk; y
`app/storage/`, inicialización y persistencia. `app/webhooks.py` contiene FastAPI.
Los scripts de la raíz conservan las interfaces públicas, incluido `main:app`.
Las rutas de proyecto están en `app/paths.py`, y las fechas en `app/dates.py`:
ni configuración ni repositorio necesitan importar un script de terminal o el
cliente HTTP para resolver rutas/fechas. `schema.sql`, `.env`, `data/` y `lotes/`
siguen en sus ubicaciones previas. Los tests actualizan sus imports a los módulos
reales y verifican las entradas públicas en subprocesos con copias temporales.

## Selección y ejecución de lotes

`batch_orders.py list ESTADO --output ARCHIVO [--limit N]` consulta `draftOrders`
para borradores o `orders` para el resto, con filtros de búsqueda y páginas de 100.
El límite por defecto es 20; 0 recorre todas las páginas. `classify_state`, compartido
con el comando individual, confirma el estado de cada nodo sin llamadas adicionales
por GID. Cursores inválidos o consultas fallidas impiden devolver la selección
parcial como si fuera completa.
El listado imprime JSON en stdout y resumen en stderr. Reemplaza el contenido
de la misma ruta con la selección actual de forma atómica, sin acumular GID.
Una lista vacía reemplaza la anterior por gids=[]; un error de consulta conserva
el archivo previo. La salida de ejecución de change sigue siendo exclusiva.

`batch_orders.py change DESTINO --input ARCHIVO --output OTRO [--dry-run]` valida
la tienda, versión, estado, GID y siguiente paso del archivo antes de procesar.
Reutiliza `transition_order`: cada elemento vuelve a consultarse en Shopify.
Se procesan secuencialmente con pausa configurable, sin mutation Bulk. No se
modifica la ruta individual, sus condiciones de pago ni su política de reintento.

La salida se crea de forma exclusiva antes de mutar. Se actualiza por reemplazo
atómico tras cada resultado. `results` contiene estado y correspondencia entre GID
de entrada y salida; `gids` solo los éxitos en el destino, para el siguiente paso.
Los errores por elemento no detienen el resto; un fallo de escritura del informe
sí interrumpe. `complete=false` conserva lo registrado tras una interrupción.
Una mutation puede haberse aplicado sin alcanzar a registrar su resultado; el
reintento con la selección original y otra salida revalida el estado e impide
repetir una transición ya alcanzada. No hay transacción distribuida ni rollback
del lote. Las simulaciones generan informe, sin escrituras remotas o SQLite, y
no se aceptan como entradas de éxitos para encadenar. `lotes/` se ignora en Git.

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
- El alcance incluye carga inicial accesible, alta, modificación y recuperación
  incremental manual de cambios sin webhook. No incluye borrado ni programación
  periódica automática. El acceso histórico depende de los permisos.
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
