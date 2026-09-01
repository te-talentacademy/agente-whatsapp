# Tu libreta de conocimiento

Todo documento que pongas en esta carpeta pasa a ser parte de lo que tu agente
sabe de tu negocio: archivos de texto (`.md`, `.txt`), **PDF — con texto o
escaneado —, Word (`.docx`) y Excel (`.xlsx`)**. Con `FEATURE_RAG=on`, cada
vez que Railway despliega tu copia, el agente lee estos archivos, los trocea y
los tiene a la mano para responder con TUS datos.

Súbelos desde la página de GitHub: entra a esta carpeta, botón «Add file →
Upload files» (o arrastra y suelta), guarda, y Railway redespliega solo. La
conversión de PDF/Word/Excel ocurre DENTRO de tu servicio en Railway — no
instalas nada en tu computadora. GitHub acepta hasta ~25 MB por archivo al
subir por navegador.

Los tres archivos de ejemplo describen un negocio inventado, «Panadería La
Espiga», para que veas el formato y pruebes desde el primer día. Bórralos
cuando subas los tuyos.

## Qué meter

- Catálogo con precios y descripciones cortas.
- Horarios, ubicación, formas de pago y de entrega.
- Preguntas frecuentes con su respuesta tal como te gusta darla.
- Políticas: cambios, devoluciones, tiempos de entrega, anticipos.

## Cómo prepararlo

- Un tema por archivo, con títulos (`#`, `##`) y párrafos cortos: el agente
  subraya por párrafos, así que cada párrafo debería poder leerse solo.
- Escribe como le hablarías a un cliente; el agente copia tu tono.
- Sin datos sensibles: nada de contraseñas, datos bancarios completos ni
  información personal de clientes.

## PDF escaneado: lo que hay que saber

Un PDF escaneado (páginas que son foto, sin texto que copiar) también
funciona, con dos condiciones:

- Necesita el motor de visión encendido (`FEATURE_VISION=on` y tu
  `OPENROUTER_API_KEY`): es quien LEE cada página escaneada. Cada página
  leída cuesta unos centavos y toma unos segundos; el resultado se guarda en
  la memoria de tu servicio y **no se vuelve a pagar** en los siguientes
  despliegues (solo se relee si cambias el archivo).
- Hay topes de protección: hasta 20 páginas escaneadas por documento y hasta
  60 lecturas por despliegue. Si subes muchos PDF escaneados a la vez, el
  primer armado toma varios minutos y avanza por partes — los registros de
  Railway te dicen cuántas páginas leyó de cada archivo y cuántas quedaron
  para el siguiente despliegue. Si un documento pasa del tope, divídelo.

## Protecciones que trae puestas

- Un archivo dañado, demasiado pesado (más de 30 MB) o con forma sospechosa
  se salta con un aviso en los registros; el resto de la libreta se arma
  igual. Si el archivo dañado reemplazaba a uno que ya funcionaba, el agente
  conserva la versión anterior — solo BORRAR el archivo de esta carpeta lo
  saca de la libreta.
- En un Excel, el agente lee los valores ya calculados: si usas fórmulas,
  ábrelo y guárdalo en Excel o Google Sheets antes de subirlo.
- La primera vez que tu servicio arranca con volumen nuevo, la libreta tarda
  ese primer armado en aparecer; después, cada redespliegue sirve la libreta
  anterior mientras arma la nueva.

## Cómo probarlo

Escríbele a tu agente una pregunta cuya respuesta esté en un archivo. Si no la
encuentra, revisa que la pregunta y el documento compartan palabras clave
(«horario», «envío», el nombre del producto), que el archivo tenga una de las
extensiones aceptadas y — si es un PDF escaneado — que la visión esté
encendida y los registros no marquen páginas saltadas.
