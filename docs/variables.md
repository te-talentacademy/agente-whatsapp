# Guía de variables — la caja fuerte de tu agente

Tu agente no guarda ninguna llave dentro del código. Todo lo delicado y todo
lo configurable vive en **variables de entorno**: la caja fuerte de tu
servicio en Railway (pestaña **Variables**). Cambias una variable, Railway
vuelve a desplegar solo, y tu agente despierta con la configuración nueva.
Sin tocar una línea de código.

Esta es la lista completa, con su nombre EXACTO. Los nombres importan letra
por letra: cópialos tal cual.

---

## Puesta en marcha — el timbre (las 4 obligatorias)

Con estas cuatro variables tu agente queda vivo: recibe mensajes, los guarda
y contesta un acuse sencillo.

| Variable | Qué es | De dónde sale | Ejemplo |
|---|---|---|---|
| `WHATSAPP_TOKEN` | La llave permanente de tu WhatsApp. Es la más delicada de todas: trátala como la llave de tu casa. | El token permanente del System User que creaste en tu cuenta de negocio de Meta | `EAAG…` (cadena larga) |
| `WHATSAPP_PHONE_NUMBER_ID` | El identificador de tu número de pruebas. No es el número de teléfono: es el código interno que Meta le pone. | Panel de tu app de Meta, sección **WhatsApp**, junto a tu número de pruebas | `123456789012345` |
| `WHATSAPP_VERIFY_TOKEN` | La contraseña compartida del timbre. La inventas tú: cualquier palabra sirve. Tiene que ser IDÉNTICA, letra por letra, aquí y en el panel de Meta. | La escribes tú misma/o | `miagente2026` |
| `META_APP_SECRET` | La firma secreta de tu app. Con ella tu agente comprueba que cada aviso viene de Meta de verdad y no de un desconocido imitándola. Sin esta variable, el timbre no acepta avisos. | Panel de tu app de Meta, en los ajustes básicos de la app (el campo se llama **App secret**; pulsa mostrar) | `a1b2c3…` (32 caracteres) |

> **Ojo con la trampa clásica**: si la verificación del timbre falla, casi
> siempre es porque el `WHATSAPP_VERIFY_TOKEN` no coincide letra por letra
> entre Railway y Meta, o porque Railway todavía estaba redesplegando cuando
> pulsaste verificar. Espera medio minuto y reintenta.

### El volumen: la memoria que no se borra

Railway borra el disco normal del servicio con **cada despliegue** — y tu
agente se despliega cada vez que editas un archivo. Para que los mensajes
pendientes y el registro de mensajes ya atendidos sobrevivan, tu servicio
necesita un **volumen**: un cajón de disco permanente.

Se crea una sola vez, desde el panel de Railway: agrega un volumen a tu
servicio y móntalo exactamente en `/data`. Listo — el agente lo detecta solo.
Si arrancas sin volumen, funciona igual, pero te lo recuerda en los registros.

---

## Comportamiento base (opcionales, ya vienen bien de fábrica)

| Variable | Qué hace | Valor de fábrica |
|---|---|---|
| `AUTO_REPLY` | El acuse: contestar «recibí tu mensaje» mientras el agente aún no piensa. `on` u `off`. | `on` |
| `AUTO_REPLY_TEXT` | El texto de ese acuse, por si quieres el tuyo. | Un saludo sencillo incluido |
| `LOG_MESSAGE_TEXT` | Mostrar el texto de cada mensaje en los registros. Perfecto para aprender; cuando atiendas clientes reales puedes apagarlo con `off`. | `on` |
| `DB_PATH` | Dónde guardar el archivo de memoria. Casi nunca hace falta tocarla: el agente elige solo el volumen si existe. | automático |

---

## El cerebro (se enciende en su fase del curso)

| Variable | Qué hace | Valor de fábrica |
|---|---|---|
| `FEATURE_BRAIN` | Enciende el cerebro: las respuestas pensadas de verdad. Como pensar cuesta dinero (poco, pero dinero), viene apagado. | `off` |
| `OPENROUTER_API_KEY` | Tu llave de OpenRouter, la central que conecta con el modelo que piensa. | — |
| `OPENROUTER_MODEL` | Qué modelo piensa las respuestas. Ya trae uno elegido, el mismo que usa un sistema real en producción: rápido, bueno y muy barato (`deepseek/deepseek-v4-flash`). Solo cámbiala si sabes lo que buscas. | el recomendado |
| `AGENT_NAME` | El nombre con el que tu agente se presenta (sustituye `{nombre}` en `personalidad.md`). | `Mi agente` |
| `DAILY_MESSAGE_LIMIT` | Tope de **solicitudes al modelo** por día, contando a todas las personas: tu cinturón de seguridad de gasto. Se aparta el cupo ANTES de cada solicitud (un intento que se corta por la red también cuenta, porque pudo cobrarse); solo se devuelve si el proveedor rechazó la solicitud sin procesarla. Al llegar al tope, el agente avisa con un mensaje fijo y deja de gastar hasta mañana. Vacía = 200. Un `0` escrito a propósito = sin límite. | 200 |

