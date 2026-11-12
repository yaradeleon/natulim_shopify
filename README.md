# Sincronización de pedidos de Shopify

Prueba técnica de Natulim con Python, FastAPI y SQLite, usando GraphQL Admin API
`2026-10`. Copia pedidos y sus líneas, recibe cambios por webhooks y permite
avanzar estados desde la terminal, individualmente o por lotes.

## 1. Instalar las herramientas

Descarga el repositorio y abre una terminal en su carpeta. Puedes usar un ZIP;
Git no es obligatorio. Necesitas Python 3.10 o superior, pip y `venv`.
Probado en Windows con Python 3.13. Los pasos de macOS y Linux son equivalentes,
pero no se han ejecutado en esos sistemas.

| Sistema | Python | Cloudflare, para recibir webhooks en local |
| --- | --- | --- |
| Windows | Instala desde [Python.org](https://www.python.org/downloads/) y comprueba `python --version`. | En PowerShell: `winget install --id Cloudflare.cloudflared --exact`. |
| macOS | Instala desde [Python.org](https://www.python.org/downloads/) y comprueba `python3 --version`. | Con Homebrew instalado: `brew install cloudflared`; sin Homebrew, usa la descarga oficial. |
| Linux | Usa el gestor de tu distribución. Ubuntu/Debian: `sudo apt install python3 python3-pip python3-venv`. Comprueba que la versión sea 3.10 o superior. | Instala el paquete de tu distribución y arquitectura desde la descarga oficial. |

Consulta las [descargas oficiales de Cloudflare](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/downloads/).
Abre una terminal nueva tras instalar y comprueba `cloudflared --version`.
No necesitas instalar SQLite, pandas ni un visor de base de datos.

**Windows, PowerShell:**

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\Activate.ps1
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
notepad .env
```

Si PowerShell bloquea la activación, no es necesaria: sustituye `python` por
`.\.venv\Scripts\python` en los comandos restantes.

**macOS/Linux:**

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
source .venv/bin/activate
if [ ! -e .env ]; then cp .env.example .env; fi
```

Edita `.env` con tu editor. En lo que sigue, `python` significa el del entorno
virtual. En cada terminal nueva, vuelve a activarlo desde la carpeta del proyecto
o usa su ruta completa (`.venv/bin/python` en macOS/Linux).

## 2. Configurar la aplicación de Shopify

Necesitas una app instalada en tu tienda de desarrollo, con credenciales
compatibles con `client_credentials`: app y tienda deben pertenecer a la misma
organización. Completa `.env` en la raíz:

```dotenv
SHOPIFY_SHOP_DOMAIN=tu-tienda.myshopify.com
SHOPIFY_CLIENT_ID=tu_client_id
SHOPIFY_CLIENT_SECRET=tu_client_secret
SHOPIFY_WEBHOOK_SECRET=secreto_de_firma_de_tus_webhooks
SHOPIFY_API_VERSION=2026-10
DATABASE_PATH=data/shopify.db
```

El dominio no lleva `https://` ni rutas. El Client ID y Client Secret son los de
la app; el token se obtiene y renueva automáticamente en memoria. Para webhooks
creados en el panel de Shopify, usa su secreto de firma, que puede ser distinto
del Client Secret. Si dejas `SHOPIFY_WEBHOOK_SECRET` vacío, el código usa el Client
Secret. `.env` guarda credenciales; `.venv` contiene el entorno Python: son cosas
distintas y ambas quedan fuera de Git. Las variables del sistema prevalecen sobre
`.env`; reinicia FastAPI después de cambiar la configuración.

Permisos de la app:

- Carga y consulta: `read_orders`. Para pedidos de más de 60 días se necesita
  además el acceso histórico correspondiente (`read_all_orders`).
- Confirmar borradores y registrar pagos: `read_draft_orders`,
  `write_draft_orders` y `write_orders`, además de `read_orders`.
- Preparar pedidos gestionados por la tienda:
  `read_merchant_managed_fulfillment_orders` y
  `write_merchant_managed_fulfillment_orders`.

Si cambias permisos, vuelve a autorizar la instalación según indique Shopify.
No se necesitan permisos de condiciones de pago para el flujo implementado.

## 3. Crear la base y cargar los pedidos existentes

```bash
python init_db.py
python sync_orders.py
```

El primer comando crea `data/shopify.db` desde `schema.sql`. Repetirlo conserva
los datos. Si eliges otra ruta en `.env`, inicialízala con
`python init_db.py --db RUTA`; la carga también inicializa la base configurada.

La carga usa `bulkOperationRunQuery` para exportar los pedidos accesibles y sus
líneas. Consulta el progreso cada cinco segundos, descarga el archivo JSONL con
HTTPX y valida su estructura en una SQLite temporal antes de importar. Cada pedido
y sus líneas se guardan en una transacción. No hace una petición por cada pedido
ni carga todo el archivo en memoria. Al terminar, revisa el resumen de guardados,
omitidos y fallidos. Puedes repetirla sin duplicar pedidos. No hay un tiempo de
carga garantizado ni una medición formal de rendimiento.

Después de una carga Bulk correcta, puedes recuperar cambios cuyo webhook no
haya llegado:

```bash
python sync_orders.py --incremental
```

Consulta cambios por `updated_at` desde el último punto de control, con cinco
minutos de solapamiento. Solo avanza ese punto si la carga termina sin fallos.
Es manual: no existe una tarea programada ni recupera eliminaciones.

## 4. Recibir creación y actualización por webhooks

1. Arranca FastAPI y deja la terminal abierta:

   ```bash
   python -m uvicorn main:app --host 127.0.0.1 --port 8000 --workers 1
   ```

2. Abre `http://127.0.0.1:8000/health`: debe devolver `{"status":"ok"}`.
   Esto comprueba el servidor, no las credenciales de Shopify.
3. En otra terminal, abre el túnel y déjalo funcionando:

   ```bash
   cloudflared tunnel --url http://127.0.0.1:8000
   ```

   Si Windows no encuentra el ejecutable instalado con Winget, prueba:

   ```powershell
   & "C:\Program Files (x86)\cloudflared\cloudflared.exe" tunnel --url http://127.0.0.1:8000
   ```

4. Copia la URL `https://….trycloudflare.com` que aparece y abre su `/health`.
   El dominio cambia al crear otro túnel; actualiza los destinos si ocurre.
5. En Shopify, **Configuración > Notificaciones > Webhooks**, configura dos avisos
   con formato JSON y versión `2026-10`:

   | Evento | Destino |
   | --- | --- |
   | Creación de pedido, `orders/create` | `https://TU-TUNEL/shopify/webhook/orders/create` |
   | Actualización de pedido, `orders/updated` | `https://TU-TUNEL/shopify/webhook/orders/updated` |

   Sustituye `https://TU-TUNEL` por el dominio completo activo. Comprueba el secreto
   en `.env` y reinicia FastAPI si lo has cambiado.

Ambas rutas comparten el receptor: valida firma, tienda y tema, guarda la entrega
en una cola y devuelve HTTP 200. Después, un consumidor consulta el pedido actual
en Shopify y guarda sus datos y todas sus líneas en SQLite. Si falla, reintenta
con espera de 2 a 60 segundos. Los duplicados no crean pedidos duplicados y un
reinicio conserva los trabajos pendientes. Los reintentos vencidos reciben turno
antes de las entregas posteriores.

## 5. Crear, modificar y comprobar un pedido

`check_order.py` consulta solo SQLite. Acepta un único argumento de estas formas:

```bash
python check_order.py 6506
python check_order.py "#6506"
python check_order.py "gid://shopify/Order/456"
```

Sustituye los ejemplos por el número o GID real. El número visible `#6506` y el
identificador del GID son distintos. Consulta la base de `DATABASE_PATH` en `.env`
o, por defecto, `data/shopify.db`. Si no encuentra el pedido, muestra un mensaje
y termina con código 1.

**Creación:** con servidor y túnel abiertos, crea un pedido ficticio en Shopify y
completa su creación. Dejarlo como borrador no dispara `orders/create`. Anota su
número, espera el POST 200 en FastAPI y unos segundos de procesamiento, y búscalo
con `check_order.py`. Ejemplo ilustrativo de la salida:

```text
PEDIDO LOCAL
shopify_order_id: gid://shopify/Order/456
name: #6506
created_at: 2026-11-12T10:00:00+00:00
updated_at: 2026-11-12T10:00:00+00:00
note: (vacío)
financial_status: PENDING
fulfillment_status: UNFULFILLED
current_total_amount: 19.90
currency_code: EUR

LÍNEAS
Producto ficticio | quantity=1 | currentQuantity=1 | gid://shopify/LineItem/789

ÚLTIMOS WEBHOOKS DEL PEDIDO
orders/create | done | intentos fallidos=0 | error=-
```

**Actualización:** edita la nota del mismo pedido en Shopify a
`Prueba de actualización` y guarda. Repite la consulta: conserva GID y número,
cambia la nota y avanza `updated_at`. Entre los avisos aparecerá:

```text
orders/updated | done | intentos fallidos=0 | error=-
```

No ejecutes otra carga entre ambas pruebas: así compruebas el efecto del webhook.
HTTP 200 significa que se guardó el aviso; `done` confirma su procesamiento.

| Resultado | Qué revisar |
| --- | --- |
| GET a una URL webhook devuelve 405 | Es normal: requiere POST firmado. Usa `/health` para comprobar acceso. |
| POST 401 | Secreto de firma incorrecto o servidor pendiente de reiniciar. |
| POST 403 | El dominio de tienda no coincide con la configuración. |
| POST 400 | Tema, ruta, JSON o identificador incorrectos. |
| No llega ningún POST | Servidor, túnel activo y destino configurado en Shopify. |
| Trabajo `pending` | Espera y revisa `last_error`; un error de permisos no se arregla solo. |

## 6. Cambiar estados desde la terminal

Es un flujo simplificado para la prueba, no todos los estados del negocio.
Los comandos ejecutan operaciones reales de Shopify y después refrescan SQLite.
Editar SQLite directamente no modifica Shopify.

| Estado admitido | Resultado en Shopify |
| --- | --- |
| `BORRADOR` | Borrador abierto o con factura enviada; todavía no es un `Order`. |
| `CONFIRMADO` | Pedido creado, pago pendiente y sin preparar: `PENDING` / `UNFULFILLED`. |
| `PAGADO` | Pago registrado, aún sin preparar: `PAID` / `UNFULFILLED`. |
| `PREPARADO` | Pago registrado y preparación completa: `PAID` / `FULFILLED`. |

`NUEVO` es alias de `BORRADOR`; `ENVIADO`, de `PREPARADO`. Este último no acredita
recogida por un transportista ni entrega física. Los rótulos del panel dependen
del idioma; la tabla muestra los valores de pago y preparación de la API.

1. Crea un borrador ficticio en Shopify con importe positivo y un producto físico,
   gestionado por la tienda en una sola ubicación. Toma el ID de la URL del
   borrador y construye `gid://shopify/DraftOrder/ID`.
2. Comprueba el paso sin modificar nada y después confírmalo:

   ```bash
   python change_order.py "gid://shopify/DraftOrder/123" CONFIRMADO --dry-run
   python change_order.py "gid://shopify/DraftOrder/123" CONFIRMADO
   ```

   Sustituye `123` por tu ID. Ejecuta `draftOrderComplete(paymentPending: true)`:
   aparece un pedido con pago pendiente, sin añadir condiciones de pago.
   El comando muestra el nuevo GID de tipo `Order`; úsalo en los siguientes pasos.
3. Registra el pago externo con el GID devuelto:

   ```bash
   python change_order.py "gid://shopify/Order/456" PAGADO
   ```

   Ejecuta `orderMarkAsPaid`. En Shopify queda pagado: registra un pago,
   no realiza un cobro a una tarjeta ni cambia solo una etiqueta local.
4. Prepara todas sus unidades pendientes:

   ```bash
   python change_order.py "gid://shopify/Order/456" PREPARADO
   python check_order.py "gid://shopify/Order/456"
   ```

   Ejecuta `fulfillmentCreate` sin notificar al cliente. Requiere exactamente una
   orden de preparación visible, abierta, sin solicitud enviada y con acción
   `CREATE_FULFILLMENT`. No gestiona varias ubicaciones ni servicios externos.

No permite saltos ni retrocesos: intentar `PAGADO -> CONFIRMADO` devuelve error.
Repetir el estado actual no repite la operación y refresca la copia local del
pedido. Si no existe o no es accesible, lo indica sin ejecutar la operación.
`--dry-run` consulta y valida, pero no garantiza que Shopify acepte el cambio
cuando se ejecute después.

## 7. Filtrar y cambiar pedidos por lotes

El filtro consulta **Shopify**, no SQLite. Por ejemplo:

```bash
python batch_orders.py list BORRADOR --limit 5 --output lotes/borradores.json
```

Admite los cuatro estados anteriores. Muestra el JSON en la terminal y lo guarda,
creando la carpeta si hace falta. `gids` contiene los identificadores seleccionados.
Cada listado correcto reemplaza el contenido del mismo archivo; si no hay
candidatos, deja `gids: []`. Si la consulta falla, conserva el archivo anterior.
Sin `--limit` selecciona hasta 20; con `--limit 0`, todos.

Para filtrar los demás estados, cambia el argumento y el archivo de salida:

```bash
python batch_orders.py list CONFIRMADO --limit 5 --output lotes/pendientes_pago.json
python batch_orders.py list PAGADO --limit 5 --output lotes/pendientes_preparacion.json
python batch_orders.py list PREPARADO --limit 5 --output lotes/ya_preparados.json
```

Revisa los GID antes de ejecutar el lote. Para avanzar los mismos pedidos:

```bash
# 1. Simular la confirmación, sin cambiar Shopify ni SQLite.
python batch_orders.py change CONFIRMADO --input lotes/borradores.json --output lotes/simulacion.json --dry-run

# 2. Confirmar los borradores seleccionados.
python batch_orders.py change CONFIRMADO --input lotes/borradores.json --output lotes/confirmados.json

# 3. Registrar el pago de los pedidos confirmados correctamente.
python batch_orders.py change PAGADO --input lotes/confirmados.json --output lotes/pagados.json

# 4. Preparar los pedidos pagados correctamente.
python batch_orders.py change PREPARADO --input lotes/pagados.json --output lotes/preparados.json
```

Los comentarios `#` sirven en PowerShell y en macOS/Linux. La simulación no se usa
como entrada del siguiente paso: no contiene éxitos reales. Cada resultado tiene
`results` con éxitos y errores, y `gids` solo con pedidos verificados en el estado
solicitado. Al confirmar, devuelve los nuevos GID de `Order`. Puedes consultar
cualquiera con `check_order.py` y verlo en Shopify con el mismo resultado que
en el recorrido individual.

Los cambios son secuenciales, con pausa de 0,5 segundos; se ajusta con `--delay`.
Un fallo no impide intentar el resto. Las salidas de `change` deben ser archivos
nuevos: para repetir, usa la entrada original y otro nombre de salida. Se guardan
resultados después de cada elemento. No existe rollback del lote ni garantía
de evitar límites de Shopify.

## Decisiones, comprobaciones y límites

- FastAPI recibe avisos, HTTPX consulta Shopify y `sqlite3` guarda los datos.
  Bulk evita consultar individualmente miles de pedidos. La cola permite responder
  antes de esperar a Shopify; pedido y líneas se guardan juntos para evitar copias parciales.
- **102 pruebas pasan en Windows**, con respuestas Shopify simuladas y arranque
  real de Uvicorn. Manualmente se verificaron creación y actualización: el borrador
  `#D17` generó `#6506`, presente en SQLite con ambos avisos en `done`. Falta validar
  de extremo a extremo la recuperación incremental y los lotes contra la tienda real.
  Las salidas mostradas en esta guía son ilustrativas.
- Una tienda y un proceso Uvicorn por base. Sin despliegue ni sincronización de
  clientes, inventario, borradores o eliminaciones. Los comandos sí consultan y
  confirman borradores; solo el pedido resultante se guarda localmente.
- No cubre pagos parciales, reembolsos, autorizaciones, cancelados ni preparaciones
  parciales. Shopify y SQLite no comparten transacción: ante una interrupción,
  consulta con `--dry-run` antes de repetir; el cambio remoto puede existir.
- Pendiente del review: respuestas distintas con igual `updatedAt` pueden
  sobrescribirse. El solapamiento incremental tampoco garantiza recuperar cambios
  con retrasos de indexación superiores a cinco minutos. `paymentPending` está
  obsoleto, aunque se comprobó en `2026-10`; revisar antes de cambiar de API.

Código: `app/cli` contiene comandos, `app/services` los procesos, `app/shopify`
las consultas y Bulk, `app/storage` SQLite y `app/webhooks.py` el receptor.
Comandos públicos y `schema.sql` están en la raíz. Más detalle en el
[diseño técnico](docs/technical.md) y el [resumen ejecutivo](docs/executive.md).

Con el entorno virtual activo, para pruebas y formato:

```bash
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests
python -m black --check .
```

Para aplicar formato: `python -m black .`. Credenciales, bases, lotes y archivos
de instrucciones locales están excluidos de Git.
