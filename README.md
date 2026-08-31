# Tu agente de WhatsApp

Esta es la plantilla de un agente de WhatsApp completo. La usas una vez, con
el botón **«Use this template»**, y desde ese segundo la copia es tuya: tu
repositorio, tu servicio, tus llaves, tus reglas.

---

## 1. Qué es

Un agente de WhatsApp **completo y listo para producción**: contesta
mensajes de texto, entiende notas de voz e imágenes, responde con tu propia
voz y, cuando lo enciendas, hasta descuelga llamadas.

No es un juguete de curso. Es la misma arquitectura que un sistema real que
opera en producción atendiendo cientos de conversaciones diarias: el timbre
que confirma rápido y trabaja con calma por detrás, la fila de espera que no
pierde mensajes, los reintentos con pausa, los topes de gasto y los
interruptores de emergencia. Todo eso ya viene armado; tú lo vas encendiendo
pieza por pieza, a tu ritmo.

Y el camino completo se hace desde el navegador: sin terminal, sin instalar
nada en tu computadora.

## 2. Qué incluye

Un solo esqueleto que va ganando órganos. Cada órgano se enciende con una
variable — sin tocar código:

| Órgano | Qué hace | Variable que lo enciende |
|---|---|---|
| **El timbre y la fila** | Recibe cada mensaje, comprueba que viene de Meta de verdad, lo guarda y confirma al instante. Nada se pierde, nada se duplica. | Activo desde el primer día (con las 4 variables de la puesta en marcha) |
| **El acuse** | Mientras el agente aún no piensa, contesta «recibí tu mensaje». | `AUTO_REPLY` (ya viene encendido) |
| **El cerebro** | Piensa respuestas de verdad, con la personalidad que tú le escribas. | `FEATURE_BRAIN` |
| **La libreta de conocimiento** | Responde con TUS documentos: tu catálogo, tus precios, tus preguntas frecuentes. | `FEATURE_RAG` |
| **Los oídos** | Entiende las notas de voz que te mandan. | `FEATURE_VOICE_IN` |
| **Los ojos** | Entiende las fotos — y solo mira cuando hay foto, para no gastar de más. | `FEATURE_VISION` |
| **La voz** | Responde con notas de voz… con TU voz. Si la voz falla, sale texto: la conversación nunca se corta. | `FEATURE_VOICE_OUT` |
| **El teléfono** | Contesta llamadas de WhatsApp con tu voz; y hace llamadas, siempre pidiendo permiso antes. | `FEATURE_CALLS` / `FEATURE_OUTBOUND_CALLS` |
| **El blindaje** | Topes de gasto por día, interruptores de emergencia y registros claros para operarlo tranquila/o. | Topes y candados en la [guía de variables](docs/variables.md) |

La lista exacta de variables, con su nombre letra por letra y de dónde sale
cada llave, vive en **[docs/variables.md](docs/variables.md)** — esa guía es
la fuente de verdad.

## 3. Qué pones tú

La plantilla trae toda la maquinaria. Tú pones lo que la hace TUYA:

- **Tus llaves**: tu cuenta de Meta (WhatsApp), tu llave de OpenRouter (el
  cerebro) y tu cuenta de Cartesia (la voz). Cada una se crea en minutos y
  vive en tu caja fuerte de variables, jamás en el código.
- **Tu conocimiento**: los documentos de tu negocio, en la carpeta
  `conocimiento/` de tu copia.
- **Tu voz**: tu clon de voz, creado por ti en tu cuenta de Cartesia.
- **Tu número**: el número de pruebas gratuito de Meta para aprender, y tu
  número real cuando decidas salir a producción.

## 4. Qué NO es

- **No es un SaaS.** No hay mensualidad de esta plantilla, no hay panel de
  nadie más, no hay letra chica.
- **No hay ningún servidor de terceros en medio.** Tus mensajes van de Meta a
  TU servicio. Nadie más los ve.
- **No dependes de nadie**, salvo de tus propias cuentas (Meta, Railway,
  OpenRouter, Cartesia) — todas a tu nombre, todas bajo tu control.
- **La copia es tuya para siempre.** Puedes leerla, cambiarla, romperla y
  arreglarla. Nadie te la puede quitar ni apagar.

---

## Puesta en marcha

El recorrido completo, con capturas y trucos, lo ves en la lección. El
resumen es este:

1. **Saca tu copia**: botón **«Use this template»** → **«Create a new
   repository»** → ponle el nombre de tu negocio.
2. **Despliega en Railway**: **New Project** → **«Deploy from GitHub repo»** →
   elige tu copia → **Deploy Now**.
3. **El volumen**: agrega a tu servicio un volumen montado en `/data` — la
   memoria que sobrevive a cada despliegue (una sola vez; detalle en la
   [guía de variables](docs/variables.md)).
4. **La caja fuerte**: en la pestaña **Variables**, pega las 4 de la puesta en
   marcha: `WHATSAPP_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`,
   `WHATSAPP_VERIFY_TOKEN` y `META_APP_SECRET`.
5. **Tu dirección**: **Settings → Networking → Generate Domain** — copia la
   dirección generada.
6. **Conecta el timbre**: en el panel de Meta (**WhatsApp → Configuration**),
   pega la dirección en **Callback URL**, escribe tu misma palabra en
   **Verify token** y pulsa **Verify and save**. Después — paso aparte que
   casi todo el mundo se salta — pulsa **Manage** y suscríbete al campo
   **messages**.
7. **El primer hola**: escríbele a tu número de pruebas desde tu celular y
   mira aparecer el mensaje en los registros de Railway, uno o dos segundos
   después.

¿Algo no salió a la primera? La [guía de operación](docs/runbook-operacion.md)
trae el "revisa en orden" de cada tropiezo típico.

## Enciende el cerebro y la libreta

Cuando el timbre ya suena, tu agente empieza a pensar con dos variables y un
archivo:

1. **La llave**: crea tu cuenta en OpenRouter, genera una llave y pégala en
   Railway como `OPENROUTER_API_KEY`. Enciende el cerebro con
   `FEATURE_BRAIN=on`. Desde ese redeploy, tu agente responde de verdad.
2. **Su carácter**: edita `personalidad.md` desde GitHub (el lápiz arriba a la
   derecha del archivo): quién es, cómo habla, qué hace y qué no. Guarda, y
   Railway redespliega con la personalidad nueva.
3. **Su conocimiento**: sube tus documentos (`.md` o `.txt`) a la carpeta
   `conocimiento/` — catálogo, horarios, preguntas frecuentes — y enciende la
   libreta con `FEATURE_RAG=on`. Los tres archivos de ejemplo describen un
   negocio inventado para que pruebes antes de subir los tuyos.

Tu cinturón de seguridad viene puesto: `DAILY_MESSAGE_LIMIT` (200 respuestas
por día de fábrica) frena el gasto si algo se descontrola.