> **Privacidad del cerebro**: con `FEATURE_BRAIN=on`, cada turno envía a
> OpenRouter el mensaje de la persona, las últimas vueltas de esa conversación,
> tu `personalidad.md` y los fragmentos de tu libreta que vengan al caso.
> Todas las solicitudes llevan de fábrica la política `data_collection: deny`:
> OpenRouter **descarta a los proveedores marcados como que recopilan datos o
> entrenan con ellos**. Lo que esa política NO garantiza: cero retención (ese
> es otro control, `zdr`, que hoy no se activa porque dejaría sin proveedor al
> modelo recomendado); el proveedor elegido puede registrar o conservar
> solicitudes según sus propios términos, y las políticas de OpenRouter siguen
> aplicando. Si para el modelo elegido no existe un proveedor que cumpla la
> política, la solicitud se rechaza (lo verás en los registros) y el agente
> responde con el acuse.

> **La personalidad no es una variable**: vive en el archivo `personalidad.md`
> de tu copia. Lo editas desde GitHub (el lápiz, arriba a la derecha del
> archivo), guardas, y Railway redespliega con el carácter nuevo.

## La libreta de conocimiento (se enciende en su fase)

| Variable | Qué hace | Valor de fábrica |
|---|---|---|
| `FEATURE_RAG` | Enciende la libreta: tu agente responde con TUS documentos (los archivos `.md` y `.txt` de la carpeta `conocimiento/` de tu copia). Necesita el cerebro encendido. Sin llaves ni servicios extra: la libreta vive dentro de tu propio servicio y se rearma en cada despliegue. | `off` |

## Oídos, ojos y voz (se encienden en su fase)

| Variable | Qué hace | Valor de fábrica |
|---|---|---|
| `FEATURE_VOICE_IN` | Los oídos: entender las notas de voz que te mandan. | `off` |
| `FEATURE_VOICE_OUT` | La voz: responder con notas de voz con TU voz clonada. Si la voz falla, sale texto: la conversación nunca se corta. | `off` |
| `FEATURE_VISION` | Los ojos: entender fotos. Solo trabaja cuando hay una imagen adjunta — jamás gasta de más. | `off` |
| `CARTESIA_API_KEY` | Tu llave de Cartesia (oídos y voz usan la misma cuenta). | — |
| `CARTESIA_VOICE_ID` | El identificador de tu voz clonada en Cartesia. | — |
| `OPENROUTER_VISION_MODEL` | El segundo motor: el modelo que mira las fotos. Ya trae uno elegido. | el recomendado |

## El teléfono (se enciende en su fase)

| Variable | Qué hace | Valor de fábrica |
|---|---|---|
| `FEATURE_CALLS` | Contestar llamadas de WhatsApp con tu voz. | `off` |
| `FEATURE_OUTBOUND_CALLS` | Hacer llamadas (siempre pidiendo permiso antes). Doble candado: exige también `FEATURE_CALLS`. | `off` |
| `CLOUDFLARE_TURN_KEY_ID` | El puente de audio de Cloudflare (primera de dos). | — |
| `CLOUDFLARE_TURN_API_TOKEN` | El puente de audio de Cloudflare (segunda de dos). | — |
| `CALL_MAX_MINUTES` | Tope de duración por llamada. | 5 |
| `CALL_DAILY_MINUTES_LIMIT` | Tope de minutos de llamada por día. Vacía = 30. `0` = sin límite. | 30 |

---

## Las reglas de la caja fuerte

1. **Los interruptores** (`FEATURE_…`) solo encienden con la palabra exacta
   `on`. Cualquier otra cosa — vacío, un error de dedo, la variable ausente —
   cuenta como apagado. Si algo cuesta dinero, la duda apaga.
2. **Los topes** (`…_LIMIT`, `…_MINUTES`): variable vacía = el valor
   recomendado; un `0` escrito a propósito = sin límite. Así una variable en
   blanco jamás te deja sin protección por accidente.
3. **Ninguna llave va en el código.** Nunca. Si alguien ve tu repositorio,
   no ve ninguna llave: todas viven en tu caja fuerte de Railway.
