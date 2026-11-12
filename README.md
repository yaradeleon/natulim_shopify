# natulim_shopify

Prueba técnica de Natulim: sincronización de pedidos de Shopify con Python,
FastAPI y SQLite. API prevista: GraphQL Admin 2026-10.

## Estado actual

Implementados carga inicial e incremental con Bulk Operations, almacenamiento
SQLite, webhooks firmados de creación y actualización y consulta local por comando.
Los avisos se guardan en una cola persistente; un consumidor consulta el estado
actual del pedido mediante GraphQL y guarda pedido y líneas en una transacción.
Las cargas y los webhooks son de lectura en Shopify. El comando explícito
`change_order.py` añade escritura para un flujo de demostración de estados y
refresca después la copia local; editar SQLite directamente no dispara cambios.

Durante la prueba manual se recibió una actualización real con HTTP 200 y el
usuario confirmó que probó la actualización. También creó un pedido nuevo en
Shopify; queda registrar la comprobación de su copia local y del trabajo en
`done`. HTTP 200 confirma recepción en cola, no el guardado final.
La introspección de Postman describe el esquema `Order`; no se ha adjuntado una
respuesta completa de un pedido ficticio que valide todos los campos guardados.

## Requisitos

- Python 3.10 o superior y una copia del repositorio (Git es opcional si se descarga un ZIP).
- Acceso a una tienda de desarrollo y a una app instalada con permiso `read_orders`.
- Client ID y Client Secret compatibles con `client_credentials` (app y tienda
  de la misma organización); acceso a Bulk queries habilitado cuando corresponda.
- Para la prueba de webhooks: acceso a Configuración > Notificaciones > Webhooks
  y `cloudflared`. Su instalación se explica en la prueba de aceptación.

No hace falta instalar SQLite por separado ni un visor gráfico. Python incluye
`sqlite3`; `check_order.py` permite inspeccionar los resultados.

## Arranque desde una copia limpia

Desde la raíz de la copia del repositorio, con Python 3.10 o superior:

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
notepad .env
```

Completa los dos marcadores de credenciales de `.env` y comprueba el dominio.
No hace falta activar el entorno virtual ni cambiar la política de PowerShell.
Usa el Client ID y Client Secret de tu app instalada. El token Admin API se obtiene
automáticamente mediante `client_credentials`; no se copia ni se guarda en archivos.
No incluyas credenciales reales en `.env.example` ni en Git.

| Variable | Obligatoria | Valor por defecto / corrección |
| --- | --- | --- |
| `SHOPIFY_SHOP_DOMAIN` | Sí | Sin valor por defecto. Ejemplo: `natulim-sevica.myshopify.com`, sin `https://` ni rutas |
| `SHOPIFY_CLIENT_ID` | Sí | Sin valor por defecto. Sustituir `REEMPLAZAR_AQUI` por el Client ID de la app |
| `SHOPIFY_CLIENT_SECRET` | Sí | Sin valor por defecto. Sustituir `REEMPLAZAR_AQUI` por el Client Secret de la app |
| `SHOPIFY_WEBHOOK_SECRET` | No | Vacío usa `SHOPIFY_CLIENT_SECRET`; rellenar si la suscripción usa otro secreto de firma |
| `SHOPIFY_API_VERSION` | No | `2026-10`; formato trimestral `AAAA-MM` |
| `DATABASE_PATH` | No | `data/shopify.db` junto al proyecto; debe apuntar a un archivo |

`Settings.from_env()` llama a `load_dotenv()` con la ruta absoluta al `.env`
junto a `config.py`. Uvicorn ejecuta esa carga durante el arranque de FastAPI:
el comando siguiente **sí carga `.env`**, sin necesitar `--env-file`.
Las variables del entorno tienen prioridad sobre `.env`, incluso si están vacías.
Si hay varios errores locales, se muestran juntos con instrucciones para corregirlos,
sin imprimir los valores de los tokens o secretos. El arranque no valida las
credenciales contra Shopify; esa validación requiere una consulta real.

Si conservas un `.env` de la versión anterior, añade `SHOPIFY_CLIENT_ID` y
`SHOPIFY_CLIENT_SECRET`: `SHOPIFY_ACCESS_TOKEN` ya no se usa. Conserva el secreto
de webhook que ya estuviera configurado si corresponde a tu suscripción.

Después de guardar `.env`:

```powershell
.\.venv\Scripts\python init_db.py
.\.venv\Scripts\python sync_orders.py
.\.venv\Scripts\python -m uvicorn main:app --host 127.0.0.1 --port 8000 --workers 1
```

