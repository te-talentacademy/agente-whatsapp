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

## A dónde viajan los datos cuando el cerebro está encendido

Con el cerebro apagado, nada sale de tu servicio salvo el acuse hacia Meta.
Con `FEATURE_BRAIN=on`, cada turno envía a OpenRouter (tu cuenta) el mensaje,
las últimas vueltas de esa conversación, tu personalidad y los fragmentos de
tu libreta relevantes; OpenRouter lo pasa al proveedor del modelo. Tu agente
pide en cada solicitud la política `data_collection: deny`, con la que
OpenRouter descarta a los proveedores marcados como que recopilan datos o
entrenan con ellos. No es cero retención: el proveedor elegido puede registrar
o conservar solicitudes según sus términos, y las políticas de OpenRouter
aplican igual. Cuéntaselo a tus clientes si tu negocio lo requiere, no metas
en la libreta datos que no quieras que salgan de casa, y si necesitas una
garantía más fuerte, revisa en OpenRouter los modelos con endpoints de cero
retención (`zdr`).

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
- `modelo o proveedor no disponible con la política de privacidad` → el
  modelo que elegiste no tiene un proveedor que pase el filtro «sin
  recopilación de datos». Deja `OPENROUTER_MODEL` vacía para usar el
  recomendado (que sí lo pasa) o elige otro modelo con esa política.
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

## Si los oídos, los ojos o la voz no funcionan

- Los tres ramales necesitan `FEATURE_BRAIN=on` y la llave de OpenRouter; los
  oídos y la voz necesitan además `CARTESIA_API_KEY` (y la voz,
  `CARTESIA_VOICE_ID`). Cada falta se anuncia en los registros con su nombre.
- `NOTA DE VOZ transcrita de …` → los oídos funcionan; ese texto entra al
  turno como si la persona lo hubiera escrito.
- `Nota de voz enviada a …` → la voz salió. Si en su lugar ves `La voz fallo
  (…)` o `Voz omitida: …`, ahí está el motivo (llave, tope diario, respuesta
  demasiado larga, turno viejo): el texto ya se entregó igual.
- `Tope diario de voz alcanzado (DAILY_VOICE_LIMIT)` → tu cinturón de gasto
  de Cartesia actuó; súbelo si te quedó corto.
- Las fotos solo se miran con `FEATURE_VISION=on` y siempre viajan al modelo
  con visión (`OPENROUTER_VISION_MODEL`); si pusiste ahí un modelo sin
  visión, el rechazo aparece en los registros con el nombre de la variable.
- El audio de las notas de voz va a Cartesia (tu cuenta) para transcribirse,
  y el texto de las respuestas va a Cartesia para locutarse; las fotos van al
  modelo con visión con la misma política de privacidad del cerebro. Con los
  ramales apagados, nada de esto sale de tu servicio.

## Si el teléfono no funciona

El teléfono es el ramal con más piezas: cerebro + voz + el puente de audio
(TURN de Cloudflare). Cada rechazo queda en los registros con su motivo, en
este vocabulario:

- `LLAMADA … rechazada: FEATURE_CALLS está apagado` → el interruptor.
- `LLAMADA … rechazada: el cerebro está apagado o sin llave` → las llamadas
  necesitan `FEATURE_BRAIN=on` y `OPENROUTER_API_KEY`.
- `LLAMADA … rechazada: faltan CARTESIA_API_KEY / CARTESIA_VOICE_ID` → sin
  voz no hay llamada.
- `LLAMADA … rechazada: sin-relay-vigente` → faltan las dos llaves TURN de
  Cloudflare (el puente de audio).
- `el relevo rechazó la credencial (401): revisa las llaves TURN` → las
  llaves TURN están mal copiadas o revocadas. Ver el fallo típico de abajo.
- `LLAMADA … rechazada: ya hay una llamada en curso` → el agente atiende una
  a la vez; la segunda persona recibe el rechazo y puede escribir por texto.
- `tope diario de minutos de llamada alcanzado` → tu cinturón actuó; sube
  `CALL_DAILY_MINUTES_LIMIT` si te quedó corto.
- `LLAMADA …: quedó a medias (…); la cierro` → un reinicio o despliegue
  interrumpió una llamada; el agente la cierra solo, con motivo (`reinicio`
  o `lease-vencido`), y queda listo para la siguiente. No hay nada que hacer.
- `LLAMADA …: aparto N segundos … (reserva)` / `consumo real N s; devuelvo
  N s … (conciliación)` → la contabilidad del cupo diario: se aparta el
  máximo al descolgar y se devuelve lo no usado al colgar. Si el servicio se
  cae a mitad de llamada, lo apartado queda contado (el cupo jamás se regala).

### El fallo típico, resuelto paso a paso: el puente de audio caído

Síntoma: nadie puede llamarte; en los registros aparece, en cada intento,
`el relevo rechazó la credencial (401): revisa las llaves TURN` y la llamada
se rechaza. El texto sigue funcionando perfecto (los ramales son
independientes).

1. **Diagnóstico**: abre los registros de Railway y busca `relevo`. El 401
   dice que Cloudflare no aceptó tus llaves: fueron borradas, rotadas o mal
   copiadas.
2. **Arreglo**: entra a tu panel de Cloudflare (sección Realtime → TURN),
   crea o vuelve a copiar la TURN key, y pega sus dos valores en
   `CLOUDFLARE_TURN_KEY_ID` y `CLOUDFLARE_TURN_API_TOKEN` en Railway.
3. **Verificación**: Railway redespliega solo al guardar variables. Llama de
   nuevo: en los registros verás `relevo de audio listo (TLS 443, vida …)`
   seguido de `LLAMADA …: descolgada`. Puente reparado.

### Si las llamadas salientes no salen

1. `FEATURE_CALLS=on` **y** `FEATURE_OUTBOUND_CALLS=on` (doble candado).
2. `CALL_OWNER_NUMBER` es TU número — solo tú puedes ordenar `llamar +52…`,
   escribiéndoselo al agente por WhatsApp, tal cual, sin nada más en el
   mensaje.
3. El agente te confirma cada paso por texto: `Solicitud de permiso
   enviada`, `Permiso recibido …: llamando ahora`, o el motivo exacto si no
   pudo. Sin la aceptación de la persona no hay llamada: así funciona
   WhatsApp, y es lo correcto.
4. Las solicitudes de permiso tienen límite (1 al día, 2 por semana por
   persona) y el agente lo respeta él solo — si te frena, te dice hasta
   cuándo.

## Fichas agotadas

Si un mensaje falló cinco veces seguidas, su ficha queda marcada como agotada
y deja de reintentar para no atascar la fila. En los registros queda el último
error. Arregla la causa (casi siempre una llave) y pide que te vuelvan a
escribir: un mensaje nuevo de esa persona revive la ficha desde cero.

---

## Cuando esta guía no alcanza

Si el síntoma no está aquí o el arreglo no funcionó, pregunta en el portal de
la academia: <https://app.talent-academy.com/es/preguntas>. Copia en tu
pregunta las líneas del registro donde aparece el problema.
