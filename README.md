# natulim_shopify

Prueba técnica de Natulim: sincronización de pedidos de Shopify con Python,
FastAPI y SQLite. API prevista: GraphQL Admin 2026-10.

## Estado actual

Implementados carga inicial masiva con Bulk Operations, almacenamiento SQLite,
webhooks firmados de creación y actualización y consulta local por comando.
Los avisos se guardan en una cola persistente; un consumidor consulta el estado
actual del pedido mediante GraphQL y guarda pedido y líneas en una transacción.
La integración no crea ni modifica pedidos en Shopify.

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
`orders`, `order_line_items` y `webhook_jobs`. Repetirlo conserva los registros.
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

Verificado localmente en Windows con Python 3.13: 38 pruebas, compatibilidad de
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