En otra terminal PowerShell:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
```

Debe devolver `status: ok`. Esto comprueba el servidor, no el acceso a Shopify.
Detén el servidor con `Ctrl+C`. Usa **un solo proceso/worker por base**: la cola
de este MVP tiene un consumidor.

En macOS/Linux, desde la raíz del repositorio, los comandos equivalentes son:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
if [ ! -e .env ]; then cp .env.example .env; fi
# Edita .env con tu editor y sustituye los marcadores antes de continuar.
.venv/bin/python init_db.py
.venv/bin/python sync_orders.py
.venv/bin/python -m uvicorn main:app --host 127.0.0.1 --port 8000 --workers 1
```

Desde otra terminal: `curl http://127.0.0.1:8000/health`, que devuelve
`{"status":"ok"}`. Los comandos macOS/Linux están documentados, pero no se han
ejecutado en esos sistemas.

### Inicialización SQLite

`python init_db.py` también funciona sin instalar dependencias y sin credenciales:
solo usa la biblioteca estándar. Crea `data/shopify.db` junto al proyecto, con
`orders`, `order_line_items`, `webhook_jobs` y `sync_state`. Repetirlo conserva los registros.
El esquema está en `schema.sql`. La inicialización es transaccional y usa
`CREATE TABLE IF NOT EXISTS`; no realiza migraciones de esquemas anteriores.

`init_db.py` no carga `.env`. Si cambias `DATABASE_PATH`, crea esa misma base con
`python init_db.py --db RUTA`. Las rutas relativas de `--db` se resuelven desde el
directorio de trabajo; las de `DATABASE_PATH`, desde la raíz del proyecto.
El arranque de FastAPI también inicializa la base indicada en `DATABASE_PATH`.

## Carga inicial de pedidos existentes

Con las credenciales completadas, ejecuta desde la raíz del proyecto:

```powershell
.\.venv\Scripts\python sync_orders.py
```

Este comando es el que **trae a SQLite los pedidos que ya estaban en Shopify**.
No necesita FastAPI en ejecución y crea la base configurada si falta. Inicia
`bulkOperationRunQuery` para exportar pedidos accesibles, incluidos los
cerrados/cancelados, con todas sus líneas. Shopify gestiona la paginación.
El comando consulta el estado cada cinco segundos (máximo una hora), descarga
el JSONL y valida su número de objetos antes de escribir en SQLite. Usa una base
temporal en disco y comparte `save_order` con los webhooks. La operación GraphQL
`mutation` solo inicia la exportación de lectura; no modifica pedidos en Shopify.

Al terminar muestra pedidos guardados, omitidos porque la copia local es más
reciente, pedidos fallidos y fallos de listado. Código de salida 0 significa carga
completa sin errores; 1, configuración inválida o carga parcial/fallida. En
PowerShell puedes consultar `$LASTEXITCODE` inmediatamente después del comando.
Si falla la exportación, la descarga o la validación completa del archivo,
no guarda ningún pedido de esa exportación. Si falla una escritura individual,
conserva su copia local, conserva los pedidos ya confirmados y continúa. Repite el comando
para recuperar fallos: no duplica pedidos ni líneas. Un HTTP 429 o error de cuota
se informa como fallo; en la carga inicial no hay reintento automático.

Durante la espera muestra el estado y el número de objetos exportados (pedidos
y líneas); después informa del guardado cada 100 pedidos. No requiere túnel ni
`bulk_operations/finish`. Detén con `Ctrl+C` cualquier carga anterior antes de
ejecutar el comando actualizado. Los datos ya guardados no se duplican.
Interrumpir el comando no cancela la exportación remota; repetirlo inicia otra.
No se utilizan resultados parciales de operaciones fallidas. Según el tipo de
app, puede ser necesario habilitar **Allow bulk queries** en el Partner Dashboard.
La velocidad real en la tienda todavía no se ha medido.

El token se conserva únicamente en memoria por proceso y se renueva antes del
vencimiento indicado por `expires_in`. Un HTTP 401 provoca una renovación y un
único reintento. `.env.example` permanece como plantilla estática.

