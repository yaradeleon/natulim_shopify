# Resumen ejecutivo

La primera versión copia el estado de pedidos de Shopify a SQLite y permite
reflejar cambios posteriores conservando la identidad del pedido y sus líneas.
Prioriza una demostración reproducible de creación y actualización dentro del
tiempo de la prueba técnica.

El comando `sync_orders.py` exporta en bloque los pedidos existentes accesibles
y todas sus líneas con Bulk Operations, valida el archivo y los importa. Puede
repetirse sin duplicarlos. Tanto esta carga como las notificaciones usan el mismo
guardado transaccional. Los tokens se obtienen y renuevan automáticamente con
Client ID y Client Secret y permanecen en memoria.

Se reciben notificaciones firmadas, se conservan en una cola local y se consulta
GraphQL Admin para recuperar el estado completo. Las entregas repetidas no duplican
pedidos. Los fallos de consulta se vuelven a intentar y los trabajos pendientes
sobreviven a un reinicio.

La implementación está comprobada con pruebas automatizadas y datos ficticios.
En la prueba manual se recibió un aviso real y el usuario confirmó la actualización.
Se creó un pedido nuevo en Shopify; queda registrar su comprobación local y el
resumen de importación. El README explica cómo reproducir y verificar ambos flujos
en Windows, macOS y Linux con consulta por comando. No se ha desplegado.

Clientes, productos, variantes e inventario quedan fuera de esta primera fase.
La siguiente ampliación prevista es crear o actualizar clientes locales por su
identificador Shopify, respetando los pedidos sin cliente asociado.

El alcance operativo es una tienda y un proceso local. Una evolución para uso
continuado añadiría reconciliación de eventos perdidos, monitorización, purga de
trabajos completados y gestión de errores permanentes.
