# Operar tu agente — guía del día a día

Tu agente vive solo en la nube, pero conviene saber mirarlo. Todo lo de esta
guía se hace desde el navegador.

## Leer los registros (logs)

En Railway, entra a tu servicio y abre la pestaña **Deployments** o **Logs**.
Ahí ves, en tiempo real, lo que tu agente va haciendo:

- `MENSAJE RECIBIDO de +52…: "hola"` — alguien tocó el timbre y el mensaje
  quedó guardado. Esta línea aparece uno o dos segundos después de enviado el
  mensaje; nunca minutos.
- `Agente listo. Esperando el primer mensaje.` — el arranque terminó bien.
- Líneas de `AVISO` o `ERROR` — el agente te está contando qué le falta o qué
  no pudo hacer, en claro. Léelas: casi siempre traen la solución escrita.

## Saber si está vivo

Abre en el navegador la dirección de tu servicio (la que generaste en
**Settings → Networking**). Si ves «Tu agente está vivo», todo está en orden:
la memoria responde y el trabajador de fondo está despierto. Esa misma página
la usa Railway para vigilar cada despliegue.

## Cada cuánto se despliega

Cada vez que cambias un archivo de tu copia (o una variable), Railway vuelve a
desplegar solo. Tarda un par de minutos. Mientras tanto tu agente sigue
contestando con la versión anterior hasta el cambio de guardia.

## La memoria y el volumen

Con el volumen montado en `/data` (ver la guía de variables), los mensajes
pendientes y el registro de mensajes ya atendidos sobreviven a cada
despliegue. Si en los registros ves el aviso de «disco temporal», te falta
adjuntar el volumen: hazlo una vez y olvídate.

## El texto de los mensajes en los registros

De fábrica, el agente escribe en los registros el texto de cada mensaje que
recibe: así aprendes viéndolo trabajar. Cuando tu agente empiece a atender
clientes de verdad, esos mensajes son conversaciones privadas de tu negocio —
apaga el detalle poniendo `LOG_MESSAGE_TEXT=off` en tus variables. Los
registros seguirán mostrando que llegó un mensaje y de quién, sin el
contenido.

## Si un mensaje no aparece en los registros

Revisa en orden — es casi siempre uno de estos tres:

1. **La suscripción**: en el panel de Meta, además de verificar el timbre hay
   un segundo paso — pulsar **Manage** y activar el campo **messages**.
   Verificar y suscribirse son DOS acciones distintas.
2. **La contraseña compartida**: `WHATSAPP_VERIFY_TOKEN` debe ser idéntica,
   letra por letra, en Railway y en Meta.
3. **El redeploy**: si acabas de cambiar una variable, espera a que Railway
   termine de desplegar y prueba de nuevo.

## Si el agente no contesta el acuse

- Con el número de pruebas de Meta solo pueden escribirte (y recibir
  respuesta) los números que agregaste a la lista de destinatarios del panel.
  No es una falla: es el corralito de pruebas de Meta.
- Revisa los registros: si el envío falló, ahí está el motivo en claro
  (llave vencida, servicio saturado…). Los fallos pasajeros se reintentan
  solos, con pausas cada vez más largas.

## Si el cerebro no responde (o responde con el acuse)

Con `FEATURE_BRAIN=on`, en los registros verás `RESPUESTA a +52…: "…"` con
lo que el agente contestó. Si en vez de eso ves el acuse o un aviso, el motivo
está en la línea de al lado:

- `falta OPENROUTER_API_KEY` → pega tu llave en Variables.
- `llave rechazada` → la llave está mal copiada o fue revocada; genera otra en
  OpenRouter.
- `sin saldo en OpenRouter` → recarga tu cuenta (el gasto normal es de
  centavos al día).
- `modelo no encontrado` → deja `OPENROUTER_MODEL` vacía para usar el
  recomendado.
- `servicio saturado` → pasajero: el agente reintenta solo con pausa.
- `Tope diario de respuestas alcanzado` → tu cinturón de seguridad actuó;
  sube `DAILY_MESSAGE_LIMIT` si el tope te quedó corto.

## Si el agente no usa tus documentos

1. `FEATURE_RAG=on` y `FEATURE_BRAIN=on` las dos.
2. Los archivos están en `conocimiento/` con extensión `.md` o `.txt`
   (`README.md` no se indexa).
3. En los registros del arranque aparece `Libreta lista: N fragmentos` con
   N mayor que cero. Si dice 0, los archivos no se subieron a tu copia.
4. La pregunta y el documento comparten palabras clave: el agente subraya por
   palabras, así que «¿a qué hora abren?» encuentra «horario» si tu documento
   dice «horario de atención» y «abrimos».

## Fichas agotadas

Si un mensaje falló cinco veces seguidas, su ficha queda marcada como agotada
y deja de reintentar para no atascar la fila. En los registros queda el último
error. Arregla la causa (casi siempre una llave) y pide que te vuelvan a
escribir: un mensaje nuevo de esa persona revive la ficha desde cero.