El flujo `client_credentials` requiere una app instalada y app/tienda dentro de
la misma organización Shopify. Necesitas `read_orders`; por defecto el acceso
a pedidos está limitado a los últimos 60 días. Los anteriores requieren acceso
aprobado `read_all_orders`. «Todos» significa todos los accesibles con los permisos
concedidos. Consulta la [guía oficial de autenticación](https://shopify.dev/docs/apps/build/authentication-authorization/client-credentials-grant).

## Recuperar cambios sin webhook: carga incremental

Después de una carga completa correcta con esta versión, ejecuta:

Windows:

```powershell
.\.venv\Scripts\python sync_orders.py --incremental
```

macOS/Linux:

```bash
.venv/bin/python sync_orders.py --incremental
```

Usa la misma exportación Bulk de la carga inicial, filtrando `orders` por
`updated_at` y ordenando por `UPDATED_AT`. Trae los pedidos modificados en ese
intervalo con todas sus líneas, incluidos pedidos nuevos accesibles. No lista
todos los GID para consultarlos individualmente. No añade pandas ni dependencias.
Shopify prepara el archivo de forma asíncrona; el tiempo real depende del volumen
de cambios y de Shopify, y no se ha medido contra esta tienda.

La tabla `sync_state` guarda `last_successful_sync_at` por dominio de tienda.
Es la hora de **inicio** de la última carga correcta, en UTC, no la hora de su fin
ni el mayor `Order.updatedAt` almacenado. Así, cambios durante la ejecución quedan
para la siguiente. La consulta incremental incluye desde cinco minutos antes de
ese punto hasta el inicio de la ejecución actual, ambos límites inclusivos.
Repetir los pedidos del solapamiento no duplica filas ni sustituye una versión
local más reciente. Los webhooks no avanzan este punto de control.

Solo se avanza cuando toda la exportación está validada y todos sus pedidos se
han guardado u omitido por existir una versión más reciente. Una exportación vacía
correcta también lo avanza. Si falla una consulta, descarga, escritura o guardado
del punto de control, el comando sale con código 1 y conserva el punto anterior;
los pedidos ya confirmados permanecen. Repite el mismo comando para recuperarlos.
`Ctrl+C` tampoco avanza el punto. Si otra carga lo modificó mientras esta corría,
se conserva su valor para evitar sobrescribirlo con un resultado concurrente;
para esta versión se recomienda ejecutar un solo comando de carga cada vez.

**Bases creadas con la versión anterior:** inicializar tablas no establece una
fecha de control. Si falta, `--incremental` avisa y termina sin llamar a Shopify
ni lanzar automáticamente una carga completa. Ejecuta una vez `sync_orders.py`
sin opciones y espera a que termine correctamente para establecerla. Conserva
los datos existentes. No se deduce la fecha del pedido más reciente: podrían
faltar modificaciones de otros pedidos anteriores a esa fecha.

Es un comando manual: no ejecuta un temporizador ni requiere FastAPI o túnel.
Su ejecución periódica puede programarse más adelante. El solapamiento reduce
el riesgo por límites e indexación tardía, pero no garantiza recuperar retrasos
superiores a cinco minutos. Mantén el reloj del equipo sincronizado. No recupera
pedidos eliminados ni inaccesibles por permisos; si cambia el alcance de acceso
o se necesita reconciliar todo el histórico, repite una carga completa.
Cada base debe seguir dedicada a una única tienda.

### Prueba de recuperación para el revisor

1. Completa una carga con esta versión para establecer la fecha de control.
2. Detén FastAPI con `Ctrl+C` para que no procese la siguiente modificación.
3. En Shopify cambia la nota de un pedido ficticio a `Recuperado por incremental`
   y guarda. Conserva su número.
4. Con FastAPI todavía detenido, ejecuta `sync_orders.py --incremental`. Si
   Shopify aún no refleja el cambio en su búsqueda, espera y repite el comando.
5. Ejecuta `check_order.py NUMERO` con el Python del entorno. Deben verse la nota
   nueva y `updated_at` actualizado, sin necesitar un webhook `done` para ese
   cambio. La recuperación por Bulk no crea trabajos en `webhook_jobs`.
6. Reinicia FastAPI. Shopify puede reintentar las entregas pendientes; el mismo
   pedido puede procesarse de nuevo sin duplicarse.

La implementación del filtro sigue la [consulta oficial de pedidos](https://shopify.dev/docs/api/admin-graphql/2026-10/queries/orders)
y el flujo de [Bulk Operations](https://shopify.dev/docs/apps/build/apis/graphql-admin/bulk-operations/queries).
La prueba de recuperación contra la tienda real queda pendiente; las pruebas
automatizadas descritas abajo simulan Shopify y sus fallos.

## Cambiar el estado desde terminal

`change_order.py` consulta Shopify, valida el avance, ejecuta la operación remota
y refresca SQLite. Es una entrada explícita por GID y estado; no observa cambios
directos en las tablas. No necesita FastAPI ni Cloudflare para funcionar.

El flujo de demostración es `BORRADOR -> CONFIRMADO -> PAGADO -> PREPARADO`.
`NUEVO` es alias de `BORRADOR` y `ENVIADO` de `PREPARADO`. Estos nombres son una
regla de esta aplicación, no un único campo de estado de Shopify:

| Estado del flujo | Representación / operación en Shopify |
| --- | --- |
| `BORRADOR` | `DraftOrder.status` en `OPEN` o `INVOICE_SENT`; el comando no crea borradores. |
| `CONFIRMADO` | `Order` con `displayFinancialStatus=PENDING` y `displayFulfillmentStatus=UNFULFILLED`, creado mediante `draftOrderComplete`. No se interpreta el booleano `Order.confirmed` como este estado. |
| `PAGADO` | `displayFinancialStatus=PAID` y `UNFULFILLED`, mediante `orderMarkAsPaid`. |
| `PREPARADO` / `ENVIADO` | `PAID` y `FULFILLED`, mediante `fulfillmentCreate`; no acredita recogida del transportista ni entrega física. |

### Permisos y preparación de la prueba

Además de las credenciales de `.env`, autoriza los permisos de la app necesarios
para el flujo: `read_orders`, `write_orders`, `read_draft_orders`,
`write_draft_orders`,
`read_merchant_managed_fulfillment_orders` y `write_merchant_managed_fulfillment_orders`.
La cuenta debe poder completar borradores, marcar como pagado y preparar
pedidos. Si cambias los permisos, vuelve a autorizar la instalación según indique
Shopify; cada ejecución del comando obtiene un token nuevo.
El comando no crea ni modifica condiciones de pago y no requiere
`read_payment_terms` ni `write_payment_terms`.

Usa un borrador ficticio con importe positivo y un producto físico cuya preparación
gestione la tienda en una sola ubicación. `PAGADO` registra un pago real en el
sistema: no es cambiar una etiqueta local. El flujo rechaza autorizaciones,
pagos/reembolsos parciales, cancelados, retenidos y preparaciones parciales.

### Ejemplo completo

1. En Shopify crea un borrador ficticio (Pedidos > Crear pedido) y déjalo como
   borrador. Obtén su ID numérico de la URL `/draft_orders/ID` y construye
   `gid://shopify/DraftOrder/ID`. Para un pedido ya importado, `check_order.py`
   muestra su `shopify_order_id`. El GID no es el número visible `#6501`.
2. Opcionalmente comprueba la transición sin modificar Shopify ni SQLite:

   ```powershell
   .\.venv\Scripts\python change_order.py "gid://shopify/DraftOrder/123" CONFIRMADO --dry-run
   ```

3. Confirma el borrador:

   ```powershell
   .\.venv\Scripts\python change_order.py "gid://shopify/DraftOrder/123" CONFIRMADO
   ```

   Sustituye `123` por el ID real. Ejecuta únicamente
   `draftOrderComplete(id: ..., paymentPending: true)`: confirma el borrador
   dejando el pago pendiente, sin añadir ni modificar condiciones de pago.
   **Devuelve otro GID**, de tipo `Order`; úsalo a
   partir de este punto. No se guardan borradores como si fueran pedidos locales.
4. Con el GID del pedido que devolvió, por ejemplo `gid://shopify/Order/456`:

   ```powershell
   .\.venv\Scripts\python change_order.py "gid://shopify/Order/456" PAGADO
   .\.venv\Scripts\python change_order.py "gid://shopify/Order/456" PREPARADO
   .\.venv\Scripts\python check_order.py "gid://shopify/Order/456"
   ```

   Se requiere `canMarkAsPaid=true` para pagar. Preparar requiere exactamente una
   `FulfillmentOrder` visible, `OPEN`, `UNSUBMITTED` y con acción
   `CREATE_FULFILLMENT`. Se preparan todas sus unidades pendientes, sin truncar
   líneas, con `notifyCustomer=false`. Se rechazan más órdenes de preparación
   o páginas adicionales antes de escribir; no se gestionan servicios externos.

En macOS/Linux sustituye `.\.venv\Scripts\python` por `.venv/bin/python` en los
mismos comandos. Los identificadores del ejemplo son ficticios.

No se permiten saltos ni retrocesos. Pedir `BORRADOR`/`NUEVO` para un pedido ya
confirmado devuelve un mensaje de que ha avanzado y código de salida 1.
Si Shopify no devuelve el pedido o borrador, muestra: «Pedido o borrador no
disponible; comprueba el GID y los permisos de lectura», y termina con código 1
sin ejecutar mutations ni guardar el pedido en SQLite. No se puede distinguir
solo con una respuesta nula si el recurso no existe o no es accesible.
Un ID con formato incorrecto (por ejemplo `#6501` o un número sin GID) se rechaza
antes de consultar Shopify y muestra un ejemplo del formato completo requerido.
En modo normal el comando puede inicializar las tablas locales antes de detectar
un pedido inexistente, pero no inserta ni actualiza ese pedido; `--dry-run` no
inicializa la base ni escribe en ella.
Repetir el estado actual devuelve éxito sin repetir mutations y refresca SQLite
si es un `Order`. Se puede reutilizar el GID de un borrador completado: el comando
resuelve su nuevo pedido antes de validar el paso. `--dry-run` es una consulta
previa, no reserva el estado ni garantiza que la operación posterior sea aceptada.

`paymentPending` sigue disponible y se verificó por introspección en la API
2026-10 de la tienda, pero Shopify lo marca como obsoleto y recomienda usar
condiciones de pago. Se mantiene para este flujo sencillo solicitado, sin
gestionar condiciones. Revisar su disponibilidad antes de cambiar de versión;
no quitarlo sin más: su valor por defecto es `false` y podría marcar el pedido
como pagado al completarlo. No se ejecutan escrituras de prueba automáticamente.

Shopify y SQLite no comparten transacción. Si hay interrupción,
error de red o fallo local posterior, el cambio remoto puede estar aplicado: el
comando lo advierte y no promete rollback. Consulta primero con `--dry-run` y
repite el estado solicitado para refrescar SQLite sin volver a ejecutarlo cuando
Shopify ya lo muestre. No ejecutes transiciones concurrentes sobre el mismo pedido;
la validación y la mutation no forman una operación atómica entre ambas lecturas.

La [confirmación de borradores](https://shopify.dev/docs/api/admin-graphql/2026-10/mutations/draftOrderComplete),
el [registro de pago](https://shopify.dev/docs/api/admin-graphql/2026-10/mutations/orderMarkAsPaid)
y la [creación de fulfillment](https://shopify.dev/docs/api/admin-graphql/2026-10/mutations/fulfillmentCreate)
se contrastaron con el plugin y la documentación oficial. La prueba de escritura
contra la tienda real sigue pendiente; durante el desarrollo solo se simularon
las respuestas y no se modificó ningún pedido remoto.

## Filtrar y actualizar estados por lotes

El comando individual `change_order.py` se mantiene. `batch_orders.py` añade dos
pasos separados: listar candidatos por estado desde Shopify y procesar los GID
seleccionados. Comparte `classify_state` y reutiliza `transition_order` para cada
elemento, con las mismas mutations, validaciones y refresco de SQLite.

### Listar candidatos y revisar sus GID

Windows, desde la raíz del proyecto:

```powershell
.\.venv\Scripts\python batch_orders.py list BORRADOR --limit 5 --output lotes/borradores.json
```

Imprime el JSON completo por terminal y lo guarda en el archivo indicado, con
los GID en la propiedad `gids`, tienda y estado. Los mensajes de resumen van a
stderr para poder copiar el JSON de stdout. Cada vez que repites el listado con
la misma ruta, reemplaza todo su contenido con la selección actual: no acumula
GID ni exige otro nombre. Sin candidatos, `gids` queda vacío; si falla la consulta,
conserva el archivo anterior. Abre el archivo con tu editor para revisarlo; puedes quitar
GID de esa lista antes de ejecutarlos, conservando el resto de metadatos.
Listar no modifica Shopify ni crea SQLite. También acepta los estados
`CONFIRMADO`, `PAGADO`, `PREPARADO` y los aliases del comando individual.

El filtro remoto de borradores incluye `OPEN` e `INVOICE_SENT`; para pedidos
combina pago y preparación. Se comprueban además los campos devueltos con las
mismas reglas locales para excluir cancelados o estados fuera del flujo.
Consulta páginas de 100 candidatos sin una petición individual de estado por
cada GID. El límite por defecto es 20 y se informa si puede haber más resultados;
`--limit 0` recorre todas las páginas accesibles. Una selección no garantiza que
todos sean procesables: el estado y los requisitos se vuelven a consultar al
ejecutar, y un borrador con importe cero, por ejemplo, se rechazará entonces.

### Ejecutar y encadenar los pasos

Opcionalmente simula la confirmación de todos los seleccionados:

```powershell
.\.venv\Scripts\python batch_orders.py change CONFIRMADO --input lotes/borradores.json --output lotes/simulacion.json --dry-run
```

Después, para ejecutar el flujo real:

```powershell
.\.venv\Scripts\python batch_orders.py change CONFIRMADO --input lotes/borradores.json --output lotes/confirmados.json
.\.venv\Scripts\python batch_orders.py change PAGADO --input lotes/confirmados.json --output lotes/pagados.json
.\.venv\Scripts\python batch_orders.py change PREPARADO --input lotes/pagados.json --output lotes/preparados.json
```

Cada comando es independiente; revisa su resultado antes del siguiente. Al
confirmar, `confirmados.json` contiene los GID nuevos de tipo `Order` para el pago.
La propiedad `results` conserva la correspondencia `input_gid` -> `gid`, el estado
y el resultado de cada intento. `gids` contiene solo los éxitos verificados en
el estado de destino, incluidos los que ya estaban en él. Los fallidos quedan
registrados para revisión y no pasan automáticamente al siguiente paso.

También puedes empezar por los pedidos que ya están confirmados o pagados:

Estos filtros son independientes y pueden seleccionar otros pedidos de la tienda.
Para avanzar siempre los mismos, encadena los archivos de resultados del ejemplo
anterior, sin volver a filtrar entre pasos.

```powershell
.\.venv\Scripts\python batch_orders.py list CONFIRMADO --limit 5 --output lotes/pendientes_pago.json
.\.venv\Scripts\python batch_orders.py change PAGADO --input lotes/pendientes_pago.json --output lotes/pago_resultados.json
.\.venv\Scripts\python batch_orders.py list PAGADO --limit 5 --output lotes/pendientes_preparacion.json
.\.venv\Scripts\python batch_orders.py change PREPARADO --input lotes/pendientes_preparacion.json --output lotes/preparacion_resultados.json
```

En macOS/Linux sustituye `.\.venv\Scripts\python` por `.venv/bin/python`. No
necesita FastAPI ni túnel. Los permisos y los efectos de pago/preparación son los
mismos que en el comando individual; este lote tampoco añade condiciones de pago.

### Resultados, interrupciones y límites

- Procesa los elementos secuencialmente, con una pausa de 0,5 segundos entre GID,
  configurable mediante `--delay`. Es una coordinación de operaciones individuales,
  no una exportación Bulk ni una única mutation masiva; no promete una duración.
- Si un pedido falla, registra el error y continúa con el siguiente. Estado,
  canMarkAsPaid y disponibilidad del fulfillment se comprueban en el momento de
  cada transición; una selección antigua no autoriza saltos ni retrocesos.
- Guarda resultados tras cada elemento y reemplaza el JSON de forma atómica.
  `complete=false` identifica una ejecución interrumpida; los GID que ya figuran
  como éxito siguen disponibles. El último cambio remoto puede estar aplicado
  aunque su resultado aún no se haya registrado. No existe rollback de todo el lote.
- Para reintentar, usa el archivo de entrada original y **otro nombre de salida**.
  Los pedidos que ya están en el estado solicitado no repiten mutations. Si alguno
  avanzó más, se rechaza el retroceso y se registra el error.
- Los listados actualizan el mismo archivo con contenido nuevo de forma atómica.
  Los resultados de `change` requieren otro nombre y no se sobrescriben.
  Rechaza otra tienda, GID inválidos,
  combinaciones de estado/tipo de GID incorrectas y saltos de pasos. Deduplica GID.
- `--dry-run` no modifica pedidos ni SQLite, pero sí escribe su informe. Sus
  resultados no se aceptan como entrada para el siguiente paso real.
- Salida 0: todos los intentos correctos (o una selección vacía); salida 1:
  algún fallo o error de configuración/archivo; `Ctrl+C`: 130. Una pausa fija no
  garantiza evitar límites de Shopify: errores de cuota se registran sin reintentar
  automáticamente las mutations. Empieza con lotes pequeños y revisa los fallos.

Los archivos de `lotes/` contienen identificadores operativos y se excluyen de
Git. No se han ejecutado escrituras de lotes contra la tienda real. La selección
se contrastó con las consultas oficiales de [borradores](https://shopify.dev/docs/api/admin-graphql/2026-10/queries/draftOrders)
y [pedidos](https://shopify.dev/docs/api/admin-graphql/2026-10/queries/orders).

## Modelo y correspondencia con Shopify

| Tabla.columna | Campo de Shopify |
| --- | --- |
| `orders.shopify_order_id` | `Order.id` (GID completo, clave primaria) |
| `orders.name` | `Order.name` |
| `orders.created_at` | `Order.createdAt` |
| `orders.updated_at` | `Order.updatedAt` |
| `orders.note` | `Order.note` (admite NULL) |
| `orders.financial_status` | `Order.displayFinancialStatus` (admite NULL) |
| `orders.fulfillment_status` | `Order.displayFulfillmentStatus` |
| `orders.current_total_amount` | `Order.currentTotalPriceSet.shopMoney.amount` |
| `orders.currency_code` | `Order.currentTotalPriceSet.shopMoney.currencyCode` |
| `order_line_items.shopify_line_item_id` | `LineItem.id` (GID completo, clave primaria) |
| `order_line_items.order_id` | `Order.id` (clave foránea a `orders`) |
| `order_line_items.title` | `LineItem.title` |
| `order_line_items.quantity` | `LineItem.quantity` |
| `order_line_items.current_quantity` | `LineItem.currentQuantity` |

Los importes son texto decimal para conservar precisión; para calcular se usará
`decimal.Decimal`. Las fechas son texto ISO 8601 que la integración
normaliza a UTC. Los estados conservan los nombres de los valores enum.
SQLite no valida por sí solo estos formatos.

Cada pedido admite varias líneas. El índice sobre `order_id` facilita su consulta.
Cada conexión que escriba datos debe ejecutar `PRAGMA foreign_keys = ON`;
es una opción por conexión. No se borran líneas en cascada.
Cliente, catálogo e inventario quedan como ampliaciones opcionales.

## Validación local

Para inspeccionar un pedido sin escribir SQL ni instalar un visor, ejecuta:

```powershell
.\.venv\Scripts\python check_order.py 6501
```

En macOS/Linux: `.venv/bin/python check_order.py 6501`.
Sustituye `6501` por el número real. También acepta `"#6501"` o el GID completo.
Muestra los campos locales del pedido, todas sus líneas y sus últimos cinco
webhooks con estado e intentos fallidos. Lee `DATABASE_PATH` de `.env` y abre
SQLite en modo de solo lectura. No necesita credenciales Shopify ni conexión
a la tienda. Después de editar el pedido, repite el comando para comparar
`note`, `updated_at` y el GID; el trabajo correspondiente debe estar `done`.

```powershell
.\.venv\Scripts\python -m unittest discover -s tests -v
```

Las pruebas usan bases temporales y datos ficticios. Comprueban creación vacía,
conservación de datos tras reinicializar, firma, duplicados, actualización,
eventos antiguos, paginación, reintentos y rollback. No validan todavía una
sincronización con la tienda real. En macOS/Linux usa `.venv/bin/python`.

Las bases locales y los archivos `.env` se excluyen de Git. No guardar tokens ni
credenciales en el repositorio.

Verificado localmente en Windows con Python 3.13: 88 pruebas, compatibilidad de
dependencias con `pip check`, creación SQLite y arranque real de Uvicorn en una
copia temporal de los archivos del proyecto. La prueba de arranque usa `.env`
con credenciales ficticias y una ruta de base distinta para demostrar su carga;
comprueba `/health` en un puerto libre y no llama a Shopify. Reutiliza las
dependencias instaladas en el entorno de pruebas, no crea otro entorno virtual.
La prueba manual contra la tienda se distingue de estas pruebas simuladas en
Estado actual. La importación total y su duración no cuentan con un resumen
de resultados registrado. macOS/Linux no se han probado en esta sesión.
Las pruebas simulan la obtención/renovación del token, carga repetida, paginación
de pedidos y líneas, preservación ante errores y continuidad tras un fallo de escritura.
La cobertura incremental incluye filtro temporal y solapamiento, fechas UTC,
recuperación sin webhook con 301 líneas, carga vacía, ausencia de punto de control,
fallos y cancelación sin avanzar la fecha, repetición sin duplicados, preservación
de versiones nuevas y control de actualizaciones concurrentes del marcador.
Las pruebas de escritura simulan el flujo completo, cambio de GID, permisos
rechazados, saltos/retrocesos, repetición sin nuevas mutations, simulación sin
escrituras, errores de red ambiguos y recuperación tras fallo de SQLite.
Los lotes añaden pruebas de selección paginada, filtros compartidos, cambio de GID
y encadenado de las tres transiciones, fallos parciales, revalidación remota,
simulación sin escrituras, interrupción con salida parcial y validación de archivos.

## Prueba de aceptación con Shopify (Windows, macOS y Linux)

También se aceptan las rutas ya configuradas en la tienda:
`/shopify/webhook/orders/create` para `orders/create` y
`/shopify/webhook/orders/updated` para `orders/updated`.
Comparten firma, cola y sincronización con `/webhooks/orders`; en las rutas
específicas el tema de la cabecera debe coincidir con la ruta. Conserva esos
sufijos si ya los usas. El dominio debe coincidir con el túnel actualmente activo:
adaptar las rutas locales no permite recuperar un dominio temporal antiguo.

1. Sigue Arranque desde una copia limpia para tu sistema: crea el entorno,
   instala dependencias y completa `.env`. No sobrescribas un `.env` existente.
2. Ejecuta `init_db.py` y `sync_orders.py` con el Python del entorno. Espera al
   resumen de la carga y comprueba que no hay fallos. La carga no necesita túnel.
   Si cambiaste `DATABASE_PATH`, usa `init_db.py --db RUTA`; la carga también
   inicializa automáticamente la base configurada.
3. Arranca Uvicorn con el comando de tu sistema y un solo worker. Conserva esa
   terminal abierta. Abre `http://127.0.0.1:8000/health` en el navegador: debe
   mostrar `{"status":"ok"}`.
4. Instala `cloudflared` si aún no lo tienes. En Windows con Winget:

   ```powershell
   winget install --id Cloudflare.cloudflared --exact
   ```

   En macOS con Homebrew instalado:

   ```bash
   brew install cloudflared
   ```

   En Linux, o sin esos gestores, instala el paquete/binario de tu sistema y
   arquitectura siguiendo las [descargas oficiales de Cloudflare](https://developers.cloudflare.com/tunnel/downloads/).
   Abre una terminal nueva y comprueba `cloudflared --version`.
5. En otra terminal abre el túnel, en cualquiera de los sistemas:

   ```text
   cloudflared tunnel --url http://127.0.0.1:8000
   ```

   Si Windows no encuentra el ejecutable instalado con Winget, prueba:

   ```powershell
   & "C:\Program Files (x86)\cloudflared\cloudflared.exe" tunnel --url http://127.0.0.1:8000
   ```

   Copia la URL `https://….trycloudflare.com` del recuadro y deja la terminal
   abierta. Comprueba en el navegador `https://TU-TUNEL/health`. Los Quick Tunnels
   no requieren cuenta, pero su dominio cambia al crear otro túnel.
6. En Shopify > Configuración > Notificaciones > Webhooks, crea o edita dos
   avisos con formato JSON y versión 2026-10:

   | Evento | URL |
   | --- | --- |
   | Creación de pedido (`orders/create`) | `https://TU-TUNEL/shopify/webhook/orders/create` |
   | Actualización de pedido (`orders/updated`) | `https://TU-TUNEL/shopify/webhook/orders/updated` |

   Sustituye `https://TU-TUNEL` por la dirección pública completa del túnel activo.
   Si ya existen, conserva las rutas y actualiza únicamente el dominio cuando
   cambie. Copia el Secreto actual del panel a `SHOPIFY_WEBHOOK_SECRET` en `.env`.
   No es necesariamente el Client Secret de la app. Reinicia FastAPI después de
   guardar `.env`, manteniendo Cloudflare abierto.
7. Crea un pedido ficticio y completa su creación: un borrador no prueba
   `orders/create`. Anota su número. FastAPI debe registrar un POST con HTTP 200.
   Espera unos segundos y consulta el pedido desde otra terminal:

   Windows:

   ```powershell
   .\.venv\Scripts\python check_order.py 6501
   ```

   macOS/Linux:

   ```bash
   .venv/bin/python check_order.py 6501
   ```

   Sustituye `6501` por su número. Deben aparecer sus datos, sus líneas y el
   trabajo `orders/create` en `done`. Si sigue pendiente, repite la consulta.
8. En ese mismo pedido, pulsa el símbolo + o editar junto a Notas, escribe
   `Prueba webhook Natulim` y guarda. Repite `check_order.py`. Comprueba que
   `note` cambió, `updated_at` avanzó, el GID es el mismo y `orders/updated` está
   en `done`. No vuelvas a ejecutar la carga entre estos pasos: así se demuestra
   que la actualización procede del webhook. Guarda ambas salidas como evidencia.
9. Opcionalmente, modifica cantidades y revisa `quantity`, `currentQuantity` y
   el total. Una línea retirada puede permanecer con cantidad actual cero.
   Para comprobar recuperación, detén FastAPI después de encolar un aviso y
   arráncalo de nuevo; los trabajos pendientes se recuperan desde SQLite.

### Diagnóstico de la prueba

| Resultado | Interpretación / comprobación |
| --- | --- |
| GET de una ruta webhook devuelve 405 | Esperado al abrirla en navegador: solo acepta POST. Usa `/health` para comprobar acceso. |
| POST devuelve 401 | Firma incorrecta: revisa el secreto de estas suscripciones y reinicia FastAPI tras guardar `.env`. |
| POST devuelve 403 | El dominio de tienda de la cabecera no coincide con `SHOPIFY_SHOP_DOMAIN`. |
| POST devuelve 400 | Revisa tema, ruta, JSON e identificadores requeridos. |
| POST devuelve 200 | Aviso persistido en cola; falta comprobar el pedido y trabajo `done`. |
| `pending` con intentos fallidos | Revisa `last_error` en `check_order.py` y la terminal FastAPI; el consumidor reintenta. |
| Pedido no encontrado | Comprueba número, `DATABASE_PATH`, recepción del aviso y que se completó la creación. |
| Dominio del túnel no resuelve | Crea otro túnel y actualiza el dominio de ambos destinos; no se recupera cambiando rutas locales. |

No se necesita DB Browser ni escribir SQL para este recorrido.

Los cambios de pedidos hechos desde el panel o desde otras apps se procesan por
el mismo flujo cuando generan la notificación suscrita. La sincronización termina
en SQLite: sus operaciones GraphQL son solo consultas de lectura. Arrancar FastAPI
por sí solo no ejecuta la carga inicial. Abre la base indicada por `DATABASE_PATH`
con SQLite/DB Browser; si omites esa variable, es `data/shopify.db`.

Las cabeceras requeridas son `X-Shopify-Hmac-Sha256`, `X-Shopify-Shop-Domain`,
`X-Shopify-Topic` y `X-Shopify-Webhook-Id`. Se verifica el HMAC del cuerpo original
antes de procesar el JSON. En Postman no basta con pegar un JSON: también hay
que generar la firma de ese cuerpo con el secreto local.

## Documentación

- [Diseño técnico y límites](docs/technical.md).
- [Resumen ejecutivo](docs/executive.md).
